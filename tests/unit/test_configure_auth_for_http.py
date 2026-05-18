"""Phase 7 activation switch tests.

configure_auth_for_http() is the load-bearing function that turns the
Fly deployment multi-tenant. Without it, the per-user creds resolver
(Phase 4) and per-user setup tool (Phase 5) and per-user URL lookup
(Phase 6) all exist but never fire — every cloud user falls through
to the operator's identity.

The most subtle bit is the post-init scope-bundling: setting
``valid_scopes`` in the GoogleProvider constructor only advertises
scopes at the well-known endpoint, but Claude.ai's DCR/CIMD clients
won't actually request them unless we ALSO patch
``client_registration_options.default_scopes`` AND
``_default_scope_str``. If we skip the patches, every Workspace tool
call silently 401s for insufficient scopes.

These tests guard those mutations so a future refactor can't quietly
break the multi-tenant flow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Don't leak our env mutations into other tests."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-signing-key")
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    cs = tmp_path / "client_secrets.json"
    import json
    cs.write_text(json.dumps({
        "web": {
            "client_id": "TEST_CLIENT_ID.apps.googleusercontent.com",
            "client_secret": "TEST_CLIENT_SECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["https://example.fly.dev/oauth/google/api/callback"],
        },
    }))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRETS_PATH", str(cs))
    # Make sure the insecure-transport guard doesn't trip in tests.
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    yield


def _fresh_mcp():
    """Stand-in for a FastMCP instance with the surface we touch."""
    mcp = MagicMock()
    mcp.auth = None
    return mcp


def test_first_call_sets_mcp_auth_to_a_GoogleProvider():
    from google_docs_mcp.oauth_google import configure_auth_for_http

    mcp = _fresh_mcp()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider"
    ) as gp_class:
        configure_auth_for_http(mcp)

    gp_class.assert_called_once()
    assert mcp.auth is gp_class.return_value


def test_second_call_is_idempotent_no_op():
    """Avoid clobbering the provider on a re-entry — important for
    test isolation and hot-reload scenarios."""
    from google_docs_mcp.oauth_google import configure_auth_for_http

    mcp = _fresh_mcp()
    mcp.auth = MagicMock(name="already_configured_provider")
    sentinel = mcp.auth

    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider"
    ) as gp_class:
        configure_auth_for_http(mcp)

    gp_class.assert_not_called()
    assert mcp.auth is sentinel


def test_required_scopes_are_identity_only():
    """``required_scopes`` is what MUST be granted for auth to succeed.
    If we put Workspace scopes here and Google grants less, the OAuth
    handshake fails entirely. Keep identity-only as the floor."""
    from google_docs_mcp.oauth_google import IDENTITY_SCOPES, configure_auth_for_http

    mcp = _fresh_mcp()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider"
    ) as gp_class:
        configure_auth_for_http(mcp)

    kwargs = gp_class.call_args.kwargs
    assert kwargs["required_scopes"] == IDENTITY_SCOPES


def test_valid_scopes_include_full_workspace_union():
    """``valid_scopes`` advertises at the well-known endpoint that
    Workspace scopes CAN be requested. Without this in the constructor
    arg, the discovery metadata is wrong."""
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, configure_auth_for_http,
    )

    mcp = _fresh_mcp()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider"
    ) as gp_class:
        configure_auth_for_http(mcp)

    valid_scopes = set(gp_class.call_args.kwargs["valid_scopes"])
    for s in GOOGLE_API_SCOPES:
        assert s in valid_scopes, f"Workspace scope {s!r} missing from valid_scopes"


def test_default_scopes_patched_on_client_registration_options():
    """The DCR-side patch. Without this, Claude.ai's connector only
    requests identity scopes regardless of valid_scopes — the silent-
    insufficient-scope foot-gun the agent's research warned about."""
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, IDENTITY_SCOPES, configure_auth_for_http,
    )

    mcp = _fresh_mcp()
    provider = MagicMock()
    provider.client_registration_options = MagicMock()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider",
        return_value=provider,
    ):
        configure_auth_for_http(mcp)

    expected = set(IDENTITY_SCOPES) | set(GOOGLE_API_SCOPES)
    assert set(provider.client_registration_options.default_scopes) == expected, (
        "client_registration_options.default_scopes not patched — "
        "Claude.ai's DCR clients will only request identity scopes"
    )


def test_default_scope_str_patched_on_private_attr():
    """The CIMD-side patch. ``_default_scope_str`` is consulted when a
    client omits ``scope`` in /authorize and by the CIMD path. Missing
    this breaks Claude Code (CIMD) even if DCR (Claude.ai) is OK."""
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, IDENTITY_SCOPES, configure_auth_for_http,
    )

    mcp = _fresh_mcp()
    provider = MagicMock()
    provider.client_registration_options = MagicMock()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider",
        return_value=provider,
    ):
        configure_auth_for_http(mcp)

    expected = " ".join(sorted(set(IDENTITY_SCOPES) | set(GOOGLE_API_SCOPES)))
    assert provider._default_scope_str == expected


def test_insecure_transport_envvar_refused(monkeypatch):
    """Foot-gun guard: refuse to run if OAUTHLIB_INSECURE_TRANSPORT=1
    is set. This is for localhost HTTP dev only; on Fly (HTTPS) it
    silently disables transport security checks."""
    from google_docs_mcp.oauth_google import configure_auth_for_http

    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    mcp = _fresh_mcp()

    with pytest.raises(RuntimeError, match="OAUTHLIB_INSECURE_TRANSPORT"):
        configure_auth_for_http(mcp)


def test_relax_token_scope_envvar_set():
    """Without this, partial-grant consents (user unchecked Apps Script)
    cause Flow.fetch_token to raise — dead-end re-auth UX."""
    import os
    from google_docs_mcp.oauth_google import configure_auth_for_http

    mcp = _fresh_mcp()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider"
    ):
        configure_auth_for_http(mcp)

    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"


def test_missing_client_id_raises_loudly(monkeypatch, tmp_path):
    from google_docs_mcp.oauth_google import configure_auth_for_http
    import json

    cs = tmp_path / "broken.json"
    cs.write_text(json.dumps({"web": {"client_secret": "Y"}}))  # no client_id
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRETS_PATH", str(cs))

    mcp = _fresh_mcp()
    with pytest.raises(RuntimeError, match="client_id and client_secret"):
        configure_auth_for_http(mcp)
