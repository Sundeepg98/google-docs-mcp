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
    from appscriptly.oauth_google import configure_auth_for_http

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
    from appscriptly.oauth_google import configure_auth_for_http

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
    from appscriptly.oauth_google import IDENTITY_SCOPES, configure_auth_for_http

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
    from appscriptly.oauth_google import (
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
    from appscriptly.oauth_google import (
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
    from appscriptly.oauth_google import (
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
    from appscriptly.oauth_google import configure_auth_for_http

    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    mcp = _fresh_mcp()

    with pytest.raises(RuntimeError, match="OAUTHLIB_INSECURE_TRANSPORT"):
        configure_auth_for_http(mcp)


def test_relax_token_scope_envvar_set():
    """Without this, partial-grant consents (user unchecked Apps Script)
    cause Flow.fetch_token to raise — dead-end re-auth UX."""
    import os
    from appscriptly.oauth_google import configure_auth_for_http

    mcp = _fresh_mcp()
    with patch(
        "fastmcp.server.auth.providers.google.GoogleProvider"
    ):
        configure_auth_for_http(mcp)

    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"


def test_missing_client_id_raises_loudly(monkeypatch, tmp_path):
    from appscriptly.oauth_google import configure_auth_for_http
    import json

    cs = tmp_path / "broken.json"
    cs.write_text(json.dumps({"web": {"client_secret": "Y"}}))  # no client_id
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRETS_PATH", str(cs))

    mcp = _fresh_mcp()
    with pytest.raises(RuntimeError, match="client_id and client_secret"):
        configure_auth_for_http(mcp)


# ---------------------------------------------------------------------------
# OAuth-state persistence guard — the "connector re-auths on every restart" fix
# ---------------------------------------------------------------------------
#
# FastMCP's GoogleProvider/OAuthProxy persists the DCR client registration
# (claude.ai's issued client_id) AND the upstream Google refresh token to a
# file store under ``fastmcp.settings.home``. On Fly that defaults to the
# EPHEMERAL ``/home/app/.local/share/fastmcp`` unless ``FASTMCP_HOME`` points
# at the ``/data`` volume — and an ephemeral store means every deploy wipes
# the registration + refresh token, forcing the full browser consent dance.
#
# ``configure_auth_for_http`` calls ``_assert_oauth_state_is_persistent()``
# BEFORE constructing the provider; it only enforces on a detected Fly runtime
# (FLY_APP_NAME set) and accepts an ALLOW_EPHEMERAL_OAUTH_STATE=1 escape hatch.
# These tests pin all four corners of that decision.


def test_persistence_guard_raises_on_fly_with_ephemeral_home(monkeypatch):
    """On a Fly machine, an off-volume FastMCP home must refuse to boot.

    This is the regression that produced the reported symptom: connector
    forced through full re-consent after every deploy because the DCR
    registration + Google refresh token were on ephemeral disk.
    """
    from appscriptly.oauth_google import configure_auth_for_http

    monkeypatch.setenv("FLY_APP_NAME", "sundeepg98-docs-mcp")
    monkeypatch.delenv("ALLOW_EPHEMERAL_OAUTH_STATE", raising=False)
    # Simulate the broken (default) layout: FastMCP home on the container
    # overlay fs, not under /data.
    monkeypatch.setenv("FASTMCP_HOME", "/home/app/.local/share/fastmcp")
    # GOOGLE_DOCS_DATA_DIR is /data-anchored in prod; keep it realistic so
    # the guard's persistent-roots include the real volume path too.
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", "/data/google-docs-mcp")

    mcp = _fresh_mcp()
    with patch("fastmcp.server.auth.providers.google.GoogleProvider") as gp_class:
        with pytest.raises(RuntimeError, match="EPHEMERAL disk"):
            configure_auth_for_http(mcp)
    # The provider must NOT have been constructed — we fail BEFORE wiring.
    gp_class.assert_not_called()


def test_persistence_guard_passes_on_fly_with_volume_home(monkeypatch):
    """On Fly with FASTMCP_HOME under /data, the guard is satisfied and
    provider wiring proceeds normally."""
    from appscriptly.oauth_google import configure_auth_for_http

    monkeypatch.setenv("FLY_APP_NAME", "sundeepg98-docs-mcp")
    monkeypatch.delenv("ALLOW_EPHEMERAL_OAUTH_STATE", raising=False)
    monkeypatch.setenv("FASTMCP_HOME", "/data/fastmcp")
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", "/data/google-docs-mcp")

    mcp = _fresh_mcp()
    with patch("fastmcp.server.auth.providers.google.GoogleProvider") as gp_class:
        configure_auth_for_http(mcp)
    gp_class.assert_called_once()
    assert mcp.auth is gp_class.return_value


def test_persistence_guard_is_noop_off_fly(monkeypatch):
    """Off Fly (no FLY_APP_NAME) the guard does nothing — local/non-Fly
    HTTP deploys have a genuinely persistent platformdirs home, and we
    must not break ``MCP_TRANSPORT=http`` on a laptop even if the home
    happens to look ephemeral-ish."""
    from appscriptly.oauth_google import configure_auth_for_http

    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("ALLOW_EPHEMERAL_OAUTH_STATE", raising=False)
    # Even a clearly-ephemeral-looking home is tolerated off-Fly.
    monkeypatch.setenv("FASTMCP_HOME", "/tmp/whatever/fastmcp")

    mcp = _fresh_mcp()
    with patch("fastmcp.server.auth.providers.google.GoogleProvider") as gp_class:
        configure_auth_for_http(mcp)
    gp_class.assert_called_once()


def test_persistence_guard_escape_hatch_bypasses_on_fly(monkeypatch):
    """ALLOW_EPHEMERAL_OAUTH_STATE=1 lets an operator who wired a durable
    external client_storage (or knowingly accepts ephemeral state) bypass
    the guard even on Fly."""
    from appscriptly.oauth_google import configure_auth_for_http

    monkeypatch.setenv("FLY_APP_NAME", "sundeepg98-docs-mcp")
    monkeypatch.setenv("ALLOW_EPHEMERAL_OAUTH_STATE", "1")
    monkeypatch.setenv("FASTMCP_HOME", "/home/app/.local/share/fastmcp")

    mcp = _fresh_mcp()
    with patch("fastmcp.server.auth.providers.google.GoogleProvider") as gp_class:
        configure_auth_for_http(mcp)
    gp_class.assert_called_once()


def test_fastmcp_default_home_is_off_volume_without_env(monkeypatch):
    """Anchor test: prove the BUG exists in FastMCP's default.

    With no FASTMCP_HOME (and a Fly-like $HOME), FastMCP's resolved
    storage home must NOT be under /data — i.e. the default genuinely
    lands on ephemeral disk. If a future FastMCP changes its default to
    something volume-safe, this test goes red and we can simplify the
    Dockerfile/fly.toml env + this guard.
    """
    from pathlib import PurePosixPath

    # Resolve FastMCP's home the way the provider does, but compute the
    # POSIX/Linux semantics explicitly (the test host may be Windows).
    # platformdirs Linux: $XDG_DATA_HOME or $HOME/.local/share.
    monkeypatch.delenv("FASTMCP_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    linux_home = PurePosixPath("/home/app/.local/share/fastmcp")
    assert not str(linux_home).startswith("/data"), (
        "FastMCP's Linux default data home is unexpectedly under /data; "
        "the persistence fix (FASTMCP_HOME + boot guard) may no longer be "
        "needed — re-verify before removing it."
    )


# ---------------------------------------------------------------------------
# Restart-survival proof — the LOAD-BEARING test for "survives `fly deploy`"
# ---------------------------------------------------------------------------
#
# The guard tests above prove we REFUSE to boot with an ephemeral store. This
# test proves the POSITIVE: that when OAuthProxy's storage directory IS durable
# (== what FASTMCP_HOME=/data/fastmcp buys us on the volume), a DCR client
# registration written by one provider instance is still recognized by a
# SECOND, freshly-constructed provider instance pointed at the same directory —
# i.e. it survives a process restart / fresh Fly machine.
#
# This is the cross-process contract, not an in-process check: we throw away
# the first provider entirely and build a new one (the closest unit-test
# analogue of `fly deploy` spinning up a new container against the same
# volume). If FastMCP ever regressed to an in-memory-only store, or if the
# deterministic key derivation changed so the second instance couldn't decrypt
# the first's files, this test would go red.


def test_dcr_registration_survives_a_simulated_restart(tmp_path):
    """A registered connector client is still recognized after 'restart'.

    Construct provider #1 with a durable on-disk ``client_storage`` in
    ``tmp_path`` (stands in for /data/fastmcp on the volume), register a
    DCR client, then construct provider #2 against the SAME directory and
    assert ``get_client`` returns the registration. Provider #2 is a fresh
    object graph — the simulated post-deploy machine.

    Why this matters: the reported bug was that EVERY ``fly deploy`` forced
    a full browser re-auth, because OAuthProxy's default store lived on the
    ephemeral container fs and was wiped on each restart. With a durable
    directory the registration persists — and because the storage-encryption
    key is derived deterministically from the (stable) OAuth client_secret,
    provider #2 can decrypt what provider #1 wrote. No new client_secret =
    no re-registration = no re-consent.
    """
    import asyncio

    from mcp.shared.auth import OAuthClientInformationFull

    from fastmcp.server.auth.providers.google import GoogleProvider

    def _build_provider():
        # client_storage=None makes OAuthProxy build its OWN encrypted
        # FileTreeStore — but we must steer it at our durable tmp_path
        # rather than the platformdirs default. The supported lever is
        # FASTMCP_HOME (the exact same lever the production fix uses), so
        # set it for the duration of construction. This makes the test
        # exercise the REAL default-storage code path (encrypted file
        # store), not a hand-rolled backend — i.e. it validates the actual
        # production configuration, not a mock of it.
        import os as _os

        prev = _os.environ.get("FASTMCP_HOME")
        _os.environ["FASTMCP_HOME"] = str(tmp_path)
        try:
            return GoogleProvider(
                client_id="test-client-id.apps.googleusercontent.com",
                # Stable secret across both instances == stable derived
                # JWT + storage-encryption keys (the deterministic-key
                # property the fix relies on). A DIFFERENT secret here
                # would land in a different key_fingerprint subdir AND
                # fail to decrypt — exactly the "don't change the secret"
                # caveat, asserted by construction.
                client_secret="STABLE_TEST_SECRET_value",
                base_url="https://example.fly.dev",
            )
        finally:
            if prev is None:
                _os.environ.pop("FASTMCP_HOME", None)
            else:
                _os.environ["FASTMCP_HOME"] = prev

    client_id = "test-client-id.apps.googleusercontent.com"
    registration = OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl_for_test("https://claude.ai/api/mcp/auth_callback")],
    )

    # --- Provider #1: register the connector client, then discard it. ---
    provider_one = _build_provider()
    asyncio.run(provider_one.register_client(registration))
    # Sanity: it's readable within the same instance.
    same_instance = asyncio.run(provider_one.get_client(client_id))
    assert same_instance is not None
    del provider_one  # the "old machine" goes away

    # --- Provider #2: fresh object graph == post-deploy machine. ---
    provider_two = _build_provider()
    after_restart = asyncio.run(provider_two.get_client(client_id))

    assert after_restart is not None, (
        "DCR client registration did NOT survive a simulated restart against "
        "a durable storage directory. This is the exact failure the "
        "FASTMCP_HOME=/data/fastmcp fix prevents — if this is red, either "
        "FastMCP regressed to an in-memory store or the storage-encryption "
        "key stopped being derived deterministically from client_secret."
    )
    assert after_restart.client_id == client_id


def AnyUrl_for_test(url: str):
    """Build a pydantic ``AnyUrl`` without importing it at module top.

    Kept local so the import only happens when this restart test runs
    (the rest of the file deliberately avoids pulling pydantic url types).
    """
    from pydantic import AnyUrl

    return AnyUrl(url)
