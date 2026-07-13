"""Tests for services/apps_script/sheet_dashboard.py (PR-Δ9).

``as_install_sheet_dashboard`` is the first USE-CASE tool composing the
PR-Δ7 bound-script primitive: it installs a time-driven dashboard-refresh
automation into a Google Sheet. The deploy machinery (create / push /
deploy) is REUSED from the primitive's api.py and is already covered by
``test_api.py``; this file covers THIS module's own contributions:

  * pure ``.gs`` body synthesis — the caller's refresh function +
    a generated ``installTrigger()`` with dedup-then-create logic;
  * the schedule → trigger-builder mapping (daily / hourly / weekly);
  * handler-name extraction from the refresh function body;
  * the derived manifest scope (a time trigger ⇒ ``script.scriptapp``);
  * input validation (schedule / hour / non-empty-named body) → error;
  * the tool happy-path end-to-end at the @workspace_tool boundary,
    including the HONEST trigger-activation state in the return payload.

Fixture pattern mirrors ``test_tools.py``: this tool DECLARES
``scopes=GAS_BOUND_SCOPES``, so the @workspace_tool(creds=True) decorator
takes the SCOPE-AWARE resolution path (``auth.load_credentials`` in stdio
test mode) — we patch THAT, not ``_get_credentials_fn``. The Drive +
Apps Script HTTP boundaries are stubbed via ``InMemoryGoogleAPIClient``.
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
from appscriptly.services.apps_script import sheet_dashboard
from appscriptly.services.apps_script.sheet_dashboard import (
    _extract_handler_name,
    _trigger_builder_expr,
    build_dashboard_script_body,
)

_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# A representative caller-supplied refresh function body.
_REFRESH_FN = (
    "function refreshDashboard() {\n"
    "  var ss = SpreadsheetApp.getActiveSpreadsheet();\n"
    "  ss.getSheetByName('Dashboard').getRange('A1').setValue(new Date());\n"
    "}"
)


# ---------------------------------------------------------------------
# Creds fixture — patch the SCOPE-AWARE path (this tool declares scopes)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=GAS_BOUND_SCOPES) envelope doesn't
    launch real OAuth. This tool declares scopes, so resolution flows
    through ``auth.load_credentials`` (stdio mode) — patch that. The other
    two patches cover the no-scope path too (belt-and-suspenders)."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(
        sheet_dashboard, "_get_credentials", lambda: stub_creds
    )


def _make_script_stub() -> MagicMock:
    """An Apps Script v1 stub wired for create→push→deploy."""
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
    """Apps Script stub wired for the full create→push→deploy flow. No
    Drive stub needed — the tool binds directly to the Sheet ID without a
    mimeType detection round-trip (it only ever targets a Sheet)."""
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


def _last_pushed_files(script: MagicMock) -> list[dict]:
    """The files list from the most recent updateContent call."""
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    return body_calls[-1].kwargs["body"]["files"]


def _pushed_code_source(script: MagicMock) -> str:
    """The SERVER_JS source from the most recent updateContent push."""
    files = _last_pushed_files(script)
    return next(f for f in files if f["type"] == "SERVER_JS")["source"]


def _pushed_manifest(script: MagicMock) -> dict:
    files = _last_pushed_files(script)
    return json.loads(next(f for f in files if f["type"] == "JSON")["source"])


# =====================================================================
# Pure helpers: handler extraction
# =====================================================================


def test_extract_handler_name_parses_function_name():
    assert _extract_handler_name(_REFRESH_FN) == "refreshDashboard"


def test_extract_handler_name_handles_dollar_and_underscore_idents():
    body = "function _rebuild$Tab(){ return 1; }"
    assert _extract_handler_name(body) == "_rebuild$Tab"


def test_extract_handler_name_rejects_unnamed_function():
    """An arrow function / bare expression has no name → ValueError (it
    can't be a ScriptApp.newTrigger handler)."""
    with pytest.raises(ValueError, match="NAMED function declaration"):
        _extract_handler_name("const f = () => { doStuff(); };")


# =====================================================================
# Pure helpers: schedule → trigger-builder mapping
# =====================================================================


def test_trigger_builder_daily_uses_every_days_at_hour():
    expr = _trigger_builder_expr("daily", 6)
    assert ".everyDays(1)" in expr
    assert ".atHour(6)" in expr
    assert expr.strip().endswith(".create();")


def test_trigger_builder_hourly_uses_every_hours_and_ignores_hour():
    expr = _trigger_builder_expr("hourly", 9)
    assert ".everyHours(1)" in expr
    # Hourly has no hour-of-day — atHour must NOT appear.
    assert "atHour" not in expr


def test_trigger_builder_weekly_uses_on_week_day_monday_at_hour():
    expr = _trigger_builder_expr("weekly", 8)
    assert ".onWeekDay(ScriptApp.WeekDay.MONDAY)" in expr
    assert ".atHour(8)" in expr


def test_trigger_builder_rejects_unknown_schedule():
    with pytest.raises(ValueError, match="schedule must be one of"):
        _trigger_builder_expr("monthly", 6)


# =====================================================================
# Pure helper: full script-body synthesis
# =====================================================================


def test_build_script_body_includes_refresh_function_verbatim():
    body, handler = build_dashboard_script_body(_REFRESH_FN, "daily", 6)
    assert handler == "refreshDashboard"
    # The caller's function survives intact in the assembled body.
    assert "function refreshDashboard()" in body
    assert "SpreadsheetApp.getActiveSpreadsheet()" in body


def test_build_script_body_defines_install_trigger_function():
    body, _ = build_dashboard_script_body(_REFRESH_FN, "daily", 6)
    assert "function installTrigger()" in body
    # It wires the trigger to OUR handler by name.
    assert 'ScriptApp.newTrigger(handlerName)' in body
    assert 'var handlerName = "refreshDashboard"' in body


def test_build_script_body_dedupes_existing_triggers_before_create():
    """The classic Apps Script footgun is stacking duplicate triggers on
    re-run. installTrigger must DELETE existing triggers for the same
    handler before creating the new one."""
    body, _ = build_dashboard_script_body(_REFRESH_FN, "daily", 6)
    assert "ScriptApp.getProjectTriggers()" in body
    assert ".getHandlerFunction()" in body
    assert "ScriptApp.deleteTrigger(" in body
    # Dedup precedes create: the delete loop appears before newTrigger.
    assert body.index("deleteTrigger") < body.index("newTrigger(handlerName)")


def test_build_script_body_embeds_dashboard_note_as_comment():
    body, _ = build_dashboard_script_body(
        _REFRESH_FN, "daily", 6, dashboard_note="KPI rollup tab"
    )
    assert "// KPI rollup tab" in body


def test_build_script_body_note_cannot_break_out_of_comment():
    """A note containing */ must be neutralized so it can't terminate a
    block comment / inject code."""
    body, _ = build_dashboard_script_body(
        _REFRESH_FN, "daily", 6, dashboard_note="evil */ doEvil();"
    )
    assert "*/ doEvil();" not in body
    # The close-comment sequence is broken.
    assert "* /" in body


def test_build_script_body_maps_schedule_into_trigger():
    """The synthesized body carries the schedule-specific builder tail."""
    daily, _ = build_dashboard_script_body(_REFRESH_FN, "daily", 7)
    hourly, _ = build_dashboard_script_body(_REFRESH_FN, "hourly", 7)
    weekly, _ = build_dashboard_script_body(_REFRESH_FN, "weekly", 7)
    assert ".everyDays(1).atHour(7)" in daily
    assert ".everyHours(1)" in hourly
    assert ".onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(7)" in weekly


# =====================================================================
# Tool happy-path (end-to-end at the @workspace_tool boundary)
# =====================================================================


def test_install_dashboard_happy_path_returns_envelope(with_sheet_container):
    result = sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1",
        refresh_function_body=_REFRESH_FN,
        schedule="daily",
        hour=6,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["sheet_id"] == "SHEET-1"
    assert result["schedule"] == "daily"
    assert result["trigger_handler"] == "refreshDashboard"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"


def test_install_dashboard_reports_honest_trigger_state(with_sheet_container):
    """The deploy wires but does NOT activate the trigger — the payload
    must say so (trigger_active False, activation_required True, with an
    instruction). This is the load-bearing honesty caveat."""
    result = sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
    )
    assert result["trigger_active"] is False
    assert result["activation_required"] is True
    assert "installTrigger" in result["activation_instructions"]


def test_install_dashboard_returns_unified_activation_contract(
    with_sheet_container,
):
    """Stream 3: the legacy trigger_active alias survives AND the unified
    activation_* fields are present (activation_url = editor root, no
    function-level deep link exists; activation_function names installTrigger)."""
    result = sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
    )
    # Legacy alias preserved (back-compat).
    assert result["trigger_active"] is False
    # Unified canonical fields (build_activation_fields).
    assert result["activation_required"] is True
    assert result["activation_function"] == "installTrigger"
    assert result["activation_url"] == result["project_url"]
    assert result["activation_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert "installTrigger" in result["activation_instructions"]


def test_install_dashboard_binds_via_parent_id(with_sheet_container):
    """The create call must bind to the Sheet via parentId=sheet_id."""
    sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "SHEET-1"


def test_install_dashboard_pushes_synthesized_body(with_sheet_container):
    """The pushed .gs body carries BOTH the refresh function and the
    generated installTrigger — proving synthesis flows into the deploy."""
    sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
    )
    source = _pushed_code_source(with_sheet_container)
    assert "function refreshDashboard()" in source
    assert "function installTrigger()" in source


def test_install_dashboard_manifest_declares_trigger_scope(with_sheet_container):
    """A time trigger ⇒ the manifest must declare script.scriptapp (the
    one manifest-relevant thing for a trigger, per the #138 finding). And
    the internal __plan__ echo must be stripped from the real manifest."""
    sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
    )
    manifest = _pushed_manifest(with_sheet_container)
    assert _TRIGGER_SCOPE in manifest["oauthScopes"]
    assert "__plan__" not in manifest


def test_install_dashboard_default_name_includes_schedule(with_sheet_container):
    sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN, schedule="weekly",
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert "weekly" in body_calls[-1].kwargs["body"]["title"]


def test_install_dashboard_uses_custom_name_when_given(with_sheet_container):
    sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
        name="My KPI Refresher",
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "My KPI Refresher"


# =====================================================================
# Validation errors (rejected before any API call)
# =====================================================================


def test_install_dashboard_rejects_invalid_schedule(with_sheet_container):
    with pytest.raises(ValueError, match="schedule must be one of"):
        sheet_dashboard.as_install_sheet_dashboard(
            sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
            schedule="monthly",
        )
    # No project should have been created — validation came first.
    create_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_calls


@pytest.mark.parametrize("bad_hour", [-1, 24, 99])
def test_install_dashboard_rejects_out_of_range_hour(with_sheet_container, bad_hour):
    with pytest.raises(ValueError, match="hour must be an integer 0-23"):
        sheet_dashboard.as_install_sheet_dashboard(
            sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN, hour=bad_hour,
        )


def test_install_dashboard_rejects_empty_refresh_body(with_sheet_container):
    with pytest.raises(ValueError, match="refresh_function_body cannot be empty"):
        sheet_dashboard.as_install_sheet_dashboard(
            sheet_id="SHEET-1", refresh_function_body="   ",
        )


def test_install_dashboard_rejects_unnamed_refresh_body(with_sheet_container):
    """A non-empty but UNNAMED body (arrow fn) is rejected — no trigger
    handler name to wire."""
    with pytest.raises(ValueError, match="NAMED function declaration"):
        sheet_dashboard.as_install_sheet_dashboard(
            sheet_id="SHEET-1",
            refresh_function_body="const f = () => { rebuild(); };",
        )


# =====================================================================
# API error path → ToolError (standard creds=True envelope)
# =====================================================================


def test_install_dashboard_api_httperror_maps_to_tool_error():
    """An Apps Script HttpError on create → @workspace_tool envelope
    translates it to ToolError."""
    script = MagicMock(name="script-v1-stub-erroring")
    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            sheet_dashboard.as_install_sheet_dashboard(
                sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
            )


# =====================================================================
# Decorator-envelope cross-check: scope-aware creds resolution fires
# =====================================================================


def test_install_dashboard_resolves_creds_via_scope_aware_path(
    with_sheet_container, monkeypatch
):
    """Canary: because this tool DECLARES scopes, resolution flows through
    the scope-aware path (auth.load_credentials in stdio mode) with the
    tool's declared scopes threaded as extra_scopes."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    sheet_dashboard.as_install_sheet_dashboard(
        sheet_id="SHEET-1", refresh_function_body=_REFRESH_FN,
    )

    assert len(calls) == 1
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
