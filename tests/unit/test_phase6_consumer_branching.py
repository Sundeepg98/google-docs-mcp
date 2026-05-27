"""Phase 6 mode-branching tests.

server._get_credentials() (re-exported from _tool_helpers.py since
M3 Phase C / v2.1.5) and docx_import._resolve_webapp_url() must
branch correctly on transport mode:

- HTTP / multi-tenant (current_user_id_or_none() returns sub):
  per-user resolver, per-user URL from user_store.
- Stdio / single-tenant (returns None): operator's cached creds,
  operator's URL from local config.

Without these guards a multi-tenant Fly deploy would route every
cloud chat user's API calls through the operator's Google identity
and Apps Script Web App — i.e. exactly the v1.0 broken state.

**M3 Phase C note:** ``_get_credentials`` moved to
``google_docs_mcp._tool_helpers`` (the 3-consumer extraction trigger:
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
    import google_docs_mcp._tool_helpers as helpers_mod
    helpers_mod._creds_cache = None

    yield tmp_path

    helpers_mod._creds_cache = None


# ---------------------------------------------------------------
# server._get_credentials() mode branching
# ---------------------------------------------------------------


def test_get_credentials_stdio_mode_uses_load_credentials(isolated_state):
    """No auth context → operator's local OAuth cache (stdio behavior)."""
    from google_docs_mcp import server

    fake_creds = MagicMock(valid=True)
    with patch(
        "google_docs_mcp._tool_helpers.current_user_id_or_none", return_value=None,
    ), patch(
        "google_docs_mcp._tool_helpers.load_credentials", return_value=fake_creds,
    ) as load_mock, patch(
        "google_docs_mcp._tool_helpers.get_credentials_for_user"
    ) as per_user_mock:
        result = server._get_credentials()

    assert result is fake_creds
    load_mock.assert_called_once()
    per_user_mock.assert_not_called()


def test_get_credentials_http_mode_uses_per_user_resolver(isolated_state):
    """Auth context present → per-user resolver, stdio path NOT touched."""
    from google_docs_mcp import server

    fake_creds = MagicMock()
    # PR-Δ5: production credentials returned by ``get_credentials_for_user``
    # are stamped with the requesting user_id (via ``_stamp_tenant``) so
    # the ``assert_tenant_match`` check inside ``_get_credentials`` can
    # verify the tenant binding. Stamp the mock with the matching id so
    # the assertion passes — mirrors the production contract.
    fake_creds._google_docs_mcp_user_id = "user-sub-abc"
    with patch(
        "google_docs_mcp._tool_helpers.current_user_id_or_none",
        return_value="user-sub-abc",
    ), patch(
        "google_docs_mcp._tool_helpers.resolve_runtime_oauth_config",
        return_value={
            "client_config": {"web": {"client_id": "X", "client_secret": "Y"}},
            "signing_key": "K",
            "base_url": "https://example.fly.dev",
        },
    ), patch(
        "google_docs_mcp._tool_helpers.get_credentials_for_user", return_value=fake_creds,
    ) as per_user_mock, patch(
        "google_docs_mcp._tool_helpers.load_credentials"
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

    from google_docs_mcp import server
    from google_docs_mcp.credentials import NeedsReauthError

    with patch(
        "google_docs_mcp._tool_helpers.current_user_id_or_none", return_value="user-1",
    ), patch(
        "google_docs_mcp._tool_helpers.resolve_runtime_oauth_config",
        return_value={"client_config": {}, "signing_key": "K", "base_url": "B"},
    ), patch(
        "google_docs_mcp._tool_helpers.get_credentials_for_user",
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


# ---------------------------------------------------------------
# docx_import._resolve_webapp_url() mode branching
# ---------------------------------------------------------------


def test_resolve_webapp_url_stdio_mode_returns_local_config_value(isolated_state):
    """No auth context → operator's local config URL (stdio behavior)."""
    from google_docs_mcp import config, docx_import

    config.save({"apps_script_webapp_url": "https://operator.example/exec"})

    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none", return_value=None,
    ):
        url = docx_import._resolve_webapp_url()

    assert url == "https://operator.example/exec"


def test_resolve_webapp_url_http_mode_returns_per_user_value(isolated_state):
    """Auth context present + per-user URL in user_store → that URL."""
    from google_docs_mcp import docx_import, user_store

    user_store.save_state(
        "user-alice",
        {"apps_script_url": "https://script.google.com/macros/s/ALICE/exec"},
    )

    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none",
        return_value="user-alice",
    ):
        url = docx_import._resolve_webapp_url()

    assert url == "https://script.google.com/macros/s/ALICE/exec"


def test_resolve_webapp_url_http_mode_returns_None_when_user_has_no_setup(
    isolated_state,
):
    """User authorized but never ran gdocs_setup_apps_script — URL absent.
    Returns None so the caller raises the HTTP-specific 'run setup' error."""
    from google_docs_mcp import docx_import

    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none",
        return_value="user-new",
    ):
        url = docx_import._resolve_webapp_url()

    assert url is None


def test_http_mode_isolation_alice_cannot_see_bobs_url(isolated_state):
    """Cross-user isolation guard: alice's webapp URL lookup must NEVER
    return bob's URL. Without this, a multi-tenant deploy would route
    cross-user."""
    from google_docs_mcp import docx_import, user_store

    user_store.save_state("alice", {"apps_script_url": "https://script.google.com/macros/s/ALICE/exec"})
    user_store.save_state("bob", {"apps_script_url": "https://script.google.com/macros/s/BOB/exec"})

    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none", return_value="alice",
    ):
        alice_url = docx_import._resolve_webapp_url()
    with patch(
        "google_docs_mcp.docx_import.current_user_id_or_none", return_value="bob",
    ):
        bob_url = docx_import._resolve_webapp_url()

    assert alice_url == "https://script.google.com/macros/s/ALICE/exec"
    assert bob_url == "https://script.google.com/macros/s/BOB/exec"
    assert alice_url != bob_url
