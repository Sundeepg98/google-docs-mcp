"""Tests for services/apps_script/calendar_sync.py (GAS parity — Calendar).

``as_install_calendar_sync`` is the Calendar analogue of
``as_install_sheet_dashboard``: a USE-CASE tool composing the PR-Δ7
bound-script primitive into a TIME-DRIVEN "create/sync Calendar events from
Sheet rows" automation bound to a Google Sheet. The deploy + body-synthesis
machinery is REUSED (from api.py and sheet_dashboard.py respectively, both
already covered by their own tests); this file covers THIS module's own
contributions:

  * the GENERATED manifest declares the full ``calendar`` scope (the
    load-bearing scope guard) AND the time-trigger ``script.scriptapp``;
  * appscriptly's OWN consent gains NO new scope from this tool;
  * the honest trigger-activation state + ``manifest_scope`` in the return;
  * the tool happy-path end-to-end at the @workspace_tool boundary;
  * input validation (schedule / hour / non-empty-named body) → error.

Fixture pattern mirrors ``test_sheet_dashboard.py``: this tool DECLARES
``scopes=GAS_BOUND_SCOPES`` so the @workspace_tool(creds=True) envelope
takes the SCOPE-AWARE path (``auth.load_credentials`` in stdio test mode).
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
from appscriptly.services.apps_script import calendar_sync

_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# A representative caller-supplied Sheet -> Calendar sync function body.
_SYNC_FN = (
    "function syncEvents() {\n"
    "  var cal = CalendarApp.getDefaultCalendar();\n"
    "  var rows = SpreadsheetApp.getActiveSpreadsheet()\n"
    "    .getSheetByName('Schedule').getDataRange().getValues();\n"
    "  for (var i = 1; i < rows.length; i++) {\n"
    "    cal.createEvent(rows[i][0], new Date(rows[i][1]), new Date(rows[i][2]));\n"
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
    monkeypatch.setattr(calendar_sync, "_get_credentials", lambda: stub_creds)


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
    """Apps Script stub for create→push→deploy. No Drive stub — the tool
    binds directly to the Sheet ID (no mimeType detection round-trip)."""
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
# Tool happy-path
# =====================================================================


def test_happy_path_returns_envelope(with_sheet_container):
    result = calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN, schedule="daily", hour=6,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["sheet_id"] == "SHEET-1"
    assert result["schedule"] == "daily"
    assert result["trigger_handler"] == "syncEvents"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert result["manifest_scope"] == _CALENDAR_SCOPE


def test_reports_honest_trigger_state(with_sheet_container):
    result = calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN,
    )
    assert result["trigger_active"] is False
    assert result["activation_required"] is True
    assert "installTrigger" in result["activation_instructions"]


def test_binds_via_parent_id(with_sheet_container):
    calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN,
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "SHEET-1"


def test_pushes_synthesized_body_with_handler_and_install_trigger(
    with_sheet_container,
):
    calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN,
    )
    source = _pushed_code_source(with_sheet_container)
    assert "function syncEvents()" in source
    assert "CalendarApp.getDefaultCalendar()" in source
    assert "function installTrigger()" in source


def test_body_maps_schedule_into_trigger(with_sheet_container):
    calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN, schedule="weekly", hour=8,
    )
    source = _pushed_code_source(with_sheet_container)
    assert ".onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(8)" in source


def test_default_name_includes_schedule(with_sheet_container):
    calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN, schedule="hourly",
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert "hourly" in body_calls[-1].kwargs["body"]["title"]


def test_uses_custom_name_when_given(with_sheet_container):
    calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN, name="Roster Pusher",
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "Roster Pusher"


# =====================================================================
# SCOPE GUARD (load-bearing) — calendar scope in GENERATED manifest only
# =====================================================================


def test_generated_manifest_declares_calendar_and_trigger_scopes(
    with_sheet_container,
):
    """CalendarApp needs the full calendar scope; a time trigger needs
    script.scriptapp. BOTH must land in the GENERATED bound script's
    manifest oauthScopes (and the internal __plan__ echo stripped)."""
    calendar_sync.as_install_calendar_sync(
        sheet_id="SHEET-1", sync_function_body=_SYNC_FN,
    )
    manifest = _pushed_manifest(with_sheet_container)
    assert _CALENDAR_SCOPE in manifest["oauthScopes"]
    assert _TRIGGER_SCOPE in manifest["oauthScopes"]
    # Observability (gap #5): the failure reporter's send-only mail scope.
    assert (
        "https://www.googleapis.com/auth/script.send_mail"
        in manifest["oauthScopes"]
    )
    assert "__plan__" not in manifest


def test_tool_declares_only_baseline_gas_scopes_not_calendar():
    """The TOOL itself declares only GAS_BOUND_SCOPES for appscriptly's own
    consent — NOT a tool-level calendar declaration. The calendar scope is
    the bound script's (generated manifest), surfaced on the machine-
    readable tool.annotations.scopes."""
    import asyncio

    from appscriptly.server import mcp
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == "as_install_calendar_sync")
    declared = list(getattr(tool.annotations, "scopes", []) or [])
    assert declared, "as_install_calendar_sync must declare its scopes"
    assert set(declared) == set(GAS_BOUND_SCOPES)
    assert _CALENDAR_SCOPE not in declared


# =====================================================================
# Validation errors (rejected before any API call)
# =====================================================================


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_rejects_empty_sheet_id(with_sheet_container, bad_id):
    with pytest.raises(ValueError, match="sheet_id cannot be empty"):
        calendar_sync.as_install_calendar_sync(
            sheet_id=bad_id, sync_function_body=_SYNC_FN,
        )
    create_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_calls


def test_rejects_invalid_schedule(with_sheet_container):
    with pytest.raises(ValueError, match="schedule must be one of"):
        calendar_sync.as_install_calendar_sync(
            sheet_id="SHEET-1", sync_function_body=_SYNC_FN, schedule="monthly",
        )


@pytest.mark.parametrize("bad_hour", [-1, 24, 99])
def test_rejects_out_of_range_hour(with_sheet_container, bad_hour):
    with pytest.raises(ValueError, match="hour must be an integer 0-23"):
        calendar_sync.as_install_calendar_sync(
            sheet_id="SHEET-1", sync_function_body=_SYNC_FN, hour=bad_hour,
        )


def test_rejects_empty_sync_body(with_sheet_container):
    with pytest.raises(ValueError, match="sync_function_body cannot be empty"):
        calendar_sync.as_install_calendar_sync(
            sheet_id="SHEET-1", sync_function_body="   ",
        )


def test_rejects_unnamed_sync_body(with_sheet_container):
    with pytest.raises(ValueError, match="NAMED function declaration"):
        calendar_sync.as_install_calendar_sync(
            sheet_id="SHEET-1",
            sync_function_body="const f = () => { sync(); };",
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
            calendar_sync.as_install_calendar_sync(
                sheet_id="SHEET-1", sync_function_body=_SYNC_FN,
            )
