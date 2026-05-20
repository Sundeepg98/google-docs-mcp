"""HTTP middleware tests (v1.3.1).

Guards:
- ``derive_trusted_hosts`` priority order: TRUSTED_HOSTS env > FLY_APP_NAME
  derivation > fail-open with WARN
- Refuses to start when FLY_REGION is set without FLY_APP_NAME (the R20
  silent fail-open path)
- BodySizeLimitMiddleware returns 413 on oversize Content-Length, 400
  on malformed Content-Length, passes small bodies through
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


# ---------------------------------------------------------------------
# derive_trusted_hosts priority + fail-modes
# ---------------------------------------------------------------------


def test_explicit_trusted_hosts_env_wins(monkeypatch):
    monkeypatch.setenv("TRUSTED_HOSTS", "a.example.com,b.example.com")
    monkeypatch.setenv("FLY_APP_NAME", "should-be-ignored")
    monkeypatch.delenv("FLY_REGION", raising=False)

    from google_docs_mcp.http_server import derive_trusted_hosts
    result = derive_trusted_hosts()
    assert result == ["a.example.com", "b.example.com"]


def test_fly_app_name_derivation(monkeypatch):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("FLY_APP_NAME", "my-app")
    monkeypatch.delenv("FLY_REGION", raising=False)

    from google_docs_mcp.http_server import derive_trusted_hosts
    result = derive_trusted_hosts()
    assert "my-app.fly.dev" in result
    assert "*.my-app.fly.dev" in result
    assert "localhost" in result


def test_derive_trusted_hosts_includes_fly_internal(monkeypatch):
    """v2.0.6 deploy-blocker fix: Fly's internal health probe sends a
    Host header that doesn't match <app>.fly.dev. Without these entries
    the probe is rejected with 400 BEFORE reaching the /health handler,
    the deploy health gate fails, and Fly aborts the deploy.

    Regression guard for the v78-v82 incident (5 consecutive aborted
    deploys). See derive_trusted_hosts docstring for the full context.
    """
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("FLY_APP_NAME", "my-app")
    monkeypatch.delenv("FLY_REGION", raising=False)

    from google_docs_mcp.http_server import derive_trusted_hosts
    result = derive_trusted_hosts()
    # The two Fly-internal-probe entries:
    assert "my-app.internal" in result, (
        f"Fly internal hostname <app>.internal missing; deploys will "
        f"fail TrustedHost on internal probes. Got: {result!r}"
    )
    assert "*.internal" in result, (
        f"Fly machine-id.vm.<app>.internal pattern missing; deploys "
        f"will fail TrustedHost on per-machine probes. Got: {result!r}"
    )


def test_fail_open_with_warn_when_neither_env_set(monkeypatch, caplog):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("FLY_REGION", raising=False)

    from google_docs_mcp.http_server import derive_trusted_hosts
    with caplog.at_level("WARNING"):
        result = derive_trusted_hosts()
    assert result == ["*"]
    assert any("fail-open" in rec.message for rec in caplog.records)


def test_refuses_startup_on_fly_region_without_fly_app_name(monkeypatch):
    """R20 attack #2 mitigation: refuse to fail-open on Fly infra."""
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.setenv("FLY_REGION", "iad")  # presence of FLY_REGION = "on Fly"

    from google_docs_mcp.http_server import derive_trusted_hosts
    with pytest.raises(RuntimeError, match="FLY_REGION"):
        derive_trusted_hosts()


def test_explicit_trusted_hosts_works_even_on_fly(monkeypatch):
    """Explicit TRUSTED_HOSTS must override the FLY_REGION assertion."""
    monkeypatch.setenv("TRUSTED_HOSTS", "explicit.fly.dev")
    monkeypatch.setenv("FLY_REGION", "iad")
    monkeypatch.delenv("FLY_APP_NAME", raising=False)

    from google_docs_mcp.http_server import derive_trusted_hosts
    # The explicit override path returns BEFORE the FLY_REGION assertion.
    result = derive_trusted_hosts()
    assert result == ["explicit.fly.dev"]


# ---------------------------------------------------------------------
# BodySizeLimitMiddleware
# ---------------------------------------------------------------------


def _build_test_app(max_bytes: int = 10):
    """A minimal Starlette app with only BodySizeLimitMiddleware wired."""
    from google_docs_mcp.http_server import BodySizeLimitMiddleware

    async def echo(_request):
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[Route("/echo", echo, methods=["POST"])],
        middleware=[Middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)],
    )


def test_body_size_413_on_oversize_content_length():
    app = _build_test_app(max_bytes=10)
    client = TestClient(app)
    resp = client.post("/echo", content=b"x" * 100)
    assert resp.status_code == 413
    body = resp.json()
    assert "max_bytes" in body


def test_body_size_400_on_invalid_content_length():
    app = _build_test_app(max_bytes=10)
    client = TestClient(app)
    resp = client.post(
        "/echo", content=b"hi",
        headers={"content-length": "not-a-number"},
    )
    assert resp.status_code == 400
    assert "invalid Content-Length" in resp.text


def test_body_size_passes_small_payload():
    app = _build_test_app(max_bytes=10_000)
    client = TestClient(app)
    resp = client.post("/echo", content=b"hello")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------
# OAuth callback HTML escaping (reflected XSS prevention, v2.0.5)
# ---------------------------------------------------------------------


def test_error_page_escapes_html_metachars():
    """Reflected XSS prevention: _error_page must escape HTML metachars."""
    from google_docs_mcp.http_server import _error_page
    resp = _error_page("<script>alert(1)</script>", 400)
    body = resp.body.decode("utf-8")
    assert "&lt;script&gt;" in body
    assert "<script>" not in body
    assert "alert(1)" in body  # escaped form still readable


# ---------------------------------------------------------------------
# OAuth callback CSP header (defense-in-depth on top of XSS fix, v2.0.6)
# ---------------------------------------------------------------------


def test_oauth_error_page_includes_csp_header():
    """Defense-in-depth: if a future edit forgets to escape the body
    substitution, CSP must block the injected script from loading."""
    from google_docs_mcp.http_server import _error_page
    resp = _error_page("test", 400)
    csp = resp.headers.get("content-security-policy", "")
    assert csp, "expected Content-Security-Policy header on _error_page"
    assert "default-src 'none'" in csp, (
        f"CSP must lock down default-src; got {csp!r}"
    )
    # No script-src directive at all (template has no <script> tags).
    # Absence is stricter than `script-src 'none'` because UA falls back
    # to default-src, which is already 'none'.
    assert "script-src" not in csp, (
        f"CSP must NOT permit any script source; got {csp!r}"
    )


def test_oauth_success_page_includes_csp_header():
    """Same defense-in-depth on the success page — even though the
    success page's body is server-controlled and not user-influenced
    today, future edits could change that."""
    from google_docs_mcp.http_server import _success_page
    resp = _success_page()
    csp = resp.headers.get("content-security-policy", "")
    assert csp, "expected Content-Security-Policy header on _success_page"
    assert "default-src 'none'" in csp
    assert "script-src" not in csp


def test_oauth_pages_csp_allows_inline_style():
    """The _OAUTH_SUCCESS_HTML template carries an inline <style> block;
    CSP must permit it via style-src 'unsafe-inline' or the page renders
    unstyled. Regression guard against an over-aggressive future CSP edit."""
    from google_docs_mcp.http_server import _error_page, _success_page
    for resp in (_error_page("x", 400), _success_page()):
        csp = resp.headers["content-security-policy"]
        assert "style-src 'unsafe-inline'" in csp, (
            f"inline <style> requires style-src 'unsafe-inline'; got {csp!r}"
        )
