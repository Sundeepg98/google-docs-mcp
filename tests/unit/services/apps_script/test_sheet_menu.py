"""Tests for services/apps_script/sheet_menu.py (GAS service-parity).

``as_install_sheet_menu`` is the Sheets analogue of ``as_install_doc_menu``
— it composes the PR-Δ7 bound-script primitive into a custom-menu installer
for a Spreadsheet (``SpreadsheetApp.getUi()``). Coverage mirrors
test_doc_menu.py:

  * **Pure script generation** (``build_menu_script``) — the onOpen
    menu-builder + per-item handlers are emitted correctly and safely,
    using ``SpreadsheetApp.getUi()`` (the only structural difference from
    the Docs menu).
  * **Manifest scope derivation** — reusing #138's ``build_manifest`` with
    a ``menu`` key derives ``script.container.ui`` into the GENERATED
    manifest.
  * **Validation** — empty title / no items / malformed items raise
    ValueError before any API call.
  * **Tool happy-path** — end-to-end at the ``@workspace_tool(creds=True,
    scopes=...)`` boundary via ``InMemoryGoogleAPIClient``.
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
from appscriptly.services.apps_script import sheet_menu
from appscriptly.services.apps_script.sheet_menu import build_menu_script


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=...) envelope doesn't launch real
    OAuth (this tool declares scopes → scope-aware path)."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    """Apps Script v1 stub: create / updateContent / versions /
    deployments pre-wired to plausible defaults."""
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "SHEET1",
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
def with_script_client():
    """Apps Script stub wired for the full create→push→deploy flow.

    NOTE: as_install_sheet_menu knows the container is a Sheet (a
    SpreadsheetApp menu is Sheets-specific), so it does NOT auto-detect the
    container kind — no Drive stub needed."""
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


_ITEMS = [
    {
        "label": "Recompute totals",
        "function_name": "recomputeTotals",
        "function_body": (
            "SpreadsheetApp.getActiveSpreadsheet().getActiveSheet()"
            ".getRange('A1').setValue('done');"
        ),
    },
    {
        "label": "Clear sheet",
        "function_name": "clearSheet",
        "function_body": (
            "SpreadsheetApp.getActiveSpreadsheet().getActiveSheet().clear();"
        ),
    },
]


# ---------------------------------------------------------------------
# Pure script generation — build_menu_script
# ---------------------------------------------------------------------


def test_script_has_onopen_that_builds_the_named_menu():
    """The generated body defines onOpen and creates the named menu via
    SpreadsheetApp.getUi().createMenu(...).addToUi()."""
    src = build_menu_script("My Tools", _ITEMS)
    assert "function onOpen(e) {" in src
    assert "SpreadsheetApp.getUi()" in src
    assert 'createMenu("My Tools")' in src
    assert ".addToUi();" in src


def test_script_uses_spreadsheet_app_not_document_app():
    """Sheets menu must use SpreadsheetApp (not DocumentApp/SlidesApp)."""
    src = build_menu_script("My Tools", _ITEMS)
    assert "DocumentApp" not in src
    assert "SlidesApp" not in src


def test_script_adds_one_addItem_per_menu_item():
    """Each item becomes an .addItem(label, function_name) on the menu."""
    src = build_menu_script("My Tools", _ITEMS)
    assert '.addItem("Recompute totals", "recomputeTotals")' in src
    assert '.addItem("Clear sheet", "clearSheet")' in src
    assert src.count(".addItem(") == len(_ITEMS)


def test_script_emits_a_handler_function_per_item_with_its_body():
    """Each item's function_name + function_body becomes a top-level
    handler function in the generated source."""
    src = build_menu_script("My Tools", _ITEMS)
    assert "function recomputeTotals() {" in src
    assert "function clearSheet() {" in src
    assert "setValue('done');" in src
    assert "getActiveSheet().clear();" in src


def test_script_escapes_labels_to_prevent_injection():
    """A label with quotes / special chars is emitted as a safe JS string
    literal — it can't break out of the createMenu/addItem call."""
    items = [{
        "label": 'Say "hi" \\ now',
        "function_name": "sayHi",
        "function_body": "Logger.log('hi');",
    }]
    src = build_menu_script('Menu "X"', items)
    assert r'.addItem("Say \"hi\" \\ now", "sayHi")' in src
    assert r'createMenu("Menu \"X\"")' in src


def test_script_is_deterministic():
    """Same input → byte-identical output (pure function)."""
    assert build_menu_script("T", _ITEMS) == build_menu_script("T", _ITEMS)


# ---------------------------------------------------------------------
# Validation errors (raised before any API call)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_title", ["", "   ", "\t\n"])
def test_empty_menu_title_rejected(with_script_client, bad_title):
    with pytest.raises(ValueError, match="menu_title cannot be empty"):
        sheet_menu.as_install_sheet_menu(
            sheet_id="SHEET1", menu_title=bad_title, items=_ITEMS,
        )


def test_empty_items_list_rejected(with_script_client):
    with pytest.raises(ValueError, match="at least one menu item"):
        sheet_menu.as_install_sheet_menu(
            sheet_id="SHEET1", menu_title="Tools", items=[],
        )


def test_item_reserved_function_name_rejected(with_script_client):
    with pytest.raises(ValueError, match="reserved Apps Script trigger name"):
        sheet_menu.as_install_sheet_menu(
            sheet_id="SHEET1", menu_title="Tools",
            items=[{
                "label": "Go", "function_name": "onOpen",
                "function_body": "x();",
            }],
        )


def test_duplicate_function_names_rejected(with_script_client):
    with pytest.raises(ValueError, match="duplicated"):
        sheet_menu.as_install_sheet_menu(
            sheet_id="SHEET1", menu_title="Tools",
            items=[
                {"label": "A", "function_name": "run", "function_body": "a();"},
                {"label": "B", "function_name": "run", "function_body": "b();"},
            ],
        )


def test_validation_failure_makes_no_api_call(with_script_client):
    with pytest.raises(ValueError):
        sheet_menu.as_install_sheet_menu(
            sheet_id="SHEET1", menu_title="", items=_ITEMS,
        )
    create_body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_body_calls


# ---------------------------------------------------------------------
# Tool happy-path — end-to-end via the decorator boundary
# ---------------------------------------------------------------------


def test_happy_path_returns_envelope(with_script_client):
    result = sheet_menu.as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Budget Tools", items=_ITEMS,
    )
    assert result == {
        "script_id": "SCRIPT-1",
        "deployment_id": "DEPLOY-1",
        "sheet_id": "SHEET1",
        "menu_title": "Budget Tools",
        "item_count": 2,
        "project_url": "https://script.google.com/d/SCRIPT-1/edit",
    }


def test_binds_via_parent_id_to_the_sheet(with_script_client):
    """The create call must pass parentId=sheet_id — the BINDING that
    attaches the menu script to THIS Sheet."""
    sheet_menu.as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "SHEET1"


def test_manifest_declares_ui_scope(with_script_client):
    """A menu requires script.container.ui — build_manifest must derive it
    into the pushed appsscript.json; the __plan__ echo must be stripped."""
    sheet_menu.as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    assert (
        "https://www.googleapis.com/auth/script.container.ui"
        in parsed["oauthScopes"]
    )
    assert "__plan__" not in parsed


def test_pushes_generated_menu_script_as_server_js(with_script_client):
    sheet_menu.as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert code_files, "no SERVER_JS file pushed"
    src = code_files[-1]["source"]
    assert "function onOpen(e) {" in src
    assert "SpreadsheetApp.getUi()" in src


# ---------------------------------------------------------------------
# Error path — API HttpError → ToolError via the decorator envelope
# ---------------------------------------------------------------------


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
            sheet_menu.as_install_sheet_menu(
                sheet_id="SHEET1", menu_title="Tools", items=_ITEMS,
            )


def test_resolves_creds_via_scope_aware_path(with_script_client, monkeypatch):
    """Canary: because this tool DECLARES scopes, creds resolution flows
    through the scope-aware path with the tool's scopes as extra_scopes."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    sheet_menu.as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Tools", items=_ITEMS,
    )
    assert len(calls) == 1
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
