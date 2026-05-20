"""Fresh-user OAuth + first-tool-call integration (v1.4.0b).

End-to-end-ish coverage of the path a brand-new claude.ai user takes
on first contact with our cloud MCP:

  1. Tool call detects no creds -> ``NeedsReauthError`` carrying an
     ``auth_url`` the user clicks.
  2. User finishes Google consent; Google redirects back to
     ``/oauth/google/api/callback`` with ``code`` + ``state``.
  3. ``exchange_code_for_credentials`` validates state, mocks the
     token-exchange (no real Google call), persists creds via
     ``save_credentials_json``.
  4. The next ``get_credentials_for_user`` call returns a fresh,
     refresh-token-bearing ``Credentials`` object.

Google's token endpoint is mocked at the ``Flow.from_client_config``
boundary (same approach as ``test_oauth_google.py``) — the underlying
``requests_oauthlib`` machinery uses a synchronous ``requests``
transport, so an httpx-level mock wouldn't catch it. We test the same
boundary the unit tests do, but stitched together with ``user_store``
+ ``credentials.get_credentials_for_user`` so a regression in any one
link surfaces here.

Why integration-grade vs unit: each link has its own unit test, but
the unit tests don't notice when the SHAPE of the data passed
between them drifts (e.g. ``save_credentials_json`` strips a key
that ``_credentials_from_state`` later needs). This is the joint.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest


@pytest.fixture(autouse=True)
def isolated_user_store(tmp_path, monkeypatch):
    """Per-test SQLite file + data dir so user_state doesn't bleed."""
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    yield db_file


@pytest.fixture
def client_config():
    """Operator-level OAuth client config — same shape as production env."""
    return {
        "web": {
            "client_id": "TEST_CLIENT_ID.apps.googleusercontent.com",
            "client_secret": "TEST_CLIENT_SECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [
                "https://example.fly.dev/oauth/google/api/callback"
            ],
        },
    }


@pytest.fixture
def signing_key():
    # v2.0b (PR #34 Option A.1): oauth_state.sign_state / verify_state
    # take signing_key as bytes (matches keys.get_key("oauth_state")'s
    # return type post-HKDF-strict-flip). The unit-test fixtures
    # (test_oauth_state.py, test_oauth_google.py) were swept in A.1;
    # this integration fixture was missed and silently failed every
    # test in this file with TypeError from hmac.new(). Fixed in
    # follow-up after R34 investigation.
    return b"test-signing-key-fresh-user-flow"


@pytest.fixture
def base_url():
    return "https://example.fly.dev"


@pytest.fixture
def nonce_store():
    from google_docs_mcp.crypto import NonceStore
    return NonceStore()


def _mock_flow_returning(refresh_token: str, access_token: str, scopes: list[str]):
    """Stand-in for ``google_auth_oauthlib.flow.Flow`` after fetch_token.

    Mirrors the shape ``exchange_code_for_credentials`` reads off the
    real Flow object. Includes a non-empty refresh_token (the contract
    requires it) and a future-dated expiry so downstream
    ``Credentials.valid`` evaluates True.
    """
    flow = MagicMock()
    creds = MagicMock()
    creds.refresh_token = refresh_token
    creds.to_json.return_value = json.dumps(
        {
            "token": access_token,
            "refresh_token": refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "TEST_CLIENT_ID.apps.googleusercontent.com",
            "client_secret": "TEST_CLIENT_SECRET",
            "scopes": scopes,
            # 1h-from-now expiry. The Credentials class deserializes
            # this via fromisoformat in _credentials_from_state.
            "expiry": (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=1)
            ).isoformat(),
        }
    )
    flow.credentials = creds
    return flow


# ----------------------------------------------------------------------
# Step 1: no creds yet -> tool path raises NeedsReauthError with auth_url
# ----------------------------------------------------------------------


def test_first_tool_call_with_no_creds_raises_needs_reauth(
    client_config, signing_key, base_url
):
    """Fresh user with no row in user_store: the credentials resolver must
    raise ``NeedsReauthError`` carrying a clickable auth_url, not a
    generic KeyError or 500."""
    from google_docs_mcp.credentials import (
        NeedsReauthError, get_credentials_for_user,
    )

    with pytest.raises(NeedsReauthError) as exc:
        get_credentials_for_user(
            "fresh-user-sub-001",
            client_config=client_config,
            signing_key=signing_key,
            base_url=base_url,
        )

    assert exc.value.user_id == "fresh-user-sub-001"
    assert exc.value.auth_url.startswith(
        "https://accounts.google.com/o/oauth2/auth"
    ), f"auth_url should point at Google consent, got: {exc.value.auth_url}"
    # The auth_url must include a signed state binding this user_id —
    # otherwise the callback can't tell which user is coming back.
    qs = parse_qs(urlparse(exc.value.auth_url).query)
    assert "state" in qs and qs["state"][0].count(".") == 3, (
        "auth_url is missing the signed state token "
        "(expected 4 dot-separated parts: sub_b64.nonce.exp.sig)"
    )


# ----------------------------------------------------------------------
# Step 2-4: complete the OAuth dance and verify creds resolve cleanly
# ----------------------------------------------------------------------


def test_fresh_user_full_oauth_dance_persists_usable_creds(
    client_config, signing_key, base_url, nonce_store
):
    """The big one: drive a fresh user from no-creds to usable creds via
    the same code paths the production HTTP server calls.

    Mocks Google's token endpoint at the Flow.from_client_config
    boundary (same approach as test_oauth_google.py — the lower-level
    oauthlib machinery uses the synchronous requests transport, so
    we patch one layer up).
    """
    from google_docs_mcp import user_store
    from google_docs_mcp.credentials import (
        NeedsReauthError, get_credentials_for_user,
    )
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, exchange_code_for_credentials,
    )

    user_id = "fresh-user-sub-002"

    # --- Step 1: tool call -> NeedsReauthError with auth_url. ---
    with pytest.raises(NeedsReauthError) as exc:
        get_credentials_for_user(
            user_id,
            client_config=client_config,
            signing_key=signing_key,
            base_url=base_url,
        )
    auth_url = exc.value.auth_url

    # --- Step 2: extract the state Google would echo back. ---
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    callback_url = (
        f"{base_url}/oauth/google/api/callback"
        f"?state={state}&code=FAKE_AUTH_CODE_FROM_GOOGLE"
    )

    # --- Step 3: callback runs exchange (Flow.fetch_token mocked). ---
    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        mk_flow.return_value = _mock_flow_returning(
            refresh_token="REFRESH_FROM_GOOGLE",
            access_token="ACCESS_FROM_GOOGLE",
            scopes=GOOGLE_API_SCOPES,
        )
        returned_uid, creds_json = exchange_code_for_credentials(
            state=state,
            authorization_response_url=callback_url,
            base_url=base_url,
            client_config=client_config,
            signing_key=signing_key,
            nonce_store=nonce_store,
        )

    assert returned_uid == user_id, (
        "state's user_id MUST flow through to the callback unchanged — "
        "otherwise we'd save creds under the wrong key"
    )

    # The callback handler is responsible for persistence; simulate it.
    # ``save_credentials_json`` strips operator secrets — that's tested
    # separately, but covered here as part of the joint contract.
    user_store.save_credentials_json(returned_uid, creds_json)

    # --- Step 4: a subsequent tool call MUST find the creds. ---
    creds = get_credentials_for_user(
        user_id,
        client_config=client_config,
        signing_key=signing_key,
        base_url=base_url,
    )
    assert creds.token == "ACCESS_FROM_GOOGLE"
    assert creds.refresh_token == "REFRESH_FROM_GOOGLE"
    # The operator's client_id/client_secret are injected at load time,
    # NOT read from the persisted JSON (security property).
    assert creds.client_id == "TEST_CLIENT_ID.apps.googleusercontent.com"
    assert creds.client_secret == "TEST_CLIENT_SECRET"


def test_fresh_user_persisted_state_strips_operator_secrets(
    client_config, signing_key, base_url, nonce_store
):
    """Regression guard: ``save_credentials_json`` must never persist the
    operator's ``client_id``/``client_secret`` to the per-user row.

    A user_state.db leak with operator secrets baked in lets an attacker
    impersonate the entire OAuth app to Google — way worse than a
    single user's refresh_token leaking.
    """
    from google_docs_mcp import user_store
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, build_authorization_url,
        exchange_code_for_credentials,
    )

    user_id = "fresh-user-sub-003"
    auth_url = build_authorization_url(
        user_id, base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        mk_flow.return_value = _mock_flow_returning(
            refresh_token="R", access_token="A", scopes=GOOGLE_API_SCOPES,
        )
        _, creds_json = exchange_code_for_credentials(
            state=state,
            authorization_response_url=(
                f"{base_url}/oauth/google/api/callback?state={state}&code=X"
            ),
            base_url=base_url,
            client_config=client_config,
            signing_key=signing_key,
            nonce_store=nonce_store,
        )

    user_store.save_credentials_json(user_id, creds_json)
    raw = user_store.get_state(user_id)["google_creds_json"]
    persisted = json.loads(raw)

    assert "client_id" not in persisted, (
        "operator client_id leaked into per-user persisted JSON — "
        "see user_store.save_credentials_json"
    )
    assert "client_secret" not in persisted, (
        "operator client_secret leaked into per-user persisted JSON — "
        "see user_store.save_credentials_json"
    )
    # The per-user secrets MUST still be there.
    assert persisted["refresh_token"] == "R"
    assert persisted["token"] == "A"


def test_oauth_callback_endpoint_strips_operator_secrets_in_production(
    client_config, signing_key, base_url, monkeypatch,
):
    """End-to-end guard against the v2.0.3 regression.

    The existing ``test_fresh_user_persisted_state_strips_operator_secrets``
    above exercises ``save_credentials_json`` in isolation -- but the
    production OAuth callback in ``http_server.py`` could (and did,
    until v2.0.3) call ``save_state`` directly, bypassing the wrapper
    that strips operator ``client_id`` / ``client_secret``. The isolated
    test still passed while operator OAuth-app secrets leaked into every
    user row.

    This test drives the ACTUAL Starlette callback route via TestClient:
    sign a state token, mock ``Flow.from_client_config`` so the
    code-for-token exchange returns a creds JSON with operator secrets
    populated, GET the callback endpoint, then read the persisted row
    directly from the SQLite store. If the route ever again calls
    ``save_state`` instead of ``save_credentials_json``, this test
    fails -- ``client_id`` / ``client_secret`` will be in the persisted
    JSON.
    """
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from google_docs_mcp import user_store
    from google_docs_mcp.http_server import (
        _NONCE_STORE, oauth_google_api_callback,
    )
    from google_docs_mcp.oauth_google import CALLBACK_PATH, GOOGLE_API_SCOPES
    from google_docs_mcp.oauth_state import sign_state

    user_id = "callback-e2e-user"

    # The handler reads MCP_BEARER_TOKEN (as the state signing key) and
    # GOOGLE_OAUTH_CLIENT_SECRETS_JSON (as the operator OAuth client
    # config) from env. Match the values our handcrafted state and the
    # mocked Flow expect.
    monkeypatch.setenv("MCP_BEARER_TOKEN", signing_key)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRETS_JSON", json.dumps(client_config))
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", base_url)

    # Sign a state token directly so we control both nonce + user_id.
    # The handler's _NONCE_STORE is module-level; it has not seen this
    # nonce, so consume() will succeed on first call. No PKCE verifier
    # for this test -- the mocked Flow does not exercise PKCE.
    state = sign_state(user_id, signing_key)

    # Build a minimal Starlette app with ONLY the callback route. Avoids
    # spinning up FastMCP + bearer middleware + trusted-host middleware
    # for a single-route test.
    app = Starlette(
        routes=[Route(CALLBACK_PATH, oauth_google_api_callback, methods=["GET"])],
    )

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        # The mock returns creds whose to_json() carries the operator's
        # client_id / client_secret -- exactly what Google's real
        # google_auth_oauthlib returns. If the production callback
        # persists this verbatim, those operator secrets land in
        # user_state.db.
        mk_flow.return_value = _mock_flow_returning(
            refresh_token="CALLBACK_R",
            access_token="CALLBACK_A",
            scopes=GOOGLE_API_SCOPES,
        )

        with TestClient(app) as client:
            response = client.get(
                CALLBACK_PATH,
                params={"state": state, "code": "FAKE_AUTH_CODE"},
            )

    # The handler renders an HTML success page on the happy path.
    assert response.status_code == 200, (
        f"callback returned {response.status_code}; body: {response.text[:500]!r}"
    )

    # Read the persisted row directly via the user_store facade. The
    # invariant under test is purely about the SHAPE of the persisted
    # google_creds_json blob -- operator secrets MUST be absent.
    persisted_state = user_store.get_state(user_id)
    assert "google_creds_json" in persisted_state, (
        "callback did not persist any creds for the user -- the route "
        "must have errored silently before the save"
    )
    persisted_creds = json.loads(persisted_state["google_creds_json"])

    assert "client_secret" not in persisted_creds, (
        "operator client_secret leaked into user_state.db via the OAuth "
        "callback route. The handler in http_server.py must call "
        "user_store.save_credentials_json (which strips operator secrets) "
        "and NOT user_store.save_state directly. Without this, a "
        "user_state.db leak also hands the attacker the credentials to "
        "impersonate the entire OAuth app to Google."
    )
    assert "client_id" not in persisted_creds, (
        "operator client_id leaked into user_state.db via the OAuth "
        "callback route -- same root cause as the client_secret leak above"
    )

    # The per-user credentials -- token + refresh_token -- MUST still be
    # there (we are stripping the OPERATOR secrets, not the user's
    # tokens).
    assert persisted_creds["refresh_token"] == "CALLBACK_R"
    assert persisted_creds["token"] == "CALLBACK_A"


def test_replayed_callback_state_cannot_overwrite_existing_creds(
    client_config, signing_key, base_url, nonce_store
):
    """An attacker who captures a victim's state token (browser history /
    access logs) MUST NOT be able to re-use it to plant alternate creds
    under the victim's user_id. The single-use NonceStore enforces this.

    The threat model is documented in oauth_state.py's module docstring;
    this test makes sure the property is enforced END-TO-END (not just
    in oauth_state.consume()).
    """
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, OAuthCallbackError, build_authorization_url,
        exchange_code_for_credentials,
    )

    auth_url = build_authorization_url(
        "victim-sub", base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        mk_flow.return_value = _mock_flow_returning(
            refresh_token="VICTIM_R", access_token="VICTIM_A",
            scopes=GOOGLE_API_SCOPES,
        )
        # First redemption (legitimate victim) succeeds.
        exchange_code_for_credentials(
            state=state,
            authorization_response_url=(
                f"{base_url}/oauth/google/api/callback?state={state}&code=X"
            ),
            base_url=base_url, client_config=client_config,
            signing_key=signing_key, nonce_store=nonce_store,
        )
        # Second redemption (attacker replay) must fail loudly — the
        # NonceStore consumed the nonce on first use.
        with pytest.raises(OAuthCallbackError, match="state could not be validated"):
            exchange_code_for_credentials(
                state=state,
                authorization_response_url=(
                    f"{base_url}/oauth/google/api/callback?state={state}&code=Y"
                ),
                base_url=base_url, client_config=client_config,
                signing_key=signing_key, nonce_store=nonce_store,
            )
