"""Phase 6 mode-branching tests.

server._get_credentials() (re-exported from _tool_helpers.py since
M3 Phase C / v2.1.5) must branch correctly on transport mode:

- HTTP / multi-tenant (current_user_id_or_none() returns sub):
  per-user resolver.
- Stdio / single-tenant (returns None): operator's cached creds.

Without this guard a multi-tenant Fly deploy would route every cloud
chat user's API calls through the operator's Google identity, exactly
the v1.0 broken state. (The sibling docx_import._resolve_webapp_url
branching tests died with that resolver: the tabs pipeline is pure
REST now and has no per-user web-app URL to route. See
_audit/2026-07-08-tabs-architecture-decision.md.)

**M3 Phase C note:** ``_get_credentials`` moved to
``appscriptly._tool_helpers`` (the 3-consumer extraction trigger:
docs + drive + gas_deploy all want it). ``server._get_credentials``
is now a re-export. Patches target ``_tool_helpers`` namespace —
that's where the function reads its dependencies from.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate user_store, local config dir, and creds cache per test."""
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))

    # Clear the module-level creds cache so each test starts clean.
    # The cache moved from server.py to _tool_helpers.py in M3 Phase C.
    import appscriptly._tool_helpers as helpers_mod
    helpers_mod._creds_cache = None

    yield tmp_path

    helpers_mod._creds_cache = None


# ---------------------------------------------------------------
# server._get_credentials() mode branching
# ---------------------------------------------------------------


def test_get_credentials_stdio_mode_uses_load_credentials(isolated_state):
    """No auth context → operator's local OAuth cache (stdio behavior)."""
    from appscriptly import server

    fake_creds = MagicMock(valid=True)
    with patch(
        "appscriptly._tool_helpers.current_user_id_or_none", return_value=None,
    ), patch(
        "appscriptly._tool_helpers.load_credentials", return_value=fake_creds,
    ) as load_mock, patch(
        "appscriptly._tool_helpers.get_credentials_for_user"
    ) as per_user_mock:
        result = server._get_credentials()

    assert result is fake_creds
    load_mock.assert_called_once()
    per_user_mock.assert_not_called()


def test_get_credentials_http_mode_uses_per_user_resolver(isolated_state):
    """Auth context present → per-user resolver, stdio path NOT touched."""
    from appscriptly import server

    fake_creds = MagicMock()
    # PR-Δ5: production credentials returned by ``get_credentials_for_user``
    # are stamped with the requesting user_id (via ``_stamp_tenant``) so
    # the ``assert_tenant_match`` check inside ``_get_credentials`` can
    # verify the tenant binding. Stamp the mock with the matching id so
    # the assertion passes — mirrors the production contract.
    fake_creds._google_docs_mcp_user_id = "user-sub-abc"
    with patch(
        "appscriptly._tool_helpers.current_user_id_or_none",
        return_value="user-sub-abc",
    ), patch(
        "appscriptly._tool_helpers.resolve_runtime_oauth_config",
        return_value={
            "client_config": {"web": {"client_id": "X", "client_secret": "Y"}},
            "signing_key": "K",
            "base_url": "https://example.fly.dev",
        },
    ), patch(
        "appscriptly._tool_helpers.get_credentials_for_user", return_value=fake_creds,
    ) as per_user_mock, patch(
        "appscriptly._tool_helpers.load_credentials"
    ) as load_mock:
        result = server._get_credentials()

    assert result is fake_creds
    per_user_mock.assert_called_once()
    load_mock.assert_not_called()
    # The sub was passed through correctly.
    assert per_user_mock.call_args.args[0] == "user-sub-abc"


def test_get_credentials_http_mode_NeedsReauth_raises_ToolError_with_url(
    isolated_state,
):
    """When the user hasn't authorized yet, the tool must surface a
    clickable URL — not a bare 'auth failed' that the model would
    interpret as 'try again with different params.'"""
    from fastmcp.exceptions import ToolError

    from appscriptly import server
    from appscriptly.credentials import NeedsReauthError

    with patch(
        "appscriptly._tool_helpers.current_user_id_or_none", return_value="user-1",
    ), patch(
        "appscriptly._tool_helpers.resolve_runtime_oauth_config",
        return_value={"client_config": {}, "signing_key": "K", "base_url": "B"},
    ), patch(
        "appscriptly._tool_helpers.get_credentials_for_user",
        side_effect=NeedsReauthError(
            "user-1",
            auth_url="https://accounts.google.com/o/oauth2/auth?fake",
            reason="not yet authorized",
        ),
    ):
        with pytest.raises(ToolError) as exc:
            server._get_credentials()

    assert "https://accounts.google.com/o/oauth2/auth?fake" in str(exc.value)
    assert "[Click here to authorize]" in str(exc.value)


