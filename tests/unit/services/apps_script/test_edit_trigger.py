"""Tests for services/apps_script/edit_trigger.py (ROADMAP_SPECS #8).

``as_install_edit_trigger`` is a USE-CASE tool composing the PR-Δ7
bound-script primitive: it installs a reactive **installable** ``onEdit``
trigger bound to a Google Sheet. The deploy machinery (create / push /
deploy) is REUSED from the primitive's api.py and is already covered by
``test_api.py``; this file covers THIS module's own contributions:

  * pure ``.gs`` body synthesis — the caller's handler function +
    a generated ``installTrigger()`` with dedup-then-create logic wiring
    ``ScriptApp.newTrigger(h).forSpreadsheet(id).onEdit().create()``;
  * handler-name extraction from the handler function body;
  * the derived manifest scope (an installable onEdit trigger ⇒
    ``script.scriptapp``, supplied via oauth_scopes);
  * input validation (empty sheet_id / empty-or-unnamed body) → error;
  * the tool happy-path end-to-end at the @workspace_tool boundary,
    including the HONEST trigger-activation state in the return payload.

Fixture pattern mirrors ``test_sheet_dashboard.py``: this tool DECLARES
``scopes=GAS_BOUND_SCOPES``, so the @workspace_tool(creds=True) decorator
takes the SCOPE-AWARE resolution path (``auth.load_credentials`` in stdio
test mode) — we patch THAT, not ``_get_credentials_fn``. The Drive + Apps
Script HTTP boundaries are stubbed via ``InMemoryGoogleAPIClient``.
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
from appscriptly.services.apps_script import edit_trigger
from appscriptly.services.apps_script.edit_trigger import (
    _extract_handler_name,
    build_edit_trigger_script_body,
)

_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# A representative caller-supplied onEdit handler body.
_HANDLER_FN = (
    "function onSheetEdit(e) {\n"
    "  var range = e.range;\n"
    "  range.getSheet().getRange('Z1').setValue(new Date());\n"
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
    monkeypatch.setattr(edit_trigger, "_get_credentials", lambda: stub_creds)


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
# Pure helper: handler extraction
# =====================================================================


def test_extract_handler_name_parses_function_name():
    assert _extract_handler_name(_HANDLER_FN) == "onSheetEdit"


def test_extract_handler_name_handles_dollar_and_underscore_idents():
    body = "function _react$Edit(e){ return 1; }"
    assert _extract_handler_name(body) == "_react$Edit"


def test_extract_handler_name_rejects_unnamed_function():
    """An arrow function / bare expression has no name → ValueError (it
    can't be a ScriptApp.newTrigger handler)."""
    with pytest.raises(ValueError, match="NAMED function declaration"):
        _extract_handler_name("const f = (e) => { doStuff(e); };")


# =====================================================================
# Pure helper: full script-body synthesis
# =====================================================================


def test_build_script_body_includes_handler_function_verbatim():
    body, handler = build_edit_trigger_script_body(_HANDLER_FN, "SHEET-1")
    assert handler == "onSheetEdit"
    # The caller's function survives intact in the assembled body.
    assert "function onSheetEdit(e)" in body
    assert "e.range" in body


def test_build_script_body_defines_install_trigger_function():
    body, _ = build_edit_trigger_script_body(_HANDLER_FN, "SHEET-1")
    assert "function installTrigger()" in body
    # It wires the trigger to OUR handler by name. The handler is now a
    # guarded wrapper (observability: emails the owner on failure, then
    # rethrows) that delegates to the caller's onSheetEdit.
    assert "ScriptApp.newTrigger(handlerName)" in body
    assert 'var handlerName = "__appscriptlyGuarded_onSheetEdit__"' in body
    assert "return onSheetEdit(e);" in body


def test_build_script_body_wires_for_spreadsheet_on_edit():
    """The synthesized installTrigger must bind the handler to THIS sheet
    via forSpreadsheet(id).onEdit().create() — the load-bearing shape from
    the spec."""
    body, _ = build_edit_trigger_script_body(_HANDLER_FN, "SHEET-XYZ")
    assert ".forSpreadsheet(" in body
    assert '"SHEET-XYZ"' in body
    assert ".onEdit()" in body
    assert ".create();" in body


def test_build_script_body_dedupes_existing_triggers_before_create():
    """The classic Apps Script footgun is stacking duplicate triggers on
    re-run. installTrigger must DELETE existing triggers for the same
    handler before creating the new one."""
    body, _ = build_edit_trigger_script_body(_HANDLER_FN, "SHEET-1")
    assert "ScriptApp.getProjectTriggers()" in body
    assert ".getHandlerFunction()" in body
    assert "ScriptApp.deleteTrigger(" in body
    # Dedup precedes create: the delete loop appears before newTrigger.
    assert body.index("deleteTrigger") < body.index("newTrigger(handlerName)")


def test_build_script_body_embeds_handler_note_as_comment():
    body, _ = build_edit_trigger_script_body(
        _HANDLER_FN, "SHEET-1", handler_note="audit-stamp on edit"
    )
    assert "// audit-stamp on edit" in body


def test_build_script_body_note_cannot_break_out_of_comment():
    """A note containing */ must be neutralized so it can't terminate a
    block comment / inject code."""
    body, _ = build_edit_trigger_script_body(
        _HANDLER_FN, "SHEET-1", handler_note="evil */ doEvil();"
    )
    assert "*/ doEvil();" not in body
    assert "* /" in body


def test_build_script_body_sheet_id_with_quote_is_escaped():
    """A sheet ID containing a quote can't break out of the JS string
    literal in forSpreadsheet(...)."""
    body, _ = build_edit_trigger_script_body(_HANDLER_FN, 'a"b')
    # The embedded quote is backslash-escaped, so the raw unescaped form
    # never appears inside the forSpreadsheet call.
    assert 'forSpreadsheet("a\\"b")' in body


# =====================================================================
# Tool happy-path (end-to-end at the @workspace_tool boundary)
# =====================================================================


def test_install_edit_trigger_happy_path_returns_envelope(with_sheet_container):
    result = edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1",
        handler_function_body=_HANDLER_FN,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["sheet_id"] == "SHEET-1"
    assert result["trigger_type"] == "onEdit"
    assert result["trigger_handler"] == "onSheetEdit"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"


def test_install_edit_trigger_reports_honest_trigger_state(with_sheet_container):
    """The deploy wires but does NOT activate the trigger — the payload
    must say so (trigger_active False, activation_required True, with an
    instruction). This is the load-bearing honesty caveat."""
    result = edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
    )
    assert result["trigger_active"] is False
    assert result["activation_required"] is True
    assert "installTrigger" in result["activation_instructions"]


def test_install_edit_trigger_binds_via_parent_id(with_sheet_container):
    """The create call must bind to the Sheet via parentId=sheet_id."""
    edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "SHEET-1"


def test_install_edit_trigger_pushes_synthesized_body(with_sheet_container):
    """The pushed .gs body carries BOTH the handler function and the
    generated installTrigger — proving synthesis flows into the deploy."""
    edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
    )
    source = _pushed_code_source(with_sheet_container)
    assert "function onSheetEdit(e)" in source
    assert "function installTrigger()" in source
    assert ".forSpreadsheet(" in source
    assert ".onEdit()" in source


def test_install_edit_trigger_manifest_declares_trigger_scope(with_sheet_container):
    """An installable onEdit trigger ⇒ the manifest must declare
    script.scriptapp (the one manifest-relevant thing for an installable
    trigger). And the internal __plan__ echo must be stripped from the
    real manifest."""
    edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
    )
    manifest = _pushed_manifest(with_sheet_container)
    assert _TRIGGER_SCOPE in manifest["oauthScopes"]
    # Observability (gap #5): the failure reporter's send-only mail scope.
    assert (
        "https://www.googleapis.com/auth/script.send_mail"
        in manifest["oauthScopes"]
    )
    assert "__plan__" not in manifest


def test_install_edit_trigger_uses_custom_name_when_given(with_sheet_container):
    edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
        name="My Edit Reactor",
    )
    body_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "My Edit Reactor"


# =====================================================================
# Validation errors (rejected before any API call)
# =====================================================================


def test_install_edit_trigger_rejects_empty_sheet_id(with_sheet_container):
    with pytest.raises(ValueError, match="sheet_id cannot be empty"):
        edit_trigger.as_install_edit_trigger(
            sheet_id="   ", handler_function_body=_HANDLER_FN,
        )
    create_calls = [
        c for c in with_sheet_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_calls


def test_install_edit_trigger_rejects_empty_handler_body(with_sheet_container):
    with pytest.raises(ValueError, match="handler_function_body cannot be empty"):
        edit_trigger.as_install_edit_trigger(
            sheet_id="SHEET-1", handler_function_body="   ",
        )


def test_install_edit_trigger_rejects_unnamed_handler_body(with_sheet_container):
    """A non-empty but UNNAMED body (arrow fn) is rejected — no trigger
    handler name to wire."""
    with pytest.raises(ValueError, match="NAMED function declaration"):
        edit_trigger.as_install_edit_trigger(
            sheet_id="SHEET-1",
            handler_function_body="const f = (e) => { react(e); };",
        )


# =====================================================================
# API error path → ToolError (standard creds=True envelope)
# =====================================================================


def test_install_edit_trigger_api_httperror_maps_to_tool_error():
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
            edit_trigger.as_install_edit_trigger(
                sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
            )


# =====================================================================
# Decorator-envelope cross-check: scope-aware creds resolution fires
# =====================================================================


def test_install_edit_trigger_resolves_creds_via_scope_aware_path(
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
    edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
    )

    assert len(calls) == 1
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES


def test_ledger_records_guarded_trigger_target_matching_installtrigger(
    with_sheet_container, monkeypatch,
):
    """S2 reconcile (the sharp edge): the ledger handler name MUST equal the
    function installTrigger actually wires (the observability GUARD wrapper),
    not the caller's semantic handler onSheetEdit. Uninstall's self-disarm
    reaper redefines exactly the recorded name, so a mismatch would leave the
    onEdit trigger firing forever without self-disarming. Pin: recorded ==
    ScriptApp.newTrigger target == guard_name_for('onSheetEdit')."""
    from appscriptly import automation_ledger
    from appscriptly.services.apps_script._observability import guard_name_for

    captured: dict = {}

    def spy_record(**kwargs):
        captured["handler_functions"] = list(
            kwargs.get("handler_functions") or []
        )
        return None  # capture only; don't touch the ledger DB

    monkeypatch.setattr(automation_ledger, "record_automation", spy_record)

    edit_trigger.as_install_edit_trigger(
        sheet_id="SHEET-1", handler_function_body=_HANDLER_FN,
    )

    guard = guard_name_for("onSheetEdit")
    assert captured["handler_functions"] == [guard]
    pushed = _pushed_code_source(with_sheet_container)
    assert f'var handlerName = "{guard}"' in pushed
    assert 'var handlerName = "onSheetEdit"' not in pushed
