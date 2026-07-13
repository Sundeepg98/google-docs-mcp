"""Tests for services/apps_script/task_rollover.py (GAS parity — Tasks).

``as_install_task_rollover`` is a USE-CASE tool composing the PR-Δ7
bound-script primitive into a TIME-DRIVEN Tasks automation bound to a Google
Sheet, driven by the Tasks ADVANCED service (``Tasks.*``). The deploy +
body-synthesis machinery is REUSED (api.py + sheet_dashboard.py); this file
covers THIS module's own contributions:

  * the GENERATED manifest declares the full ``tasks`` scope (scope guard)
    AND the time-trigger ``script.scriptapp``;
  * the GENERATED manifest ENABLES the Tasks advanced service under
    ``dependencies.enabledAdvancedServices`` (the Tasks-specific piece —
    ``Tasks.*`` won't resolve at runtime without it);
  * appscriptly's OWN consent gains NO new scope;
  * the honest trigger-activation state + manifest_scope + advanced_service
    in the return;
  * input validation + the happy path at the @workspace_tool boundary;
  * the pure manifest-merge helper (idempotent, merges not overwrites).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.apps_script import task_rollover
from appscriptly.services.apps_script.task_rollover import (
    _with_tasks_advanced_service,
)

_TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# A representative caller-supplied roll-over function using the Tasks
# advanced service.
_TASK_FN = (
    "function rollOverTasks() {\n"
    "  var lists = Tasks.Tasklists.list().items || [];\n"
    "  var today = new Date().toISOString();\n"
    "  for (var l = 0; l < lists.length; l++) {\n"
    "    var tasks = Tasks.Tasks.list(lists[l].id, {showCompleted: false})"
    ".items || [];\n"
    "    for (var t = 0; t < tasks.length; t++) {\n"
    "      if (tasks[t].due && tasks[t].due < today) {\n"
    "        tasks[t].due = today;\n"
    "        Tasks.Tasks.update(tasks[t], lists[l].id, tasks[t].id);\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(task_rollover, "_get_credentials", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "SHEET-1",
    }
    script.projects().updateContent().execute.return_value = {}
    script.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEPLOY-1",
    }
    return script


@pytest.fixture
def with_sheet_container():
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


def _last_pushed_files(script: MagicMock) -> list[dict]:
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    return body_calls[-1].kwargs["body"]["files"]


def _pushed_code_source(script: MagicMock) -> str:
    files = _last_pushed_files(script)
    return next(f for f in files if f["type"] == "SERVER_JS")["source"]


def _pushed_manifest(script: MagicMock) -> dict:
    files = _last_pushed_files(script)
    return json.loads(next(f for f in files if f["type"] == "JSON")["source"])


# =====================================================================
# Pure helper: advanced-service manifest merge
# =====================================================================


def test_with_tasks_advanced_service_adds_dependency():
    merged = _with_tasks_advanced_service({"timeZone": "Etc/UTC"})
    services = merged["dependencies"]["enabledAdvancedServices"]
    assert any(
        s["serviceId"] == "tasks" and s["userSymbol"] == "Tasks"
        and s["version"] == "v1"
        for s in services
    )
    # original keys preserved
    assert merged["timeZone"] == "Etc/UTC"


def test_with_tasks_advanced_service_is_idempotent():
    once = _with_tasks_advanced_service({})
    twice = _with_tasks_advanced_service(once)
    assert (
        len(twice["dependencies"]["enabledAdvancedServices"])
        == len(once["dependencies"]["enabledAdvancedServices"])
        == 1
    )


def test_with_tasks_advanced_service_merges_existing_dependencies():
    existing = {
        "dependencies": {
            "enabledAdvancedServices": [
                {"userSymbol": "Sheets", "serviceId": "sheets", "version": "v4"}
            ]
        }
    }
    merged = _with_tasks_advanced_service(existing)
    services = merged["dependencies"]["enabledAdvancedServices"]
    ids = {s["serviceId"] for s in services}
    assert ids == {"sheets", "tasks"}


def test_with_tasks_advanced_service_does_not_mutate_input():
    src = {"timeZone": "Etc/UTC"}
    _with_tasks_advanced_service(src)
    assert "dependencies" not in src


# =====================================================================
# Tool happy-path
# =====================================================================


def test_happy_path_returns_envelope(with_sheet_container):
    result = task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN, schedule="daily", hour=6,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["sheet_id"] == "SHEET-1"
    assert result["schedule"] == "daily"
    assert result["trigger_handler"] == "rollOverTasks"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert result["manifest_scope"] == _TASKS_SCOPE
    assert result["advanced_service"] == "tasks"


def test_reports_honest_trigger_state(with_sheet_container):
    result = task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN,
    )
    assert result["trigger_active"] is False
    assert result["activation_required"] is True
    assert "installTrigger" in result["activation_instructions"]


def test_binds_via_parent_id(with_sheet_container):
    task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN,
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "SHEET-1"


def test_pushes_synthesized_body(with_sheet_container):
    task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN,
    )
    source = _pushed_code_source(with_sheet_container)
    assert "function rollOverTasks()" in source
    assert "Tasks.Tasklists.list()" in source
    assert "function installTrigger()" in source


def test_uses_custom_name_when_given(with_sheet_container):
    task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN, name="Daily Rollover",
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "Daily Rollover"


# =====================================================================
# SCOPE GUARD + advanced-service wiring (load-bearing)
# =====================================================================


def test_generated_manifest_declares_tasks_and_trigger_scopes(
    with_sheet_container,
):
    task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN,
    )
    manifest = _pushed_manifest(with_sheet_container)
    assert _TASKS_SCOPE in manifest["oauthScopes"]
    assert _TRIGGER_SCOPE in manifest["oauthScopes"]
    # Observability (gap #5): the failure reporter's send-only mail scope.
    assert (
        "https://www.googleapis.com/auth/script.send_mail"
        in manifest["oauthScopes"]
    )
    # N-S3V-1: the bound Sheet's .currentonly data scope so the rollover
    # handler can read this Sheet via SpreadsheetApp. The Tasks ADVANCED
    # service keeps the FULL tasks scope (.currentonly is not honored for
    # advanced services); an explicit oauthScopes block suppresses auto-detect.
    assert (
        "https://www.googleapis.com/auth/spreadsheets.currentonly"
        in manifest["oauthScopes"]
    )
    assert "__plan__" not in manifest


def test_generated_manifest_enables_tasks_advanced_service(with_sheet_container):
    """The Tasks-specific piece: the GENERATED manifest must enable the
    Tasks advanced service so ``Tasks.*`` resolves at runtime."""
    task_rollover.as_install_task_rollover(
        sheet_id="SHEET-1", task_function_body=_TASK_FN,
    )
    manifest = _pushed_manifest(with_sheet_container)
    services = manifest["dependencies"]["enabledAdvancedServices"]
    assert any(
        s["serviceId"] == "tasks" and s["userSymbol"] == "Tasks"
        for s in services
    )


def test_tool_declares_only_baseline_gas_scopes_not_tasks():
    import asyncio

    from appscriptly.server import mcp
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == "as_install_task_rollover")
    declared = list(getattr(tool.annotations, "scopes", []) or [])
    assert declared, "as_install_task_rollover must declare its scopes"
    assert set(declared) == set(GAS_BOUND_SCOPES)
    assert _TASKS_SCOPE not in declared


# =====================================================================
# Validation errors
# =====================================================================


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_rejects_empty_sheet_id(with_sheet_container, bad_id):
    with pytest.raises(ValueError, match="sheet_id cannot be empty"):
        task_rollover.as_install_task_rollover(
            sheet_id=bad_id, task_function_body=_TASK_FN,
        )
    create_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_calls


def test_rejects_invalid_schedule(with_sheet_container):
    with pytest.raises(ValueError, match="schedule must be one of"):
        task_rollover.as_install_task_rollover(
            sheet_id="SHEET-1", task_function_body=_TASK_FN, schedule="monthly",
        )


@pytest.mark.parametrize("bad_hour", [-1, 24])
def test_rejects_out_of_range_hour(with_sheet_container, bad_hour):
    with pytest.raises(ValueError, match="hour must be an integer 0-23"):
        task_rollover.as_install_task_rollover(
            sheet_id="SHEET-1", task_function_body=_TASK_FN, hour=bad_hour,
        )


def test_rejects_empty_task_body(with_sheet_container):
    with pytest.raises(ValueError, match="task_function_body cannot be empty"):
        task_rollover.as_install_task_rollover(
            sheet_id="SHEET-1", task_function_body="   ",
        )


def test_rejects_unnamed_task_body(with_sheet_container):
    with pytest.raises(ValueError, match="NAMED function declaration"):
        task_rollover.as_install_task_rollover(
            sheet_id="SHEET-1",
            task_function_body="const f = () => { roll(); };",
        )


# =====================================================================
# API error path → ToolError
# =====================================================================


def test_api_httperror_maps_to_tool_error():
    script = MagicMock(name="script-v1-stub-erroring")
    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            task_rollover.as_install_task_rollover(
                sheet_id="SHEET-1", task_function_body=_TASK_FN,
            )
