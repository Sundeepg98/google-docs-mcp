"""Tests for services/apps_script/refresh_linked_slides.py (GAS parity).

``as_refresh_linked_slides`` composes the PR-Δ7 bound-script primitive into
a "sync linked slides from their source deck" tool — a bound script whose
``refreshLinkedSlides()`` walks ``getSlides()`` and calls ``refreshSlide()``
on each LINKED slide. It's an ON-DEMAND action (not a trigger), so the
return contract carries the honest ``run_required`` / ``refreshed_count``
state. Coverage:

  * **Pure script generation** (``build_refresh_script_body``) — onOpen
    menu + the refreshLinkedSlides walker with the linking-mode guard.
  * **Manifest scope derivation** — the onOpen menu derives
    ``script.container.ui`` and the presentations scope is declared, both
    into the GENERATED manifest only.
  * **Tool happy-path** — end-to-end envelope incl. the honest run-state.
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
from appscriptly.services.apps_script import refresh_linked_slides as rls
from appscriptly.services.apps_script.refresh_linked_slides import (
    build_refresh_script_body,
)


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


# ---------------------------------------------------------------------
# Pure script generation — build_refresh_script_body
# ---------------------------------------------------------------------


def test_script_walks_slides_and_refreshes_linked_ones():
    """The generated body iterates getSlides() and calls refreshSlide()
    only on slides whose linking mode is LINKED."""
    src = build_refresh_script_body()
    assert "getSlides()" in src
    assert "refreshSlide()" in src
    assert "getSlideLinkingMode()" in src
    assert "SlidesApp.SlideLinkingMode.LINKED" in src


def test_script_defines_the_refresh_function():
    src = build_refresh_script_body()
    assert "function refreshLinkedSlides() {" in src
    # returns the refreshed count
    assert "return refreshed;" in src


def test_script_has_onopen_menu_pointing_at_refresh_function():
    src = build_refresh_script_body("Presentation Tools")
    assert "function onOpen(e) {" in src
    assert "SlidesApp.getUi()" in src
    assert 'createMenu("Presentation Tools")' in src
    assert '.addItem("Refresh linked slides", "refreshLinkedSlides")' in src


def test_script_menu_title_is_embedded_and_escaped():
    src = build_refresh_script_body('Tools "X"')
    assert r'createMenu("Tools \"X\"")' in src


def test_script_is_deterministic():
    assert build_refresh_script_body("T") == build_refresh_script_body("T")


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_empty_presentation_id_rejected(with_script_client, bad_id):
    with pytest.raises(ValueError, match="presentation_id cannot be empty"):
        rls.as_refresh_linked_slides(presentation_id=bad_id)


@pytest.mark.parametrize("bad_title", ["", "   "])
def test_empty_menu_title_rejected(with_script_client, bad_title):
    with pytest.raises(ValueError, match="menu_title cannot be empty"):
        rls.as_refresh_linked_slides(
            presentation_id="PRES1", menu_title=bad_title,
        )


def test_validation_failure_makes_no_api_call(with_script_client):
    with pytest.raises(ValueError):
        rls.as_refresh_linked_slides(presentation_id="")
    create_body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_body_calls


# ---------------------------------------------------------------------
# Tool happy-path
# ---------------------------------------------------------------------


def test_happy_path_returns_honest_run_state(with_script_client):
    """The deploy wires the function but does not run it — so run_required
    is True and refreshed_count is None."""
    result = rls.as_refresh_linked_slides(presentation_id="PRES1")
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["presentation_id"] == "PRES1"
    assert result["refresh_function"] == "refreshLinkedSlides"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert result["refreshed_count"] is None
    assert result["run_required"] is True
    assert "refreshLinkedSlides" in result["run_instructions"]


def test_binds_via_parent_id_to_the_presentation(with_script_client):
    rls.as_refresh_linked_slides(presentation_id="PRES1")
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "PRES1"


def test_manifest_declares_ui_and_presentations_scopes(with_script_client):
    """The onOpen menu derives script.container.ui; refreshSlide() needs
    the presentations scope — both must reach the GENERATED manifest."""
    rls.as_refresh_linked_slides(presentation_id="PRES1")
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    scopes = parsed["oauthScopes"]
    assert "https://www.googleapis.com/auth/script.container.ui" in scopes
    assert "https://www.googleapis.com/auth/presentations" in scopes
    # Observability (gap #5): the failure reporter's send-only mail scope.
    assert "https://www.googleapis.com/auth/script.send_mail" in scopes
    assert "__plan__" not in parsed


def test_pushes_refresh_walker_as_server_js(with_script_client):
    rls.as_refresh_linked_slides(presentation_id="PRES1")
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert code_files
    src = code_files[-1]["source"]
    assert "refreshSlide()" in src
    assert "getSlideLinkingMode()" in src


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
            rls.as_refresh_linked_slides(presentation_id="PRES1")
