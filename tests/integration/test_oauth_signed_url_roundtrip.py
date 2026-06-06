"""R33 integration-grade roundtrip fences (security-critical post-#50/#60).

Per the R33 cross-agent consensus: per-unit fences exist for OAuth state,
signed-URL HMAC, and ``NeedsReauthError`` surfacing — but the JOINTS that
trace data end-to-end through multiple production modules don't. This
file fills the three gaps that survived the audit:

1. **OAuth callback rejects bad state at the HTTP boundary.**
   ``test_oauth_state.py`` covers ``verify_state`` in isolation.
   ``test_fresh_user_flow.py`` covers the happy-path callback. Neither
   verifies that an HMAC-tampered ``state`` arriving at the real
   ``/oauth/google/api/callback`` route is rejected without persisting
   anything — which is the production attacker path.

2. **gdocs_get_signed_upload_url output round-trips through /api/convert.**
   ``test_api_convert_multitenancy.py`` mints via the lower-level
   ``crypto.sign_upload_url``. The production MCP-tool path adds auth-
   context resolution, env-var key derivation, and a ``user_id`` echo
   that the unit test bypasses. If any of those drift, this test fails.

3. **Revoked-token flow surfaces as 401 + auth_url through /api/convert.**
   ``test_credentials.py`` proves ``get_credentials_for_user`` raises
   ``NeedsReauthError`` on ``invalid_grant``. The endpoint test in
   ``test_api_convert_multitenancy.py`` patches the resolver to raise
   directly. This test stitches the two: a real ``RefreshError`` from
   the bottom of the stack propagates through the resolver, then the
   middleware, then the convert endpoint, and emerges as the clean
   401 / auth_url contract the client depends on.

Mocks ``Flow.from_client_config`` and ``Credentials.refresh`` at the
same boundaries the existing unit tests do — the lower oauthlib stack
uses the synchronous ``requests`` transport, so an httpx-level mock
would not catch it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


# ---------------------------------------------------------------------
# Shared scaffolding (mirrors test_api_convert_multitenancy.py +
# test_fresh_user_flow.py conventions so a reader who knows either
# file can navigate this one without surprise.)
# ---------------------------------------------------------------------


SIGNING_KEY = "test-signing-key-r33-roundtrip-fences"


@pytest.fixture(autouse=True)
def env_overrides(monkeypatch, tmp_path):
    """Pin every env var the production code reads.

    ``isolated_db`` (from tests/conftest.py) already points
    ``GOOGLE_DOCS_USER_STORE_PATH`` and ``GOOGLE_DOCS_DATA_DIR`` at
    ``tmp_path``. We add the auth/signing env on top, mirroring the
    operator workflow documented in RUNBOOK §3.6 — pre-flip every
    purpose-key override is pinned to the master so HKDF derivation
    is bypassed in tests.
    """
    monkeypatch.setenv("MCP_BEARER_TOKEN", SIGNING_KEY)
    monkeypatch.setenv("MCP_API_BEARER_KEY", SIGNING_KEY)
    monkeypatch.setenv("OAUTH_STATE_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("SIGNED_URL_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver,localhost")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS_JSON", json.dumps(_client_config()),
    )
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")


@pytest.fixture(autouse=True)
def reset_nonce_store():
    """Each test starts with a fresh process-global nonce store so
    nonces from prior tests don't collide. Mirrors the pattern from
    test_api_convert_multitenancy.py.

    v2.2.1: the canonical binding lives in ``http_server._state``;
    middleware + oauth route both access it via late binding through
    that module, so reassigning the package-level ``_NONCE_STORE``
    re-export alone would NOT propagate.
    """
    from appscriptly import http_server
    from appscriptly.crypto import NonceStore
    from appscriptly.http_server import _state
    fresh = NonceStore()
    _state._NONCE_STORE = fresh
    http_server._NONCE_STORE = fresh  # keep the re-export in sync
    yield


def _client_config() -> dict:
    """Operator-level OAuth client config — production shape."""
    return {
        "web": {
            "client_id": "TEST_CLIENT_ID.apps.googleusercontent.com",
            "client_secret": "TEST_CLIENT_SECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": (
                "https://www.googleapis.com/oauth2/v1/certs"
            ),
            "redirect_uris": [
                "https://example.fly.dev/oauth/google/api/callback",
            ],
        },
    }


def _mock_flow_returning(refresh_token: str, access_token: str, scopes: list[str]):
    """Stand-in for ``google_auth_oauthlib.flow.Flow`` after fetch_token.

    Same shape as test_fresh_user_flow.py's helper — duplicated rather
    than imported to keep this file standalone for the next refactor.
    """
    flow = MagicMock()
    creds = MagicMock()
    creds.refresh_token = refresh_token
    creds.to_json.return_value = json.dumps({
        "token": access_token,
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "TEST_CLIENT_ID.apps.googleusercontent.com",
        "client_secret": "TEST_CLIENT_SECRET",
        "scopes": scopes,
        "expiry": (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=1)
        ).isoformat(),
    })
    flow.credentials = creds
    return flow


def _build_convert_app():
    """Production Starlette app wired against a stub FastMCP — the
    /api/convert route is what we exercise; /mcp is never called."""
    from fastmcp import FastMCP
    from appscriptly.http_server import build_app
    return build_app(FastMCP("stub-for-r33-roundtrip"))


def _docx_form(content: bytes = b"PK\x03\x04minimaldocx"):
    return {
        "file": (
            "test.docx", content,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    }


# ---------------------------------------------------------------------
# R33 Gap #1 — OAuth callback rejects HMAC-tampered state at the
#              HTTP boundary, without persisting any user row.
# ---------------------------------------------------------------------


def test_oauth_callback_rejects_hmac_tampered_state():
    """A state with a flipped signature byte must be rejected by the
    HTTP callback handler before any token exchange or user_store
    write. Catches the joint failure where ``verify_state`` returns
    False but the handler ignores it and persists anyway."""
    from appscriptly import user_store
    from appscriptly.http_server import oauth_google_api_callback
    from appscriptly.oauth_google import CALLBACK_PATH
    from appscriptly.oauth_state import sign_state

    user_id = "tampered-state-user"
    # v2.0b: oauth_state.sign_state / verify_state take bytes (matches
    # keys.get_key() return type). Encode str at the test boundary.
    state = sign_state(user_id, SIGNING_KEY.encode("utf-8"))
    # Flip the last character of the signature segment (state shape:
    # sub_b64.nonce.exp.sig). Any single-byte change breaks HMAC.
    parts = state.split(".")
    assert len(parts) == 4, f"state shape changed: {state!r}"
    sig = parts[-1]
    flipped = ("0" if sig[-1] != "0" else "1")
    tampered_state = ".".join(parts[:-1] + [sig[:-1] + flipped])

    app = Starlette(routes=[
        Route(CALLBACK_PATH, oauth_google_api_callback, methods=["GET"]),
    ])

    with patch(
        "appscriptly.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        # If the handler ignores the verify_state failure and proceeds,
        # this mock would supply tokens AND the AssertionError below
        # would fire — defense-in-depth tripwire.
        mk_flow.side_effect = AssertionError(
            "Flow.from_client_config must NOT be called when state HMAC "
            "fails — handler is leaking past the verify gate"
        )
        with TestClient(app) as client:
            response = client.get(
                CALLBACK_PATH,
                params={"state": tampered_state, "code": "FAKE_CODE"},
            )

    # The handler should refuse — 400-class response, NOT a redirect or
    # 200 success page. We don't pin the exact code (handler may render
    # an HTML error page); the contract is "non-2xx and no creds saved".
    assert response.status_code >= 400, (
        f"tampered state was accepted ({response.status_code}); "
        f"body: {response.text[:300]!r}"
    )
    assert user_store.get_state(user_id) == {}, (
        "user_state row exists after tampered-state callback — handler "
        "persisted creds despite HMAC failure. This is the v1.4 OAuth "
        "state-validation bypass class."
    )


# ---------------------------------------------------------------------
# R33 Gap #2 — gdocs_get_signed_upload_url's output is accepted by
#              the production /api/convert endpoint (full mint→verify
#              joint through the MCP-tool layer, not just the crypto
#              primitive that test_api_convert_multitenancy.py uses).
# ---------------------------------------------------------------------


def test_signed_url_from_mcp_tool_roundtrips_through_convert_endpoint():
    """Mint a signed URL via the production MCP tool
    ``gdocs_get_signed_upload_url()`` (which adds the env-var key
    derivation + caller-id binding the unit tests skip), then POST
    a .docx to /api/convert with that URL's query string. If the
    URL the tool emits ever drifts from what BearerTokenMiddleware
    accepts — different param names, different signing base, key-
    derivation mismatch — this test fails."""
    from appscriptly.services.admin.tools import gdocs_get_signed_upload_url

    user_id = "roundtrip-user-A"

    # --- Step 1: mint via the production MCP tool. ---
    # v2.2.2 (Gap #7): gdocs_get_signed_upload_url moved from server.py
    # to services/admin/tools.py; the current_user_id_or_none binding
    # the tool consults lives in that module now.
    with patch(
        "appscriptly.services.admin.tools.current_user_id_or_none",
        return_value=user_id,
    ), patch.dict(
        os.environ, {"PUBLIC_BASE_URL": "http://testserver"},
    ):
        minted = gdocs_get_signed_upload_url()

    # Sanity: the URL the model would copy into the sandbox.
    assert minted["user_id"] == user_id
    parsed = urlparse(minted["url"])
    assert parsed.path == "/api/convert", (
        f"signed URL path drifted: {parsed.path!r}"
    )
    qs = parsed.query

    # --- Step 2: POST a .docx to the production /api/convert. ---
    captured = {}

    def fake_get_creds_for_user(uid, **kwargs):
        captured["per_user_uid"] = uid
        return "per-user-creds-sentinel"

    def fake_convert(creds, **kwargs):
        captured["convert_creds"] = creds
        captured["convert_user_id"] = kwargs.get("user_id")
        return {"doc_id": "DOC_R33", "url": "https://x", "tabs": []}

    app = _build_convert_app()
    client = TestClient(app)

    with patch(
        "appscriptly.http_server.routes.convert.get_credentials_for_user",
        side_effect=fake_get_creds_for_user,
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), patch(
        "appscriptly.http_server.routes.convert._resolve_client_config",
        return_value=_client_config(),
    ):
        resp = client.post(f"/api/convert?{qs}", files=_docx_form())

    assert resp.status_code == 200, (
        f"production-tool-minted URL was rejected by /api/convert "
        f"({resp.status_code}): {resp.text[:300]!r}"
    )
    # The same user_id minted at the MCP-tool layer must flow all the
    # way into _convert_docx — the joint that R33 flagged as untested.
    assert captured["per_user_uid"] == user_id
    assert captured["convert_user_id"] == user_id
    assert captured["convert_creds"] == "per-user-creds-sentinel"


# ---------------------------------------------------------------------
# R33 Gap #3 — Real RefreshError(invalid_grant) at the bottom of the
#              stack surfaces through /api/convert as a clean 401 +
#              auth_url, not a 500. Joint of credentials.py's revoke
#              handling and http_server.py's NeedsReauthError surface.
# ---------------------------------------------------------------------


def test_invalid_grant_at_bottom_surfaces_as_401_with_auth_url_through_convert():
    """Seed a valid-but-expired user row, mint a signed URL for that
    user, POST to /api/convert, and let the production refresh path
    raise a real RefreshError(invalid_grant) — the same error Google
    sends when a user revokes consent.

    The contract: /api/convert returns 401 with body containing the
    user_id and an auth_url that the client can render as a re-consent
    link. Without this joint test, a regression where the resolver
    catches the RefreshError but the endpoint surfaces a 500 ('cleared
    creds successfully — but here's an internal error anyway') would
    only show up in production."""
    from google.auth.exceptions import RefreshError

    from appscriptly import user_store
    from appscriptly.crypto import sign_upload_url

    user_id = "revoked-roundtrip-user"

    # Seed an expired creds row that will trigger the refresh path
    # on the next get_credentials_for_user call. Shape matches what
    # _credentials_from_state in credentials.py deserializes.
    expired_iso = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(hours=2)
    ).isoformat()
    user_store.save_credentials_json(user_id, json.dumps({
        "token": "EXPIRED_ACCESS",
        "refresh_token": "REVOKED_REFRESH",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
        "expiry": expired_iso,
    }))

    # Mint a v2.1 signed URL for that user (using the crypto primitive
    # rather than the MCP tool — we already covered the MCP-tool joint
    # in test #2; here we exercise the revoke→401 joint).
    minted = sign_upload_url(
        base_url="http://testserver/api/convert",
        signing_key=SIGNING_KEY.encode("utf-8"),
        user_id=user_id,
    )
    qs = urlparse(minted["url"]).query

    app = _build_convert_app()
    client = TestClient(app)

    # Patch Credentials.refresh to raise the exact RefreshError shape
    # google_auth raises when Google's /token endpoint returns
    # invalid_grant. credentials.py's resolver must catch this and
    # raise NeedsReauthError, which http_server must surface as 401.
    from google.oauth2.credentials import Credentials

    with patch.object(
        Credentials, "refresh",
        side_effect=RefreshError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant'})"
        ),
    ), patch(
        "appscriptly.http_server.routes.convert._resolve_client_config",
        return_value=_client_config(),
    ):
        resp = client.post(f"/api/convert?{qs}", files=_docx_form())

    assert resp.status_code == 401, (
        f"revoked-token roundtrip expected 401, got {resp.status_code}: "
        f"{resp.text[:300]!r}"
    )
    body = resp.json()
    assert body.get("user_id") == user_id, (
        f"401 body missing user_id: {body!r}"
    )
    assert body.get("auth_url", "").startswith(
        "https://accounts.google.com/"
    ), (
        f"401 body missing or malformed auth_url: {body.get('auth_url')!r}. "
        "Client cannot render re-consent link without it."
    )
    # And the revoked creds row was cleared as a side effect — next
    # tool call lands in the fresh-user branch, not stuck in the
    # revoked branch (the v2.0 invalid_grant cleanup contract).
    assert user_store.get_state(user_id).get("google_creds_json") is None, (
        "revoked user_state row was NOT cleared; the next tool call "
        "would re-trigger the same invalid_grant cycle"
    )
