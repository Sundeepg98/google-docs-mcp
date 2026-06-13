"""PR-Δ1 (v2.3.4) — RFC 9728 OAuth Protected Resource Metadata endpoint.

The MCP Authorization spec MANDATES ``GET /.well-known/oauth-protected-
resource`` for any MCP server that exposes OAuth-protected resources.
Pre-PR-Δ1 the server returned 404 (verified via live curl by the
operator), which broke spec-conformant claude.ai connector discovery
probes. This file pins the shape, the surfaced scope list, and the
no-bearer-required public-endpoint contract.

The companion RFC 8414 endpoint (``/.well-known/oauth-authorization-
server``) is auto-wired by FastMCP's ``GoogleProvider`` and is not
tested here — that's FastMCP's responsibility. This file is the
``/.well-known/oauth-protected-resource`` regression guard.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _build_well_known_app() -> Starlette:
    """Minimal Starlette app with just the RFC 9728 route mounted.

    Mirrors the production wiring in ``http_server.app.build_app``
    for this specific endpoint; intentionally bypasses the
    BearerTokenMiddleware to verify the endpoint is reachable
    WITHOUT auth (claude.ai's discovery probes it before consent,
    so no bearer is available). The middleware's bypass of
    ``/.well-known/*`` is verified separately by the existing
    BearerTokenMiddleware tests; here we just exercise the handler
    in isolation.
    """
    from appscriptly.http_server.routes.observability import (
        oauth_protected_resource_metadata,
    )

    return Starlette(routes=[
        Route(
            "/.well-known/oauth-protected-resource",
            oauth_protected_resource_metadata,
            methods=["GET"],
        ),
    ])


# ---------------------------------------------------------------------
# 1. Endpoint exists at the spec-mandated path (regression guard)
# ---------------------------------------------------------------------


def test_oauth_protected_resource_returns_200_at_well_known_path(monkeypatch):
    """Pre-PR-Δ1 this path was 404 (verified by operator). The MCP
    Authorization spec MANDATES it; this assertion is the regression
    guard against an accidental route deletion."""
    monkeypatch.setenv(
        "GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev",
    )
    client = TestClient(_build_well_known_app())
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200, (
        f"RFC 9728 endpoint returned {resp.status_code} (expected 200). "
        f"MCP spec requires this path to exist on any OAuth-protected "
        f"resource server. Body: {resp.text!r}"
    )


def test_oauth_protected_resource_returns_json_content_type(monkeypatch):
    """RFC 9728 §3.2 requires the metadata response to be JSON.
    Starlette's ``JSONResponse`` sets the correct header; this is a
    canary for an accidental switch to ``PlainTextResponse``."""
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    client = TestClient(_build_well_known_app())
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.headers["content-type"].startswith("application/json"), (
        f"RFC 9728 metadata response content-type should be JSON; "
        f"got {resp.headers['content-type']!r}"
    )


def test_oauth_protected_resource_is_publicly_accessible_without_bearer(
    monkeypatch,
):
    """RFC 9728 metadata is discovery metadata — by spec it MUST be
    fetchable without any credential (claude.ai's connector probes
    it before consent is even possible). The endpoint handler itself
    doesn't enforce auth; in production, ``BearerTokenMiddleware``'s
    dispatch matcher excludes ``/.well-known/*`` from the bearer-
    required set. This test exercises the handler-level contract
    (no creds required to reach the JSON body)."""
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    client = TestClient(_build_well_known_app())
    # No Authorization header at all.
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    # Body is a real JSON object, not an auth-required error envelope.
    assert isinstance(body, dict)
    assert "resource" in body


# ---------------------------------------------------------------------
# 2. Response shape per RFC 9728 §3 (Protected Resource Metadata)
# ---------------------------------------------------------------------


def test_oauth_protected_resource_response_carries_rfc9728_required_fields(
    monkeypatch,
):
    """RFC 9728 §3 enumerates the required + optional metadata fields.
    Pin the ones we surface so a future refactor that drops one
    (e.g. forgets ``bearer_methods_supported`` after a copy-paste)
    is caught instantly."""
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/oauth-protected-resource").json()

    # ``resource`` is REQUIRED (RFC 9728 §3.1).
    assert body["resource"] == "https://example.fly.dev", (
        f"``resource`` must identify the protected resource server URL. "
        f"Got: {body.get('resource')!r}"
    )
    # ``authorization_servers`` is REQUIRED for proxy scenarios.
    # We surface ourselves because GoogleProvider exposes the RFC 8414
    # endpoint on this server (proxy in front of Google).
    assert body["authorization_servers"] == ["https://example.fly.dev"], (
        f"``authorization_servers`` should list the auth-server URL "
        f"(ours, because the RFC 8414 endpoint is here). "
        f"Got: {body.get('authorization_servers')!r}"
    )
    # ``scopes_supported`` is OPTIONAL but expected so clients can
    # advertise which scopes may be required for token requests.
    assert isinstance(body["scopes_supported"], list)
    assert len(body["scopes_supported"]) > 0, (
        "``scopes_supported`` should list every scope from "
        "GOOGLE_API_SCOPES; got an empty list."
    )
    # ``bearer_methods_supported`` is OPTIONAL but pinned because we
    # deliberately accept only the Authorization header — NOT query
    # strings or POST bodies (RFC 6750 §2.2/§2.3 surfaces we don't
    # implement). A future "let me add query-string bearer for
    # convenience" change should fail this test and force a security
    # review.
    assert body["bearer_methods_supported"] == ["header"], (
        f"``bearer_methods_supported`` must be ['header'] only. We "
        f"deliberately do NOT support query-string or POST-body bearer "
        f"presentation (RFC 6750 §2.2/§2.3 — additional attack surface). "
        f"Got: {body.get('bearer_methods_supported')!r}"
    )
    # ``resource_documentation`` is OPTIONAL but surfaced for
    # discoverability.
    assert "resource_documentation" in body


def test_oauth_protected_resource_scopes_supported_mirrors_GOOGLE_API_SCOPES(
    monkeypatch,
):
    """``scopes_supported`` MUST be the canonical baseline OAuth scope
    list. Sourcing it from ``oauth_google.GOOGLE_API_SCOPES`` (rather
    than a duplicate registry) means scope additions / removals in
    that one place propagate to the metadata endpoint without a
    second edit. This test pins the no-duplicate-registry contract."""
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")

    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/oauth-protected-resource").json()
    # Sorted for stable cross-deploy ordering — match the handler.
    assert body["scopes_supported"] == sorted(GOOGLE_API_SCOPES), (
        f"scopes_supported drift: endpoint returned "
        f"{body['scopes_supported']!r}, GOOGLE_API_SCOPES has "
        f"{sorted(GOOGLE_API_SCOPES)!r}. Update one to match the other; "
        f"the handler should source from GOOGLE_API_SCOPES directly."
    )


def test_oauth_protected_resource_includes_apps_script_scopes_after_PR_delta_1(
    monkeypatch,
):
    """PR-Δ1 (v2.3.4) promoted the Apps Script management scopes into
    the baseline ``GOOGLE_API_SCOPES`` union. The RFC 9728 metadata
    response MUST reflect this — claude.ai's discovery uses it to
    advertise to the user what scopes the consent screen will ask
    for. If a future refactor accidentally removes the Apps Script
    scopes from baseline, the second-consent UX regression would
    silently return."""
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    client = TestClient(_build_well_known_app())
    scopes_supported = client.get(
        "/.well-known/oauth-protected-resource",
    ).json()["scopes_supported"]

    assert "https://www.googleapis.com/auth/script.projects" in scopes_supported, (
        "PR-Δ1 promoted script.projects to baseline; advertised "
        "scope list should include it. Without this, claude.ai's "
        "consent screen wouldn't ask for the Apps Script scopes on "
        "first consent and the second-consent UX papercut would return."
    )
    assert "https://www.googleapis.com/auth/script.deployments" in scopes_supported, (
        "PR-Δ1 promoted script.deployments to baseline; advertised "
        "scope list should include it."
    )


# ---------------------------------------------------------------------
# 3. resource / authorization_servers URL resolution
# ---------------------------------------------------------------------


def test_oauth_protected_resource_resource_url_uses_GOOGLE_OAUTH_BASE_URL(
    monkeypatch,
):
    """The ``resource`` URL is the canonical identifier of the
    protected resource server. We derive it via
    ``_resolve_base_url`` which prefers ``GOOGLE_OAUTH_BASE_URL``
    (the operator-set env var, reliable behind Fly's edge proxy).
    A different deployment should surface its own URL — verify the
    handler isn't accidentally hard-coding a literal."""
    monkeypatch.setenv(
        "GOOGLE_OAUTH_BASE_URL", "https://different-deploy.example.com",
    )
    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/oauth-protected-resource").json()
    assert body["resource"] == "https://different-deploy.example.com"
    assert body["authorization_servers"] == [
        "https://different-deploy.example.com",
    ]


def test_oauth_protected_resource_strips_trailing_slash_from_base_url(
    monkeypatch,
):
    """``_resolve_base_url`` rstrips '/' so the ``resource`` identifier
    is canonical. Pinned so a future helper-rewrite that drops the
    rstrip doesn't quietly break URL equality checks at the
    claude.ai end."""
    monkeypatch.setenv(
        "GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev/",
    )
    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/oauth-protected-resource").json()
    assert body["resource"] == "https://example.fly.dev", (
        f"trailing slash should have been stripped; got {body['resource']!r}"
    )
