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


# ---------------------------------------------------------------------
# HealthExemptTrustedHostMiddleware (v2.3.3 — Fly internal probe fix)
# ---------------------------------------------------------------------
#
# Regression context: PR #77 (v2.0.6) added `<app>.internal` and
# `*.internal` to the TrustedHost allowlist to make Fly deploy-gate
# probes pass. v89 (deployed via PR #122's revived free-builder)
# surfaced fresh `400 Bad Request` from probes at
# `172.19.24.105:*` — a raw-IP Host header that the hostname
# allowlist can't match. Fly's probe IPs rotate per deploy, so
# pinning specific IPs is unmaintainable.
#
# Option B (per the v2.3.3 brief): exempt /health from TrustedHost
# entirely. Health endpoints are infra probes by convention; gating
# them on Host validation breaks the gate's primary use case.
# ---------------------------------------------------------------------


def _build_app_with_health_exempt(allowed_hosts):
    """A minimal Starlette app wired only with the new health-exempt
    TrustedHost middleware, plus a /health route and an /other route
    so we can prove the exemption is route-scoped."""
    from google_docs_mcp.http_server import HealthExemptTrustedHostMiddleware

    async def health(_request):
        return JSONResponse({"ok": True, "service": "google-docs-mcp"})

    async def other(_request):
        return JSONResponse({"other": True})

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/other", other, methods=["GET"]),
        ],
        middleware=[
            Middleware(
                HealthExemptTrustedHostMiddleware,
                allowed_hosts=allowed_hosts,
            ),
        ],
    )


def test_health_accepts_fly_internal_probe_with_raw_ip_host():
    """Regression: PR #77's hostname-allowlist fix did NOT cover Fly's
    raw-IP probe path. v89 deploy logs (2026-05-27):

        172.19.24.105:46588 → GET /health → 400 Bad Request

    The HealthExemptTrustedHostMiddleware bypasses Host validation
    on /health so probes with any Host header (including raw IPs)
    succeed."""
    app = _build_app_with_health_exempt(["my-app.fly.dev"])
    client = TestClient(app)
    resp = client.get("/health", headers={"Host": "172.19.24.105"})
    assert resp.status_code == 200, (
        f"Fly probe would still be rejected: {resp.status_code} "
        f"{resp.text[:200]!r}"
    )
    assert resp.json() == {"ok": True, "service": "google-docs-mcp"}


def test_health_accepts_any_raw_ip_host_header():
    """Generalize beyond Fly's specific 172.19.24.x range — the
    exemption is unconditional on /health."""
    app = _build_app_with_health_exempt(["my-app.fly.dev"])
    client = TestClient(app)
    for probe_host in ("10.0.0.1", "192.168.1.1", "[::1]", "anything.example"):
        resp = client.get("/health", headers={"Host": probe_host})
        assert resp.status_code == 200, (
            f"/health rejected probe with Host={probe_host!r}: "
            f"{resp.status_code} {resp.text[:200]!r}"
        )


def test_non_health_routes_still_reject_bad_host():
    """Critical security invariant: every NON-/health route must
    still go through the full TrustedHost gate. The exemption is
    scoped to /health only — bypassing it on / or /api/* would
    open up Host-header attacks against routes that actually echo
    or branch on the Host."""
    app = _build_app_with_health_exempt(["my-app.fly.dev"])
    client = TestClient(app)
    resp = client.get("/other", headers={"Host": "evil.example"})
    assert resp.status_code == 400, (
        f"/other accepted bad Host (security regression!): "
        f"{resp.status_code} {resp.text[:200]!r}"
    )


def test_non_health_routes_accept_allowed_host():
    """Sanity: with a valid Host, non-/health routes pass."""
    app = _build_app_with_health_exempt(["my-app.fly.dev"])
    client = TestClient(app)
    resp = client.get("/other", headers={"Host": "my-app.fly.dev"})
    assert resp.status_code == 200
    assert resp.json() == {"other": True}


def test_health_accepts_canonical_host_too():
    """Sanity: the exemption doesn't break the canonical hostname
    path. External requests to https://<app>.fly.dev/health still
    work."""
    app = _build_app_with_health_exempt(["my-app.fly.dev"])
    client = TestClient(app)
    resp = client.get("/health", headers={"Host": "my-app.fly.dev"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "google-docs-mcp"}


def test_health_exempt_middleware_passes_lifespan_through():
    """ASGI lifespan messages must reach the underlying app — without
    this, FastMCP's StreamableHTTPSessionManager (which depends on
    lifespan startup) would never receive its startup signal and the
    first /mcp request would 500.

    This is the canary that the pure-ASGI implementation correctly
    handles non-http scopes, not just the http path we route in
    __call__."""
    app = _build_app_with_health_exempt(["my-app.fly.dev"])
    with TestClient(app) as client:
        # TestClient triggers lifespan startup on context enter and
        # shutdown on exit. If lifespan messages got swallowed by the
        # middleware, the `with` block would raise.
        resp = client.get("/health")
        assert resp.status_code == 200
