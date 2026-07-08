"""Cloud-side setup_apps_script_for_user tests.

The per-user variant of v1.0.1's ledger fix. Same regression guards
(no orphan projects on retry, hash-mismatch resets, manual-delete
recovery) but for the multi-tenant cloud path where user_store rows
are the ledger and each cloud chat user has their own row.

Key extra guards (vs the single-tenant setup_apps_script_auto tests):
- Two users running setup concurrently do NOT see each other's
  partial state (cross-user isolation).
- A user's google_creds_json is preserved when the apps_script_*
  fields get cleared on hash mismatch.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def isolated_user_store(tmp_path, monkeypatch):
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    yield db_file


@pytest.fixture(autouse=True)
def healthy_webapp_probe(monkeypatch):
    """The deployment-decay self-heal probes the cached /exec URL during
    setup. Unit tests must never touch the real network: default every
    probe in this file to HEALTHY — the exact pre-probe behavioral
    baseline (cached state is trusted). Decay behavior itself is covered
    in test_webapp_exec_selfheal.py."""
    from appscriptly import setup_apps_script

    monkeypatch.setattr(
        setup_apps_script,
        "probe_webapp_health",
        lambda url, **kwargs: setup_apps_script.WebAppHealth.HEALTHY,
    )


@pytest.fixture
def mock_setup():
    """Mock AppsScriptClient with sensible cold-start return values."""
    with patch(
        "appscriptly.setup_apps_script.AppsScriptClient"
    ) as client_class:
        client = MagicMock()
        client_class.return_value = client

        from appscriptly.services.gas_deploy.api import WebAppDeployment
        client.script_exists.return_value = True
        client.create_project.return_value = "SCRIPT_ID_NEW"
        client.create_version.return_value = 1
        client.deploy_webapp.return_value = WebAppDeployment(
            script_id="SCRIPT_ID_NEW",
            deployment_id="DEPLOY_ID",
            version=1,
            url="https://script.google.com/macros/s/DEPLOY_ID/exec",
        )
        yield client


def _fake_creds():
    """Stand-in for the Credentials object the resolver returns. The
    setup function only passes it through to AppsScriptClient (mocked),
    so a sentinel is enough."""
    return MagicMock(name="fake_creds")


def test_cold_start_creates_project_once(mock_setup):
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    assert mock_setup.create_project.call_count == 1


def test_cold_start_persists_url_and_ids_to_user_store(mock_setup):
    """The headline integration: setup writes the URL/IDs into
    user_store under the user's row. Downstream tools (Phase 6)
    will read this same row."""
    from appscriptly import user_store
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")

    state = user_store.get_state("user-1")
    assert state["apps_script_url"] == "https://script.google.com/macros/s/DEPLOY_ID/exec"
    assert state["apps_script_script_id"] == "SCRIPT_ID_NEW"
    assert state["apps_script_deployment_id"] == "DEPLOY_ID"
    assert state["apps_script_version_number"] == 1
    assert state["apps_script_content_hash"]  # hash was recorded


def test_second_run_same_user_same_content_does_not_re_create_project(mock_setup):
    """THE KILLER GUARD (cloud edition). The v1.0.1 fix for orphan
    projects — same shape, but per user. Second setup call for the
    same user must NOT call create_project again, otherwise we'd
    accumulate ghost scripts in their Drive over time."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_setup.create_project.call_count == 1, (
        "second setup_apps_script_for_user call created a second Apps "
        "Script project — user_store ledger isn't catching the prior run"
    )
    assert mock_setup.push_files.call_count == 1
    assert mock_setup.create_version.call_count == 1
    assert mock_setup.deploy_webapp.call_count == 1


def test_two_users_get_independent_projects(mock_setup):
    """Cross-user isolation: setup for user-A must not look at
    user-B's ledger row. Both users get their own create_project call."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    mock_setup.create_project.side_effect = ["SCRIPT_A", "SCRIPT_B"]
    mock_setup.deploy_webapp.side_effect = [
        _deployment("SCRIPT_A", "DEPLOY_A", "https://script.google.com/macros/s/DEPLOY_A/exec"),
        _deployment("SCRIPT_B", "DEPLOY_B", "https://script.google.com/macros/s/DEPLOY_B/exec"),
    ]

    setup_apps_script_for_user(_fake_creds(), "alice")
    setup_apps_script_for_user(_fake_creds(), "bob")

    assert mock_setup.create_project.call_count == 2

    from appscriptly import user_store
    assert user_store.get_state("alice")["apps_script_script_id"] == "SCRIPT_A"
    assert user_store.get_state("bob")["apps_script_script_id"] == "SCRIPT_B"


def test_resume_after_push_files_failure(mock_setup):
    """First call: create_project ok, push_files raises. Retry must
    skip create_project (use cached script_id from user_store) and
    continue from push_files. NO orphan project created."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    mock_setup.push_files.side_effect = RuntimeError("network blip")
    with pytest.raises(RuntimeError, match="network blip"):
        setup_apps_script_for_user(_fake_creds(), "user-1")

    mock_setup.push_files.side_effect = None
    setup_apps_script_for_user(_fake_creds(), "user-1")

    # CRITICAL: only ONE create_project across both attempts.
    assert mock_setup.create_project.call_count == 1
    # push_files was retried after the failure.
    assert mock_setup.push_files.call_count == 2


def test_resume_after_deploy_failure(mock_setup):
    """deploy_webapp fails after version is cut. Retry must not
    re-create project OR re-cut version; just retry the deploy."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    mock_setup.deploy_webapp.side_effect = RuntimeError("deploy timeout")
    with pytest.raises(RuntimeError, match="deploy timeout"):
        setup_apps_script_for_user(_fake_creds(), "user-1")

    mock_setup.deploy_webapp.side_effect = None
    mock_setup.deploy_webapp.return_value = _deployment(
        "SCRIPT_ID_NEW", "DEPLOY2", "https://script.google.com/macros/s/DEPLOY2/exec",
    )
    setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_setup.create_project.call_count == 1
    assert mock_setup.create_version.call_count == 1
    assert mock_setup.deploy_webapp.call_count == 2


def test_content_change_resets_user_ledger_starts_fresh(mock_setup, tmp_path):
    """Operator updated restructure.gs between runs (different content
    hash). The user's cached ledger must be discarded so they get a
    fresh deploy of the new content."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    assert mock_setup.create_project.call_count == 1

    # Simulate operator edit by pointing the source at a different file. The
    # fake source must still carry the HMAC key sentinel (every real
    # restructure.gs does) so the v2.0c key-injection step accepts it.
    fake_path = tmp_path / "edited_restructure.gs"
    fake_path.write_text(
        "// totally different content\nvar MCP_HMAC_KEY = '__MCP_HMAC_KEY__';"
    )
    with patch(
        "appscriptly.setup_apps_script.RESTRUCTURE_GS_PATH", fake_path,
    ):
        mock_setup.create_project.return_value = "SCRIPT_ID_FRESH"
        setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_setup.create_project.call_count == 2  # new project for new content


def test_manual_deletion_in_drive_triggers_fresh_deploy(mock_setup):
    """User manually deleted the Apps Script project in their Drive
    between runs. The cached script_id is now dead — detect via
    script_exists, blow ledger away, start fresh.

    Without this, the next push_files call would 404 against a
    nonexistent script and fail confusingly."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    assert mock_setup.create_project.call_count == 1

    mock_setup.script_exists.return_value = False
    mock_setup.create_project.return_value = "SCRIPT_ID_REBORN"
    setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_setup.create_project.call_count == 2
    # The push went to the NEW script_id, not the dead one.
    last_push_args = mock_setup.push_files.call_args_list[-1].args
    assert last_push_args[0] == "SCRIPT_ID_REBORN"


def test_clear_preserves_google_creds_json(mock_setup):
    """When the apps_script_* ledger is cleared (hash mismatch or
    manual delete), the user's google_creds_json must survive — those
    are independent OAuth tokens that the user already authorized."""
    from appscriptly import user_store
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    # Seed creds independently (as the OAuth callback would).
    user_store.save_state("user-1", {"google_creds_json": '{"token":"X"}'})

    # Run setup, then trigger a ledger reset by changing script_exists.
    setup_apps_script_for_user(_fake_creds(), "user-1")
    mock_setup.script_exists.return_value = False
    setup_apps_script_for_user(_fake_creds(), "user-1")

    # creds_json must still be there.
    state = user_store.get_state("user-1")
    assert state.get("google_creds_json") == '{"token":"X"}', (
        "ledger reset wiped google_creds_json — user would have to "
        "re-authorize OAuth on every setup retry"
    )


# ---------------------------------------------------------------
# Tool-level scope guard (Issue #17 — v1.x scope reduction)
# ---------------------------------------------------------------


def test_gdocs_setup_apps_script_tool_demands_script_scopes_when_missing(
    monkeypatch, mock_setup,
):
    """Regression guard for Issue #17 (v1.x scope reduction).

    After dropping ``script.projects`` + ``script.deployments`` from
    ``GOOGLE_API_SCOPES``, a user whose first-consent grant only
    covers the default Workspace scopes must NOT silently run the
    Apps Script setup — they'd hit a 403 on the first projects.create
    call and the failure mode would be confusing. Instead, the tool
    must surface a ``needs_authorization`` response so the user
    re-consents with the additional ``script.*`` scopes added via
    Google's ``include_granted_scopes`` (incremental authorization,
    no reset of existing grants).

    This test exercises the cloud (HTTP) path: a user with default
    scopes only, calling ``gdocs_setup_apps_script``, must get back
    ``status: "needs_authorization"`` with an ``auth_url`` that the
    UI surfaces as a clickable link. Setup must NOT proceed
    (``create_project`` etc. must not be called)."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    from appscriptly import user_store

    # Seed creds for a user with ONLY the post-reduction default scopes —
    # the script.* scopes are NOT present, mirroring real life after
    # Issue #17 lands.
    user_id = "user-narrow-scopes"
    payload = {
        "token": "STORED_ACCESS_TOKEN",
        "refresh_token": "STORED_REFRESH_TOKEN",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        "expiry": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
    }
    user_store.save_state(user_id, {"google_creds_json": _json.dumps(payload)})

    # Force the cloud-mode branch by making the tool see a user_id.
    # M3 Phase C (v2.1.5): gdocs_setup_apps_script now lives in
    # services/gas_deploy/tools.py — patches target that module's
    # namespace (where the tool reads its dependencies from), not
    # server.py.
    monkeypatch.setattr(
        "appscriptly.services.gas_deploy.tools.current_user_id_or_none",
        lambda: user_id,
    )

    # Provide a fake runtime_oauth bundle so the tool can hand it to
    # the credentials resolver.
    client_config = {
        "web": {
            "client_id": "CID.apps.googleusercontent.com",
            "client_secret": "CSECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                "https://example.fly.dev/oauth/google/api/callback",
            ],
        },
    }
    monkeypatch.setattr(
        "appscriptly.services.gas_deploy.tools.resolve_runtime_oauth_config",
        lambda: {
            "client_config": client_config,
            # v2.0b: resolve_runtime_oauth_config returns signing_key
            # as bytes (matches keys.get_key("oauth_state") return
            # type post-strict-flip).
            "signing_key": b"test-signing-key",
            "base_url": "https://example.fly.dev",
        },
    )

    # M3 Phase C (v2.1.5): gdocs_setup_apps_script moved from server.py
    # to services/gas_deploy/tools.py per the per-service folder pattern.
    from appscriptly.services.gas_deploy.tools import gdocs_setup_apps_script

    result = gdocs_setup_apps_script()

    assert result["status"] == "needs_authorization", (
        "Tool should bounce to re-auth when creds lack script.* scopes; "
        f"got status={result.get('status')!r}"
    )
    assert "auth_url" in result and result["auth_url"], (
        "needs_authorization response must include an auth_url so the "
        "user can click through to consent"
    )
    # Setup work MUST NOT have proceeded — no projects created, no
    # files pushed, no deploys.
    assert mock_setup.create_project.call_count == 0, (
        "Tool ran setup despite missing script.* scopes — incremental "
        "authorization guard regressed"
    )
    assert mock_setup.push_files.call_count == 0
    assert mock_setup.deploy_webapp.call_count == 0


# Helpers
def _deployment(script_id: str, deployment_id: str, url: str):
    from appscriptly.services.gas_deploy.api import WebAppDeployment
    return WebAppDeployment(
        script_id=script_id,
        deployment_id=deployment_id,
        version=1,
        url=url,
    )
