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
import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate user_store DB + config dir across tests."""
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "user_state.db"))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-signing-key-32-characters-long")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver,localhost")
    yield


@pytest.fixture(autouse=True)
def reset_nonce_store():
    """Each test starts with a fresh process-global nonce store so
    nonces from prior tests don't collide."""
    from google_docs_mcp import http_server
    from google_docs_mcp.crypto import NonceStore
    http_server._NONCE_STORE = NonceStore()
    yield


def _build_app_under_test():
    """Build the production Starlette app with a stub FastMCP — we
    never actually call /mcp so we don't need a real MCP wired."""
    from fastmcp import FastMCP
    from google_docs_mcp.http_server import build_app
    return build_app(FastMCP("stub-for-test"))


def _mint_signed_url_for(user_id: str, *, base="http://testserver/api/convert") -> str:
    from google_docs_mcp.crypto import sign_upload_url
    minted = sign_upload_url(
        base_url=base,
        signing_key=os.environ["MCP_BEARER_TOKEN"],
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

    from google_docs_mcp.http_server import BearerTokenMiddleware

    async def echo_uid(request):
        return JSONResponse(
            {"uid": getattr(request.state, "signed_url_user_id", None)}
        )

    app = Starlette(
        routes=[Route("/api/echo", echo_uid, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware, token=os.environ["MCP_BEARER_TOKEN"],
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

    from google_docs_mcp.http_server import BearerTokenMiddleware

    async def ok(_request):
        return JSONResponse({"reached_handler": True})

    app = Starlette(
        routes=[Route("/api/echo", ok, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware, token=os.environ["MCP_BEARER_TOKEN"],
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

    from google_docs_mcp.http_server import BearerTokenMiddleware

    async def ok(_request):
        return JSONResponse({"reached_handler": True})

    app = Starlette(
        routes=[Route("/api/echo", ok, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware, token=os.environ["MCP_BEARER_TOKEN"],
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
        "google_docs_mcp.http_server.get_credentials_for_user",
        side_effect=fake_get_creds_for_user,
    ), patch(
        "google_docs_mcp.http_server.load_credentials",
        side_effect=fake_load_credentials,
    ), patch(
        "google_docs_mcp.http_server._convert_docx",
        side_effect=fake_convert,
    ), patch(
        "google_docs_mcp.http_server._resolve_client_config",
        return_value={"web": {"client_id": "X", "client_secret": "Y"}},
    ):
        resp = client.post(f"/api/convert?{qs}", files=_docx_form())

    assert resp.status_code == 200, resp.text
    assert captured["per_user_called_with"] == "user-A"
    assert "operator_called" not in captured
    assert captured["convert_creds"] == "per-user-creds-sentinel"
    # And the user_id is plumbed all the way into _convert_docx so
    # _resolve_webapp_url can pick the right tenant's Apps Script URL.
    assert captured["convert_user_id"] == "user-A"


def test_convert_endpoint_returns_401_with_auth_url_on_needs_reauth():
    """A signed-URL caller whose stored Google creds are absent / revoked
    must get a clean 401 with an auth_url, not a 500."""
    from google_docs_mcp.credentials import NeedsReauthError

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
        "google_docs_mcp.http_server.get_credentials_for_user",
        side_effect=raise_needs_reauth,
    ), patch(
        "google_docs_mcp.http_server._resolve_client_config",
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
        "google_docs_mcp.http_server.load_credentials",
        side_effect=fake_load_credentials,
    ), patch(
        "google_docs_mcp.http_server.get_credentials_for_user",
        side_effect=fail_get_per_user,
    ), patch(
        "google_docs_mcp.http_server._convert_docx",
        side_effect=fake_convert,
    ):
        resp = client.post(
            "/api/convert",
            files=_docx_form(),
            headers={"authorization": f"Bearer {os.environ['MCP_BEARER_TOKEN']}"},
        )

    assert resp.status_code == 200, resp.text
    assert captured.get("operator_called") is True
    assert captured["convert_creds"] == "operator-creds-sentinel"
    # Bearer-header callers pass user_id=None — _resolve_webapp_url
    # then falls through to operator config (intended).
    assert captured["convert_user_id"] is None


# ---------------------------------------------------------------------
# _resolve_webapp_url tenant routing (was R3 finding A4)
# ---------------------------------------------------------------------


def test_resolve_webapp_url_routes_to_explicit_user_id():
    """When called with explicit user_id (REST path), pick that user's
    apps_script_url from user_store — NOT current_user_id_or_none()'s
    answer and NOT the operator's local config."""
    from google_docs_mcp import user_store
    from google_docs_mcp.docx_import import _resolve_webapp_url

    user_store.save_state(
        "user-A",
        {"apps_script_url": "https://script.google.com/macros/s/USER_A_DEPLOY/exec"},
    )
    user_store.save_state(
        "user-B",
        {"apps_script_url": "https://script.google.com/macros/s/USER_B_DEPLOY/exec"},
    )

    # Explicit user_id wins over current_user_id_or_none.
    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none",
        return_value="user-B",
    ):
        resolved = _resolve_webapp_url(user_id="user-A")
    assert "USER_A_DEPLOY" in resolved


def test_resolve_webapp_url_falls_back_to_mcp_context_when_no_user_id():
    """Without explicit user_id, use current_user_id_or_none — the MCP
    tool path (HTTP mode)."""
    from google_docs_mcp import user_store
    from google_docs_mcp.docx_import import _resolve_webapp_url

    user_store.save_state(
        "user-B",
        {"apps_script_url": "https://script.google.com/macros/s/USER_B_DEPLOY/exec"},
    )

    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none",
        return_value="user-B",
    ):
        resolved = _resolve_webapp_url(user_id=None)
    assert "USER_B_DEPLOY" in resolved


def test_resolve_webapp_url_falls_back_to_operator_config_outside_auth():
    """No explicit user_id AND no MCP auth context — fall through to
    operator's local config (stdio mode)."""
    from google_docs_mcp.docx_import import _resolve_webapp_url

    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none",
        return_value=None,
    ), patch(
        "google_docs_mcp.docx_import.get_webapp_url",
        return_value="https://script.google.com/macros/s/OPERATOR_DEPLOY/exec",
    ):
        resolved = _resolve_webapp_url()
    assert "OPERATOR_DEPLOY" in resolved


# ---------------------------------------------------------------------
# Stdio-mode safety: gdocs_get_signed_upload_url refuses outside auth
# ---------------------------------------------------------------------


def test_gdocs_get_signed_upload_url_refuses_outside_auth_context():
    """The tool must refuse to mint a URL when there's no caller
    identity — minting one would either crash later (no uid) or, worse,
    write into the operator's Drive."""
    from fastmcp.exceptions import ToolError

    from google_docs_mcp.server import gdocs_get_signed_upload_url

    with patch(
        "google_docs_mcp.server.current_user_id_or_none",
        return_value=None,
    ):
        with pytest.raises(ToolError, match="authenticated MCP session"):
            gdocs_get_signed_upload_url()


def test_gdocs_get_signed_upload_url_binds_to_caller_user_id():
    """When called inside an MCP auth context, the minted URL embeds the
    caller's Google sub as ``uid``."""
    from google_docs_mcp.server import gdocs_get_signed_upload_url

    with patch(
        "google_docs_mcp.server.current_user_id_or_none",
        return_value="user-A",
    ):
        result = gdocs_get_signed_upload_url()
    assert result["user_id"] == "user-A"
    qs = parse_qs(urlparse(result["url"]).query)
    assert qs["uid"] == ["user-A"]
