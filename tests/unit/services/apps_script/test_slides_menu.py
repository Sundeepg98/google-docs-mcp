"""Tests for services/apps_script/slides_menu.py (GAS service-parity).

``as_install_slides_menu`` is the Slides analogue of ``as_install_doc_menu``
— it composes the PR-Δ7 bound-script primitive into a custom-menu installer
for a presentation (``SlidesApp.getUi()``), defaulting the menu title to
"Presentation Tools". Coverage mirrors test_doc_menu.py / test_sheet_menu.py.
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
from appscriptly.services.apps_script import slides_menu
from appscriptly.services.apps_script.slides_menu import build_menu_script


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "PRES1",
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
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


_ITEMS = [
    {
        "label": "Insert title slide",
        "function_name": "insertTitle",
        "function_body": (
            "SlidesApp.getActivePresentation().appendSlide();"
        ),
    },
]


# ---------------------------------------------------------------------
# Pure script generation — build_menu_script
# ---------------------------------------------------------------------


def test_script_has_onopen_that_builds_the_named_menu():
    src = build_menu_script("Presentation Tools", _ITEMS)
    assert "function onOpen(e) {" in src
    assert "SlidesApp.getUi()" in src
    assert 'createMenu("Presentation Tools")' in src
    assert ".addToUi();" in src


def test_script_uses_slides_app_not_document_or_spreadsheet_app():
    src = build_menu_script("Presentation Tools", _ITEMS)
    assert "DocumentApp" not in src
    assert "SpreadsheetApp" not in src


def test_script_adds_one_addItem_per_menu_item():
    src = build_menu_script("Presentation Tools", _ITEMS)
    assert '.addItem("Insert title slide", "insertTitle")' in src
    assert src.count(".addItem(") == len(_ITEMS)


def test_script_emits_handler_with_body():
    src = build_menu_script("Presentation Tools", _ITEMS)
    assert "function insertTitle() {" in src
    assert "appendSlide();" in src


def test_script_is_deterministic():
    assert build_menu_script("T", _ITEMS) == build_menu_script("T", _ITEMS)


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_title", ["", "   "])
def test_empty_menu_title_rejected(with_script_client, bad_title):
    with pytest.raises(ValueError, match="menu_title cannot be empty"):
        slides_menu.as_install_slides_menu(
            presentation_id="PRES1", items=_ITEMS, menu_title=bad_title,
        )


def test_empty_items_list_rejected(with_script_client):
    with pytest.raises(ValueError, match="at least one menu item"):
        slides_menu.as_install_slides_menu(
            presentation_id="PRES1", items=[],
        )


def test_validation_failure_makes_no_api_call(with_script_client):
    with pytest.raises(ValueError):
        slides_menu.as_install_slides_menu(
            presentation_id="PRES1", items=[],
        )
    create_body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_body_calls


# ---------------------------------------------------------------------
# Tool happy-path
# ---------------------------------------------------------------------


def test_default_menu_title_is_presentation_tools(with_script_client):
    """When menu_title is omitted, it defaults to 'Presentation Tools'."""
    result = slides_menu.as_install_slides_menu(
        presentation_id="PRES1", items=_ITEMS,
    )
    assert result["menu_title"] == "Presentation Tools"


def test_happy_path_returns_envelope(with_script_client):
    result = slides_menu.as_install_slides_menu(
        presentation_id="PRES1", items=_ITEMS, menu_title="Deck Tools",
    )
    assert result == {
        "script_id": "SCRIPT-1",
        "deployment_id": "DEPLOY-1",
        "on_conflict": "new",
        "reused_existing": False,
        "replaced_count": 0,
        "presentation_id": "PRES1",
        "menu_title": "Deck Tools",
        "item_count": 1,
        "project_url": "https://script.google.com/d/SCRIPT-1/edit",
    }


def test_binds_via_parent_id_to_the_presentation(with_script_client):
    slides_menu.as_install_slides_menu(
        presentation_id="PRES1", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "PRES1"


def test_manifest_declares_ui_scope(with_script_client):
    slides_menu.as_install_slides_menu(
        presentation_id="PRES1", items=_ITEMS,
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
    # Observability (gap #5): the failure reporter's send-only mail scope.
    assert (
        "https://www.googleapis.com/auth/script.send_mail"
        in parsed["oauthScopes"]
    )
    # N-S3V-1: the bound presentation's .currentonly data scope so a menu
    # handler can touch THIS presentation via SlidesApp (an explicit
    # oauthScopes block suppresses Apps Script's auto-detection).
    assert (
        "https://www.googleapis.com/auth/presentations.currentonly"
        in parsed["oauthScopes"]
    )
    assert "__plan__" not in parsed


def test_pushes_slides_app_menu_as_server_js(with_script_client):
    slides_menu.as_install_slides_menu(
        presentation_id="PRES1", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert code_files
    src = code_files[-1]["source"]
    assert "SlidesApp.getUi()" in src


# ---------------------------------------------------------------------
# Error path
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
            slides_menu.as_install_slides_menu(
                presentation_id="PRES1", items=_ITEMS,
            )
