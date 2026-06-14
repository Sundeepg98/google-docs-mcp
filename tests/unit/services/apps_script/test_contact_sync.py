"""Tests for services/apps_script/contact_sync.py (GAS parity — Contacts).

``as_install_contact_sync`` is the Contacts-specialized sibling of
``as_install_form_handler``: a USE-CASE tool composing the PR-Δ7
bound-script primitive into a REACTIVE onFormSubmit automation that
creates/updates a Google contact (via ``ContactsApp``) from each
submission, bound DIRECTLY to a Form (lifting the Forms hard-rejection).
The deploy + body-synthesis machinery is REUSED (api.py +
form_handler.build_form_handler_script_body); this file covers THIS
module's own contributions:

  * the GENERATED manifest declares the full ``contacts`` scope (the scope
    guard) AND the onFormSubmit-trigger ``script.scriptapp``;
  * appscriptly's OWN consent gains NO new scope;
  * the Forms-rejection lift (binds directly to the Form, no Drive
    auto-detect — the fixture registers NO Drive stub, so a clean deploy
    proves the reject path is bypassed);
  * the honest trigger-activation state + manifest_scope in the return;
  * input validation + the happy path at the @workspace_tool boundary.
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
from appscriptly.services.apps_script import contact_sync

_CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# A representative caller-supplied onFormSubmit -> ContactsApp handler.
_HANDLER_FN = (
    "function onSubmit(e) {\n"
    "  var items = e.response.getItemResponses();\n"
    "  var name = items[0].getResponse();\n"
    "  var email = items[1].getResponse();\n"
    "  ContactsApp.createContact(name, '', email);\n"
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
    monkeypatch.setattr(contact_sync, "_get_credentials", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "FORM-1",
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
def with_form_container():
    """Apps Script stub for create→push→deploy. NO Drive stub is
    registered: the tool binds directly to the Form ID and must NEVER call
    auto_detect_container_kind (which would hit Drive and reject the Form).
    A clean deploy here is itself proof the Forms-reject path is bypassed."""
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


def test_happy_path_returns_envelope(with_form_container):
    result = contact_sync.as_install_contact_sync(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["form_id"] == "FORM-1"
    assert result["trigger_type"] == "onFormSubmit"
    assert result["trigger_handler"] == "onSubmit"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert result["manifest_scope"] == _CONTACTS_SCOPE


def test_reports_honest_trigger_state(with_form_container):
    result = contact_sync.as_install_contact_sync(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    assert result["trigger_active"] is False
    assert result["activation_required"] is True
    assert "installTrigger" in result["activation_instructions"]


def test_lifts_forms_rejection_binds_via_parent_id(with_form_container):
    """The load-bearing Forms-rejection lift: a Form ID deploys cleanly,
    binding via parentId=form_id. The fixture has NO Drive stub — a clean
    return proves auto_detect_container_kind was never called."""
    result = contact_sync.as_install_contact_sync(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    assert result["form_id"] == "FORM-1"
    body_calls = [
        c for c in with_form_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "FORM-1"


def test_pushes_synthesized_body(with_form_container):
    contact_sync.as_install_contact_sync(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    source = _pushed_code_source(with_form_container)
    assert "function onSubmit(e)" in source
    assert "ContactsApp.createContact(" in source
    assert "function installTrigger()" in source
    assert ".forForm(" in source
    assert ".onFormSubmit()" in source


def test_uses_custom_name_when_given(with_form_container):
    contact_sync.as_install_contact_sync(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
        name="Lead Capture",
    )
    body_calls = [
        c for c in with_form_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "Lead Capture"


# =====================================================================
# SCOPE GUARD (load-bearing) — contacts scope in GENERATED manifest only
# =====================================================================


def test_generated_manifest_declares_contacts_and_trigger_scopes(
    with_form_container,
):
    contact_sync.as_install_contact_sync(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    manifest = _pushed_manifest(with_form_container)
    assert _CONTACTS_SCOPE in manifest["oauthScopes"]
    assert _TRIGGER_SCOPE in manifest["oauthScopes"]
    assert "__plan__" not in manifest


def test_tool_declares_only_baseline_gas_scopes_not_contacts():
    import asyncio

    from appscriptly.server import mcp
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == "as_install_contact_sync")
    declared = list(getattr(tool.annotations, "scopes", []) or [])
    assert declared, "as_install_contact_sync must declare its scopes"
    assert set(declared) == set(GAS_BOUND_SCOPES)
    assert _CONTACTS_SCOPE not in declared


# =====================================================================
# Validation errors
# =====================================================================


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_rejects_empty_form_id(with_form_container, bad_id):
    with pytest.raises(ValueError, match="form_id cannot be empty"):
        contact_sync.as_install_contact_sync(
            form_id=bad_id, handler_function_body=_HANDLER_FN,
        )
    create_calls = [
        c for c in with_form_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_calls


def test_rejects_empty_handler_body(with_form_container):
    with pytest.raises(ValueError, match="handler_function_body cannot be empty"):
        contact_sync.as_install_contact_sync(
            form_id="FORM-1", handler_function_body="   ",
        )


def test_rejects_unnamed_handler_body(with_form_container):
    with pytest.raises(ValueError, match="NAMED function declaration"):
        contact_sync.as_install_contact_sync(
            form_id="FORM-1",
            handler_function_body="const f = (e) => { sync(e); };",
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
            contact_sync.as_install_contact_sync(
                form_id="FORM-1", handler_function_body=_HANDLER_FN,
            )
