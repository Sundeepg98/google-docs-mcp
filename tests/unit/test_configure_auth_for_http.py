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
    # v2.0b: HKDF derivation requires ≥32-char master. The pre-flip
    # "test-signing-key" (16 chars) worked via the shim path which
    # had no length check; post-flip every test that drives a code
    # path through keys.get_key("oauth_state") would RuntimeError.
    monkeypatch.setenv(
        "MCP_BEARER_TOKEN", "test-signing-key-32-characters-long"
    )
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


# ---------------------------------------------------------------------------
# R16 F4 regression test — closes audit Gap #4 (R12 A.3, R16 F8, R30 named)
# ---------------------------------------------------------------------------
#
# The two tests above (test_default_scopes_patched_on_client_registration_options
# and test_default_scope_str_patched_on_private_attr) both use ``MagicMock``
# for the provider. That patches OUR mutation logic but cannot catch the
# upstream-breakage class: if FastMCP renames ``_default_scope_str`` in a
# future minor version, MagicMock silently auto-creates the attribute on
# write and both tests pass — while production code's same assignment line
# becomes a silent no-op (the scope override stops applying; Claude Code's
# CIMD flow falls back to identity-only and every Workspace tool call 401s
# with insufficient scope).
#
# The tests below pin the contract by instantiating a REAL ``GoogleProvider``
# from the pinned ``fastmcp`` and asserting the private attributes our
# post-init patches target actually exist on a freshly-constructed instance.
# If a fastmcp upgrade removes any of them, these tests turn red at CI
# time — BEFORE the deploy that would silently break the OAuth flow.


def test_fastmcp_GoogleProvider_still_has_default_scope_str_private_attr():
    """Pin the FastMCP private-attr contract for ``_default_scope_str``.

    ``oauth_google.py:162`` does an UNCONDITIONAL assignment to
    ``provider._default_scope_str``. If FastMCP renames or removes
    this private attribute, our assignment becomes a silent no-op
    (Python allows setting arbitrary attributes on most objects),
    and the CIMD-side scope override stops applying — Claude Code
    clients fall back to identity-only and Workspace tool calls
    401 in production.

    Three audit rounds flagged this (R12 finding A.3, R16 F4/F8,
    R30 in the named-version block). This test closes the gap at
    near-zero cost: instantiate a real GoogleProvider with throw-
    away creds and assert the attribute exists.

    Failure mode (= what triggers this test red):
      - fastmcp upgrade renames ``_default_scope_str`` to something
        else (e.g. ``default_scope_str``, ``_scope_default_str``)
      - fastmcp upgrade replaces the str-typed scope cache with a
        method / property / removed slot

    Recovery if this test goes red:
      - (a) Pin fastmcp to the previous working minor version in
        ``pyproject.toml`` until oauth_google.py can be refactored
      - (b) Refactor oauth_google.py to use a public FastMCP scope
        API (preferred long-term)
    """
    from fastmcp.server.auth.providers.google import GoogleProvider

    # Real GoogleProvider, throwaway creds — we never actually issue
    # tokens; we just need the constructed instance to inspect.
    provider = GoogleProvider(
        client_id="test",
        client_secret="test",
        base_url="http://test",
    )
    assert hasattr(provider, "_default_scope_str"), (
        "FastMCP GoogleProvider lost the ``_default_scope_str`` private "
        "attribute. oauth_google.py:162 still ASSIGNS to it (silently no-op "
        "on a missing attr in Python); the CIMD scope-override path is now "
        "broken. Fix: pin fastmcp to the prior working version OR refactor "
        "oauth_google.py to use a public scope API. See test docstring."
    )


def test_fastmcp_GoogleProvider_still_has_client_registration_options_attr():
    """Pin the FastMCP attribute contract for ``client_registration_options``.

    Companion to ``test_fastmcp_GoogleProvider_still_has_default_scope_str_private_attr``.
    ``oauth_google.py:156`` reads ``client_registration_options`` via
    ``getattr(provider, "client_registration_options", None)`` and patches
    its ``default_scopes`` — the DCR-side scope override that Claude.ai's
    connector consumes. The ``getattr`` makes the production code defensive
    against the attribute disappearing (it would silently skip the patch
    and 401 every Claude.ai Workspace call), so this test pins the contract
    explicitly: if FastMCP renames this, we want the failure at CI time,
    not in production telemetry.

    Same recovery options as the sister test.
    """
    from fastmcp.server.auth.providers.google import GoogleProvider

    provider = GoogleProvider(
        client_id="test",
        client_secret="test",
        base_url="http://test",
    )
    assert hasattr(provider, "client_registration_options"), (
        "FastMCP GoogleProvider lost ``client_registration_options``. "
        "oauth_google.py:156 silently skips its DCR-side scope patch when "
        "this attr is missing — Claude.ai's connector then only requests "
        "identity scopes and every Workspace tool call 401s. Same fix "
        "options as the ``_default_scope_str`` sister test."
    )


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
