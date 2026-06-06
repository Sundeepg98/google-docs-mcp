"""Setup idempotency / orphan-prevention tests.

Guards the v1.0.1 fix: setup-apps-script-auto must NOT create a second
Apps Script project on retry after a partial failure, and MUST resume
from the first incomplete step.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------
# Pure-state-module tests (no Apps Script API involved)
# ---------------------------------------------------------------


def test_content_hash_stable_across_calls():
    from appscriptly.setup_state import compute_content_hash

    manifest = {"timeZone": "Etc/GMT", "webapp": {"executeAs": "USER_DEPLOYING"}}
    files = {"Code": "function doPost(e) { return e; }"}
    a = compute_content_hash(manifest, files)
    b = compute_content_hash(manifest, files)
    assert a == b
    assert len(a) == 64  # SHA-256 hex


def test_content_hash_changes_when_script_edited():
    from appscriptly.setup_state import compute_content_hash

    manifest = {"timeZone": "Etc/GMT"}
    a = compute_content_hash(manifest, {"Code": "v1"})
    b = compute_content_hash(manifest, {"Code": "v2"})
    assert a != b


def test_state_matches_target_requires_both_hash_and_impersonate():
    from appscriptly.setup_state import state_matches_target

    state = {"content_hash": "H", "impersonate": "user@example.com"}
    assert state_matches_target(state, "H", "user@example.com")
    assert not state_matches_target(state, "DIFFERENT", "user@example.com")
    assert not state_matches_target(state, "H", "other@example.com")
    assert not state_matches_target(state, "H", None)
    assert not state_matches_target({}, "H", "user@example.com")


def test_state_persistence_roundtrip(tmp_path):
    from appscriptly.setup_state import load_state, save_state, state_path

    assert load_state(tmp_path) == {}
    save_state(tmp_path, {"content_hash": "abc", "script_id": "S"})
    assert state_path(tmp_path).exists()
    assert load_state(tmp_path) == {"content_hash": "abc", "script_id": "S"}


# ---------------------------------------------------------------
# End-to-end orchestration tests with a mocked AppsScriptClient.
# These are the actual regression guards for the "ghost script" bug.
# ---------------------------------------------------------------


@pytest.fixture
def mock_setup(tmp_path):
    """Mock AppsScriptClient + creds, isolate state in tmp_path."""
    with (
        patch("appscriptly.setup_apps_script.load_credentials") as load_oauth,
        patch("appscriptly.setup_apps_script.AppsScriptClient") as client_class,
        patch("appscriptly.setup_apps_script.config") as cfg_mod,
    ):
        load_oauth.return_value = MagicMock()
        client = MagicMock()
        client_class.return_value = client
        cfg_mod.load.return_value = {}
        cfg_mod.save.return_value = None

        # Sensible default returns for a cold-start happy path.
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
        yield {
            "client": client,
            "client_class": client_class,
            "data_dir": tmp_path,
        }


def test_cold_start_creates_project_once(mock_setup):
    from appscriptly.setup_apps_script import setup_apps_script_auto

    setup_apps_script_auto(data_dir=mock_setup["data_dir"])
    assert mock_setup["client"].create_project.call_count == 1


def test_second_run_with_same_content_does_NOT_create_a_second_project(mock_setup):
    """THE BUG GUARD: the agent's specific concern. Running setup twice
    without anything changing must NOT create a second Apps Script project.
    Without the ledger, the second run would call create_project again
    and orphan the first project in the user's Drive.
    """
    from appscriptly.setup_apps_script import setup_apps_script_auto

    setup_apps_script_auto(data_dir=mock_setup["data_dir"])
    setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    # The killer assertion. Pre-fix this was 2.
    assert mock_setup["client"].create_project.call_count == 1, (
        "Second setup call created a SECOND Apps Script project — "
        "the ghost-script bug. Ledger isn't catching the prior run."
    )
    # Same for downstream steps — none should be re-run.
    assert mock_setup["client"].push_files.call_count == 1
    assert mock_setup["client"].create_version.call_count == 1
    assert mock_setup["client"].deploy_webapp.call_count == 1


def test_resume_after_failure_at_push_files_step(mock_setup):
    """Partial failure: create_project succeeded, push_files raised.
    Retry must skip create_project (use cached script_id) and continue
    from push_files. NO orphan project created.
    """
    from appscriptly.setup_apps_script import setup_apps_script_auto

    # First attempt: push_files fails after create_project succeeds.
    mock_setup["client"].push_files.side_effect = RuntimeError("network blip")
    with pytest.raises(RuntimeError, match="network blip"):
        setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    # Heal the failure; retry.
    mock_setup["client"].push_files.side_effect = None
    setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    # Crucial: only ONE create_project across both attempts.
    assert mock_setup["client"].create_project.call_count == 1
    # And push_files was retried after the failure.
    assert mock_setup["client"].push_files.call_count == 2


def test_resume_after_failure_at_deploy_webapp_step(mock_setup):
    """deploy_webapp fails after version is cut. Retry must not re-create
    project OR re-cut version — both are cached. Just retry the deploy.
    """
    from appscriptly.setup_apps_script import setup_apps_script_auto

    mock_setup["client"].deploy_webapp.side_effect = RuntimeError("deploy timeout")
    with pytest.raises(RuntimeError, match="deploy timeout"):
        setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    from appscriptly.services.gas_deploy.api import WebAppDeployment
    mock_setup["client"].deploy_webapp.side_effect = None
    mock_setup["client"].deploy_webapp.return_value = WebAppDeployment(
        script_id="SCRIPT_ID_NEW", deployment_id="DEPLOY2", version=1,
        url="https://script.google.com/macros/s/DEPLOY2/exec",
    )
    setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    assert mock_setup["client"].create_project.call_count == 1
    assert mock_setup["client"].create_version.call_count == 1
    assert mock_setup["client"].deploy_webapp.call_count == 2


def test_content_change_starts_fresh(mock_setup):
    """If restructure.gs is edited between runs (different content hash),
    the cached state must be discarded — we deploy a NEW project for
    the new content."""
    from appscriptly.setup_apps_script import setup_apps_script_auto

    # First run completes happily.
    setup_apps_script_auto(data_dir=mock_setup["data_dir"])
    assert mock_setup["client"].create_project.call_count == 1

    # Simulate edited content by patching the source path.
    fake_path = mock_setup["data_dir"] / "edited.gs"
    fake_path.write_text("// totally different content")
    with patch(
        "appscriptly.setup_apps_script.RESTRUCTURE_GS_PATH", fake_path
    ):
        mock_setup["client"].create_project.return_value = "SCRIPT_ID_FRESH"
        setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    # New project created for the new content (this is the right call —
    # different source = different deploy).
    assert mock_setup["client"].create_project.call_count == 2


def test_manual_deletion_recovery(mock_setup):
    """User manually deletes the Apps Script from their Drive between
    runs. The cached script_id is dead. Next run must detect this
    (via script_exists check) and start fresh — NOT push files to a
    project that no longer exists.
    """
    from appscriptly.setup_apps_script import setup_apps_script_auto

    setup_apps_script_auto(data_dir=mock_setup["data_dir"])
    assert mock_setup["client"].create_project.call_count == 1

    # Simulate user manual-delete: script_exists now returns False.
    mock_setup["client"].script_exists.return_value = False
    mock_setup["client"].create_project.return_value = "SCRIPT_ID_REBORN"
    setup_apps_script_auto(data_dir=mock_setup["data_dir"])

    # A new project was created since the old one's gone.
    assert mock_setup["client"].create_project.call_count == 2
    # AND the push went to the NEW script_id, not the dead one.
    last_push_call = mock_setup["client"].push_files.call_args_list[-1]
    assert last_push_call.args[0] == "SCRIPT_ID_REBORN"
