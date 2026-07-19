"""Multi-tenant /api/convert tests (v2.1 — A1 ship).

Recurring CRITICAL per R3/R10/R12/R13/R19/R21 verification — closes the
v1.x/v2.0 deferral where the REST endpoint always used the operator's
Google credentials regardless of who minted the signed URL.

Guards:
- BearerTokenMiddleware extracts ``uid`` from the validated signed URL
  and stashes ``signed_url_user_id`` on request.state.
- convert_endpoint dispatches per-user (signed URL) vs operator (bearer
  header) creds.
- A signed URL minted for user A cannot be tampered to write into
  user B's Drive (HMAC compare fails after the canonical includes uid).
- Pre-v2.1 signed URLs (no ``uid``) are rejected at the middleware.
- A signed-URL caller whose user_store has no creds gets a clean 401
  with an auth_url for re-authorization — not a 500.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient


_TEST_KEY_BYTES = b"test-signing-key-32-characters-long"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate user_store DB + config dir across tests; inject a fixed
    KeyProvider so all 3 purposes return the same predictable bytes.

    **v2.1.1 (M1a-complete)**: pre-v2.1.1 this fixture set 4 env vars
    (MCP_BEARER_TOKEN + 3 overrides) all to the same value so the test
    client knew exactly what bytes the middleware would compare. The
    ``with_key_provider`` + ``InMemoryKeyProvider`` pattern (introduced
    in PR #88's M1a port) does the same thing without leaking into
    os.environ — and proves the pattern works at the consumer level.
    The InMemoryKeyProvider returns the SAME bytes for all 3 purposes
    so the bearer-header path (which compares the request token to
    keys.get_key("api_bearer")) and the signed-URL path (HMAC over
    keys.get_key("signed_url")) both authenticate with the same value
    the test uses to build the requests.
    """
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "user_state.db"))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver,localhost")
    from appscriptly.key_provider import (
        InMemoryKeyProvider,
        with_key_provider,
    )
    with with_key_provider(InMemoryKeyProvider({
        "api_bearer": _TEST_KEY_BYTES,
        "oauth_state": _TEST_KEY_BYTES,
        "oauth_state_enc": _TEST_KEY_BYTES,
        "signed_url": _TEST_KEY_BYTES,
    })):
        yield


@pytest.fixture(autouse=True)
def reset_nonce_store():
    """Each test starts with a fresh process-global nonce store so
    nonces from prior tests don't collide.

    v2.2.1: the canonical binding lives in ``http_server._state``;
    middleware + oauth route both access it via late binding through
    that module, so reassigning the package-level ``_NONCE_STORE``
    re-export (in ``http_server.__init__``) would NOT propagate.
    """
    from appscriptly import http_server
    from appscriptly.crypto import NonceStore
    from appscriptly.http_server import _state
    fresh = NonceStore()
    _state._NONCE_STORE = fresh
    http_server._NONCE_STORE = fresh  # keep the re-export in sync for any test that reads it
    yield


def _build_app_under_test():
    """Build the production Starlette app with a stub FastMCP — we
    never actually call /mcp so we don't need a real MCP wired."""
    from fastmcp import FastMCP
    from appscriptly.http_server import build_app
    return build_app(FastMCP("stub-for-test"))


def _mint_signed_url_for(user_id: str, *, base="http://testserver/api/convert") -> str:
    from appscriptly.crypto import sign_upload_url
    # v2.1.1: signed-URL HMAC uses the same _TEST_KEY_BYTES that the
    # fixture injected via InMemoryKeyProvider, so the middleware's
    # keys.get_key("signed_url") and this helper agree on the key
    # material without round-tripping through os.environ.
    minted = sign_upload_url(
        base_url=base,
        signing_key=_TEST_KEY_BYTES,
        user_id=user_id,
    )
    return minted["url"]


# ---------------------------------------------------------------------
# Middleware-layer: uid binding propagates to request.state
# ---------------------------------------------------------------------


def test_middleware_stashes_user_id_on_request_state_for_signed_url():
    """The bearer middleware verifies signed URLs and exposes the
    validated user_id to downstream handlers."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from appscriptly.http_server import BearerTokenMiddleware

    async def echo_uid(request):
        return JSONResponse(
            {"uid": getattr(request.state, "signed_url_user_id", None)}
        )

    app = Starlette(
        routes=[Route("/api/echo", echo_uid, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware,
            # v2.1.1: middleware takes bytes-typed keys (matches
            # keys.get_key()'s return type). Use the same _TEST_KEY_BYTES
            # the fixture injected via InMemoryKeyProvider so the
            # middleware authenticates requests built with that key.
            bearer_token=_TEST_KEY_BYTES,
            signed_url_key=_TEST_KEY_BYTES,
        )],
    )
    client = TestClient(app)

    url = _mint_signed_url_for("user-A", base="http://testserver/api/echo")
    qs = urlparse(url).query
    resp = client.get(f"/api/echo?{qs}")
    assert resp.status_code == 200
    assert resp.json()["uid"] == "user-A"


def test_middleware_rejects_pre_v21_signed_url_without_uid():
    """Pre-v2.1 URLs (no ``uid`` param) are rejected at the middleware
    boundary — strict cutoff documented in CHANGELOG."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from appscriptly.http_server import BearerTokenMiddleware

    async def ok(_request):
        return JSONResponse({"reached_handler": True})

    app = Starlette(
        routes=[Route("/api/echo", ok, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware,
            # v2.1.1: middleware takes bytes-typed keys (matches
            # keys.get_key()'s return type). Use the same _TEST_KEY_BYTES
            # the fixture injected via InMemoryKeyProvider so the
            # middleware authenticates requests built with that key.
            bearer_token=_TEST_KEY_BYTES,
            signed_url_key=_TEST_KEY_BYTES,
        )],
    )
    client = TestClient(app)

    # Mint a v2.1 URL and then strip the uid param to simulate a v2.0
    # client. The HMAC was computed over a canonical including uid, so
    # stripping breaks both presence-check AND signature.
    url = _mint_signed_url_for("user-A", base="http://testserver/api/echo")
    qs = parse_qs(urlparse(url).query)
    qs.pop("uid")
    rebuilt = "&".join(f"{k}={v[0]}" for k, v in qs.items())
    resp = client.get(f"/api/echo?{rebuilt}")
    assert resp.status_code == 401
    body = resp.json()
    assert "uid" in body["error"].lower() or "missing" in body["error"].lower()


def test_middleware_rejects_swapped_uid_cross_tenant_attack():
    """An attacker who has user A's signed URL must NOT be able to
    repoint it at user B by swapping the ``uid`` param — HMAC fails."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from appscriptly.http_server import BearerTokenMiddleware

    async def ok(_request):
        return JSONResponse({"reached_handler": True})

    app = Starlette(
        routes=[Route("/api/echo", ok, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware,
            # v2.1.1: middleware takes bytes-typed keys (matches
            # keys.get_key()'s return type). Use the same _TEST_KEY_BYTES
            # the fixture injected via InMemoryKeyProvider so the
            # middleware authenticates requests built with that key.
            bearer_token=_TEST_KEY_BYTES,
            signed_url_key=_TEST_KEY_BYTES,
        )],
    )
    client = TestClient(app)

    url = _mint_signed_url_for("user-A", base="http://testserver/api/echo")
    qs = parse_qs(urlparse(url).query)
    qs["uid"] = ["user-B"]  # the tamper
    rebuilt = "&".join(f"{k}={v[0]}" for k, v in qs.items())
    resp = client.get(f"/api/echo?{rebuilt}")
    assert resp.status_code == 401
    assert "signature mismatch" in resp.json()["error"]


# ---------------------------------------------------------------------
# Endpoint-layer: convert_endpoint dispatches to per-user creds
# ---------------------------------------------------------------------


def _docx_form(content: bytes = b"PK\x03\x04minimaldocx"):
    """Multipart form body for a minimal /api/convert request."""
    return {
        "file": ("test.docx", content,
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    }


def test_convert_endpoint_uses_per_user_creds_for_signed_url_caller():
    """A signed-URL POST must resolve credentials via
    get_credentials_for_user(uid), NOT load_credentials(operator_dir)."""
    app = _build_app_under_test()
    client = TestClient(app)

    url = _mint_signed_url_for("user-A")
    qs = urlparse(url).query

    captured = {}

    def fake_get_creds_for_user(user_id, **kwargs):
        captured["per_user_called_with"] = user_id
        return "per-user-creds-sentinel"

    def fake_load_credentials(*args, **kwargs):
        captured["operator_called"] = True
        raise AssertionError(
            "operator load_credentials must NOT be called for signed-URL caller"
        )

    def fake_convert(creds, **kwargs):
        captured["convert_creds"] = creds
        captured["convert_user_id"] = kwargs.get("user_id")
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.get_credentials_for_user",
        side_effect=fake_get_creds_for_user,
    ), patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        side_effect=fake_load_credentials,
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), patch(
        "appscriptly.http_server.routes.convert._resolve_client_config",
        return_value={"web": {"client_id": "X", "client_secret": "Y"}},
    ):
        resp = client.post(f"/api/convert?{qs}", files=_docx_form())

    assert resp.status_code == 200, resp.text
    assert captured["per_user_called_with"] == "user-A"
    assert "operator_called" not in captured
    assert captured["convert_creds"] == "per-user-creds-sentinel"
    # And the user_id is plumbed all the way into _convert_docx (kept
    # for route-signature stability; the pipeline itself is identified
    # by the per-user creds).
    assert captured["convert_user_id"] == "user-A"


def test_convert_endpoint_returns_401_with_auth_url_on_needs_reauth():
    """A signed-URL caller whose stored Google creds are absent / revoked
    must get a clean 401 with an auth_url, not a 500."""
    from appscriptly.credentials import NeedsReauthError

    app = _build_app_under_test()
    client = TestClient(app)

    url = _mint_signed_url_for("user-A")
    qs = urlparse(url).query

    def raise_needs_reauth(user_id, **kwargs):
        raise NeedsReauthError(
            user_id,
            auth_url="https://accounts.google.com/o/oauth2/v2/auth?xyz",
            reason="Google API credentials not yet authorized",
        )

    with patch(
        "appscriptly.http_server.routes.convert.get_credentials_for_user",
        side_effect=raise_needs_reauth,
    ), patch(
        "appscriptly.http_server.routes.convert._resolve_client_config",
        return_value={"web": {"client_id": "X", "client_secret": "Y"}},
    ):
        resp = client.post(f"/api/convert?{qs}", files=_docx_form())

    assert resp.status_code == 401
    body = resp.json()
    assert body["user_id"] == "user-A"
    assert body["auth_url"].startswith("https://accounts.google.com/")
    assert "credentials" in body["error"].lower()


def test_convert_endpoint_bearer_header_still_uses_operator_creds():
    """Bearer-header callers (legacy / operator smoke tests) bypass the
    signed-URL flow and use operator credentials — unchanged from v2.0."""
    app = _build_app_under_test()
    client = TestClient(app)

    captured = {}

    def fake_load_credentials(*args, **kwargs):
        captured["operator_called"] = True
        return "operator-creds-sentinel"

    def fail_get_per_user(*args, **kwargs):
        raise AssertionError("per-user resolver must NOT be called for bearer-header caller")

    def fake_convert(creds, **kwargs):
        captured["convert_creds"] = creds
        captured["convert_user_id"] = kwargs.get("user_id")
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        side_effect=fake_load_credentials,
    ), patch(
        "appscriptly.http_server.routes.convert.get_credentials_for_user",
        side_effect=fail_get_per_user,
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ):
        resp = client.post(
            "/api/convert",
            files=_docx_form(),
            headers={"authorization": f"Bearer {_TEST_KEY_BYTES.decode('utf-8')}"},
        )

    assert resp.status_code == 200, resp.text
    assert captured.get("operator_called") is True
    assert captured["convert_creds"] == "operator-creds-sentinel"
    # Bearer-header callers pass user_id=None (operator path).
    assert captured["convert_user_id"] is None


# ---------------------------------------------------------------------
# Stdio-mode safety: gdocs_get_signed_upload_url refuses outside auth
# ---------------------------------------------------------------------


def test_gdocs_get_signed_upload_url_refuses_outside_auth_context():
    """The tool must refuse to mint a URL when there's no caller
    identity — minting one would either crash later (no uid) or, worse,
    write into the operator's Drive."""
    from fastmcp.exceptions import ToolError

    from appscriptly.services.admin.tools import gdocs_get_signed_upload_url

    with patch(
        "appscriptly.services.admin.tools.current_user_id_or_none",
        return_value=None,
    ):
        with pytest.raises(ToolError, match="authenticated MCP session"):
            gdocs_get_signed_upload_url()


def test_gdocs_get_signed_upload_url_binds_to_caller_user_id():
    """When called inside an MCP auth context, the minted URL embeds the
    caller's Google sub as ``uid``."""
    from appscriptly.services.admin.tools import gdocs_get_signed_upload_url

    with patch(
        "appscriptly.services.admin.tools.current_user_id_or_none",
        return_value="user-A",
    ):
        result = gdocs_get_signed_upload_url()
    assert result["user_id"] == "user-A"
    qs = parse_qs(urlparse(result["url"]).query)
    assert qs["uid"] == ["user-A"]


# ---------------------------------------------------------------------
# /api/convert — signed-URL max_bytes ENFORCEMENT (dd-apps-maxbytes-enforce)
#
# Previously the signed ``max`` was returned to the caller but never
# enforced (dead contract). These guards prove the convert endpoint now
# rejects an over-cap upload with 413 — both the honestly-declared case
# AND a chunked / Content-Length-omitting POST that bypasses every
# Content-Length check.
# ---------------------------------------------------------------------


def _mint_small_cap_url(user_id="user-A", *, max_bytes, base="http://testserver/api/convert"):
    from appscriptly.crypto import sign_upload_url
    minted = sign_upload_url(
        base_url=base,
        signing_key=_TEST_KEY_BYTES,
        user_id=user_id,
        max_bytes=max_bytes,
    )
    return minted["url"]


def test_convert_rejects_over_cap_upload_413():
    """End-to-end: a signed URL with a small cap rejects a larger .docx
    body with 413 and echoes the cap. (The over-cap multipart body's
    Content-Length trips the cap; whichever layer catches it, the
    contract — 413 + max_bytes — must hold.)"""
    app = _build_app_under_test()
    client = TestClient(app)

    cap = 100
    url = _mint_small_cap_url(max_bytes=cap)
    qs = urlparse(url).query
    big = b"PK\x03\x04" + b"A" * 500  # 504 bytes > cap

    # No creds/convert patching needed: the request is rejected on size
    # before any credential resolution or conversion happens.
    resp = client.post(f"/api/convert?{qs}", files=_docx_form(content=big))
    assert resp.status_code == 413, resp.text
    assert resp.json()["max_bytes"] == cap


def test_convert_allows_under_cap_upload():
    """Regression: enforcement must NOT break the happy path — an
    under-cap upload still converts (200)."""
    app = _build_app_under_test()
    client = TestClient(app)

    cap = 10 * 1024  # 10 KiB
    url = _mint_small_cap_url(max_bytes=cap)
    qs = urlparse(url).query
    small = b"PK\x03\x04minimaldocx"  # well under cap

    captured = {}

    def fake_convert(creds, **kwargs):
        captured["called"] = True
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.get_credentials_for_user",
        return_value="per-user-creds",
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), patch(
        "appscriptly.http_server.routes.convert._resolve_client_config",
        return_value={"web": {"client_id": "X", "client_secret": "Y"}},
    ):
        resp = client.post(f"/api/convert?{qs}", files=_docx_form(content=small))

    assert resp.status_code == 200, resp.text
    assert captured.get("called") is True


def test_convert_bearer_header_path_has_no_signed_cap():
    """Bearer-header callers don't carry a per-URL cap; a large body that
    would exceed a signed cap must still convert (bounded only by
    BodySizeLimitMiddleware / Drive's ceiling, unchanged from before)."""
    app = _build_app_under_test()
    client = TestClient(app)

    captured = {}

    def fake_convert(creds, **kwargs):
        captured["called"] = True
        captured["user_id"] = kwargs.get("user_id")
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        return_value="operator-creds",
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ):
        resp = client.post(
            "/api/convert",
            files=_docx_form(content=b"PK\x03\x04" + b"B" * 4000),
            headers={"authorization": f"Bearer {_TEST_KEY_BYTES.decode('utf-8')}"},
        )

    assert resp.status_code == 200, resp.text
    assert captured.get("called") is True
    # No signed cap → user_id is None (operator path).
    assert captured.get("user_id") is None


# ---------------------------------------------------------------------
# Form validation: nest_by (nested tab split on the signed-URL path)
# ---------------------------------------------------------------------


def test_convert_rejects_invalid_nest_by_value():
    """nest_by only accepts 'heading_2'; anything else is a loud 400
    BEFORE credential resolution or any Drive work (no patches needed:
    reaching creds resolution would 500 on the missing client config)."""
    app = _build_app_under_test()
    client = TestClient(app)
    qs = urlparse(_mint_signed_url_for("user-A")).query

    resp = client.post(
        f"/api/convert?{qs}",
        files=_docx_form(),
        data={"nest_by": "heading_3"},
    )
    assert resp.status_code == 400, resp.text
    assert "nest_by" in resp.json()["error"]
    assert "heading_2" in resp.json()["error"]


def test_convert_rejects_nest_by_without_heading_1_split():
    """nest_by is only valid with split_by='heading_1' — the endpoint
    must refuse the combination rather than silently split flat."""
    app = _build_app_under_test()
    client = TestClient(app)
    qs = urlparse(_mint_signed_url_for("user-A")).query

    resp = client.post(
        f"/api/convert?{qs}",
        files=_docx_form(),
        data={"nest_by": "heading_2", "split_by": "page_break"},
    )
    assert resp.status_code == 400, resp.text
    assert "split_by='heading_1'" in resp.json()["error"]


def test_convert_passes_nest_by_to_pipeline():
    """A valid nest_by rides through to convert_docx_to_tabbed_doc; an
    absent field arrives as None (flat split, today's behavior)."""
    app = _build_app_under_test()
    client = TestClient(app)

    captured = {}

    def fake_convert(creds, **kwargs):
        captured["nest_by"] = kwargs.get("nest_by")
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.get_credentials_for_user",
        return_value="per-user-creds",
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), patch(
        "appscriptly.http_server.routes.convert._resolve_client_config",
        return_value={"web": {"client_id": "X", "client_secret": "Y"}},
    ):
        qs = urlparse(_mint_signed_url_for("user-A")).query
        resp = client.post(
            f"/api/convert?{qs}",
            files=_docx_form(),
            data={"nest_by": "heading_2"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["nest_by"] == "heading_2"

        qs = urlparse(_mint_signed_url_for("user-A")).query
        resp = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert resp.status_code == 200, resp.text
        assert captured["nest_by"] is None


def _call_handler_chunked(handler, *, scope_extra, body_chunks, headers):
    """Call a Starlette route ``handler(request)`` with a CHUNKED request
    body (multiple http.request events, ``more_body`` true until the last)
    and NO ``content-length`` header. Returns ``(status, json_body)``.

    We build the ``Request`` from a hand-rolled scope + ``receive`` and
    await the handler directly, rather than going through Starlette's sync
    ``TestClient`` — TestClient's sync transport deadlocks when streaming a
    generator request body, which is exactly the chunked case under test.
    Awaiting the handler with a chunked ``receive`` faithfully exercises
    ``request.form()`` reading a Transfer-Encoding: chunked upload, then
    the endpoint's post-read size guard, with no deadlock.
    """
    import asyncio
    import json as _json

    from starlette.requests import Request

    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/convert",
        "raw_path": b"/api/convert",
        "query_string": b"",
        "headers": raw_headers,  # deliberately no content-length
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    scope.update(scope_extra)

    # receive() hands out one body chunk per call, all but the last with
    # more_body=True — the ASGI shape of a chunked upload.
    events = [
        {"type": "http.request", "body": c, "more_body": i < len(body_chunks) - 1}
        for i, c in enumerate(body_chunks)
    ]
    events_iter = iter(events)

    async def receive():
        try:
            return next(events_iter)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def _run():
        request = Request(scope, receive)
        response = await handler(request)
        return response.status_code, _json.loads(bytes(response.body).decode("utf-8"))

    return asyncio.run(_run())


def test_convert_endpoint_post_read_guard_catches_chunked_bypass():
    """The endpoint's AUTHORITATIVE guard: a chunked / Content-Length-
    omitting POST bypasses every Content-Length check, so the only place
    the true size is known is AFTER the bytes are read. We stash a small
    cap on the ASGI scope's ``state`` (exactly what BearerTokenMiddleware
    does post-verify) and drive a chunked body that exceeds it."""
    from appscriptly.http_server.routes.convert import convert_endpoint

    CAP = 100
    boundary = "----maxbytesboundary"
    headers = [
        ("content-type", f"multipart/form-data; boundary={boundary}"),
        ("host", "testserver"),
    ]

    def _multipart(file_bytes: bytes, filename: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    # Over-cap: the decoded .docx part is 504 bytes > CAP=100. Split the
    # multipart body across TWO http.request events (more_body=True on the
    # first) to exercise the streaming read path with NO Content-Length —
    # the exact bypass the post-read guard exists to close. The request is
    # rejected on size BEFORE any credential resolution or conversion, so
    # no mocking is needed.
    over = _multipart(b"PK\x03\x04" + b"C" * 500, "big.docx")
    mid = len(over) // 2
    status, body = _call_handler_chunked(
        convert_endpoint,
        scope_extra={"state": {"signed_url_max_bytes": CAP}},
        body_chunks=[over[:mid], over[mid:]],
        headers=headers,
    )
    assert status == 413, body
    assert body["max_bytes"] == CAP
    # (The size-conditional happy path — an UNDER-cap upload still
    # converting — is covered by ``test_convert_allows_under_cap_upload``
    # via the full app + TestClient; we don't re-drive the conversion
    # pipeline through the direct-call harness here.)


# ---------------------------------------------------------------------
# gdocs_get_signed_upload_url — max_bytes input validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_max", [0, -1, 100 * 1024 * 1024 + 1, True])
def test_gdocs_get_signed_upload_url_rejects_out_of_range_max_bytes(bad_max):
    """The tool rejects an out-of-range cap with a ToolError (not a raw
    ValueError) so the connector UI renders it cleanly."""
    from fastmcp.exceptions import ToolError

    from appscriptly.services.admin.tools import gdocs_get_signed_upload_url

    with patch(
        "appscriptly.services.admin.tools.current_user_id_or_none",
        return_value="user-A",
    ):
        with pytest.raises(ToolError, match="max_bytes"):
            gdocs_get_signed_upload_url(max_bytes=bad_max)


def test_gdocs_get_signed_upload_url_default_max_bytes_unchanged():
    """VERIFY-LAST guard: the default cap stays 50 MiB — no tool-surface
    change. (The default flows into the input schema; a drift here would
    silently change the tool contract.)"""
    from appscriptly.crypto import DEFAULT_MAX_BYTES
    from appscriptly.services.admin.tools import gdocs_get_signed_upload_url

    assert DEFAULT_MAX_BYTES == 50 * 1024 * 1024
    with patch(
        "appscriptly.services.admin.tools.current_user_id_or_none",
        return_value="user-A",
    ):
        result = gdocs_get_signed_upload_url()
    assert result["max_bytes"] == 50 * 1024 * 1024
