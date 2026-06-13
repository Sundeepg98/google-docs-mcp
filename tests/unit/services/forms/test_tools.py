"""Per-tool behavior tests for services/forms/tools.py (new service).

Mirrors ``tests/unit/services/slides/test_tools.py`` exactly: canonical
per-tool happy-path coverage at the decorator-envelope boundary, using
the same ``InMemoryGoogleAPIClient`` + monkeypatched
``_get_credentials_fn`` fixture pattern.

The 7 forms tools:

  1. gforms_create_form    — forms.create (+ updateFormInfo)
  2. gforms_get_form       — forms.get
  3. gforms_add_question   — batchUpdate (createItem)
  4. gforms_update_item    — batchUpdate (updateItem)
  5. gforms_delete_item    — batchUpdate (deleteItem)
  6. gforms_list_responses — responses.list
  7. gforms_get_response   — responses.get

Per-tool API-shape coverage (body shapes, masks, envelope mapping) lives
in ``test_api.py``; this file covers the tool-layer envelope: the
decorator's ``_get_credentials_fn`` injection, ``@workspace_tool(creds=
True, scopes=...)`` wrapping, parameter forwarding, and ToolError
translation of pre-API ValueErrors.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fastmcp.exceptions import ToolError

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.forms import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True) envelope doesn't try real OAuth.

    NOTE: the Forms tools declare per-tool ``scopes=``, so the decorator's
    creds path goes through ``_resolve_credentials_for_scopes`` →
    ``auth.load_credentials`` (stdio branch) rather than the bare
    ``_get_credentials_fn``. We patch BOTH the module-level
    ``_get_credentials`` import and ``auth.load_credentials`` (used by the
    scoped path) so neither attempts a real consent flow."""
    monkeypatch.setattr(tools, "_get_credentials", lambda: stub_creds)
    monkeypatch.setattr(
        decorators, "_get_credentials_fn", lambda: stub_creds
    )

    # The scoped-creds path (scopes=[...]) resolves via auth.load_credentials
    # in stdio mode — stub it so it returns creds without OAuth.
    import appscriptly.auth as _auth

    monkeypatch.setattr(
        _auth, "load_credentials", lambda *a, **k: stub_creds
    )


@pytest.fixture
def forms_stub():
    """A Forms v1 Resource stub with the method chains pre-wired to
    plausible defaults. Individual tests override per-call as needed."""
    forms = MagicMock(name="forms-v1-stub")
    forms.forms().create().execute.return_value = {
        "formId": "F-NEW",
        "info": {"title": "T"},
        "responderUri": "https://docs.google.com/forms/d/F-NEW/viewform",
    }
    forms.forms().get().execute.return_value = {
        "formId": "F1",
        "info": {"title": "Feedback", "description": "d"},
        "responderUri": "https://docs.google.com/forms/d/F1/viewform",
        "items": [],
    }
    forms.forms().batchUpdate().execute.return_value = {
        "replies": [{"createItem": {"itemId": "ITEM-1"}}],
    }
    forms.forms().responses().list().execute.return_value = {
        "responses": [],
        "nextPageToken": "",
    }
    forms.forms().responses().get().execute.return_value = {
        "responseId": "R1",
        "createTime": "2026-01-01T00:00:00Z",
        "lastSubmittedTime": "2026-01-01T00:01:00Z",
        "answers": {},
    }
    return forms


@pytest.fixture
def with_forms_stub(forms_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("forms", "v1"): forms_stub,
    })):
        yield forms_stub


# ---------------------------------------------------------------------
# 1. gforms_create_form
# ---------------------------------------------------------------------


def test_gforms_create_form_happy_path(with_forms_stub):
    result = tools.gforms_create_form(title="My Survey")
    assert result == {
        "form_id": "F-NEW",
        "url": "https://docs.google.com/forms/d/F-NEW/viewform",
        "title": "T",
        "description": "",
    }


def test_gforms_create_form_rejects_blank_title_as_toolerror(with_forms_stub):
    """The api raises ValueError; the tool wraps it as ToolError for
    cloud-mode callers."""
    with pytest.raises(ToolError, match="title cannot be empty"):
        tools.gforms_create_form(title="   ")


# ---------------------------------------------------------------------
# 2. gforms_get_form
# ---------------------------------------------------------------------


def test_gforms_get_form_happy_path(with_forms_stub):
    result = tools.gforms_get_form(form_id="F1")
    assert result == {
        "form_id": "F1",
        "title": "Feedback",
        "description": "d",
        "url": "https://docs.google.com/forms/d/F1/viewform",
        "items": [],
    }


# ---------------------------------------------------------------------
# 3. gforms_add_question
# ---------------------------------------------------------------------


def test_gforms_add_question_happy_path(with_forms_stub):
    result = tools.gforms_add_question(
        form_id="F1", title="Your name?", question_type="text",
    )
    assert result == {
        "form_id": "F1",
        "item_id": "ITEM-1",
        "question_type": "text",
        "index": 0,
    }


def test_gforms_add_question_forwards_choice_options(with_forms_stub):
    tools.gforms_add_question(
        form_id="F1", title="Pick", question_type="choice",
        options=["A", "B"], choice_type="CHECKBOX",
    )
    last = with_forms_stub.forms().batchUpdate.call_args_list[-1]
    cq = (
        last.kwargs["body"]["requests"][0]["createItem"]["item"]
        ["questionItem"]["question"]["choiceQuestion"]
    )
    assert cq["type"] == "CHECKBOX"
    assert cq["options"] == [{"value": "A"}, {"value": "B"}]


def test_gforms_add_question_validation_wrapped_as_toolerror(with_forms_stub):
    with pytest.raises(ToolError, match="choice questions require"):
        tools.gforms_add_question(
            form_id="F1", title="Pick", question_type="choice",
        )


# ---------------------------------------------------------------------
# 4. gforms_update_item
# ---------------------------------------------------------------------


def test_gforms_update_item_happy_path(with_forms_stub):
    with_forms_stub.forms().batchUpdate().execute.return_value = {"replies": [{}]}
    result = tools.gforms_update_item(form_id="F1", index=2, title="New")
    assert result == {
        "form_id": "F1",
        "index": 2,
        "updated_fields": ["title"],
    }


def test_gforms_update_item_no_fields_wrapped_as_toolerror(with_forms_stub):
    with pytest.raises(ToolError, match="no fields supplied"):
        tools.gforms_update_item(form_id="F1", index=0)


# ---------------------------------------------------------------------
# 5. gforms_delete_item
# ---------------------------------------------------------------------


def test_gforms_delete_item_happy_path(with_forms_stub):
    with_forms_stub.forms().batchUpdate().execute.return_value = {"replies": [{}]}
    result = tools.gforms_delete_item(form_id="F1", index=3)
    assert result == {"form_id": "F1", "deleted_index": 3}


def test_gforms_delete_item_negative_index_wrapped_as_toolerror(with_forms_stub):
    with pytest.raises(ToolError, match="index must be >= 0"):
        tools.gforms_delete_item(form_id="F1", index=-1)


# ---------------------------------------------------------------------
# 6. gforms_list_responses
# ---------------------------------------------------------------------


def test_gforms_list_responses_happy_path(with_forms_stub):
    with_forms_stub.forms().responses().list().execute.return_value = {
        "responses": [{
            "responseId": "R1",
            "createTime": "2026-01-01T00:00:00Z",
            "lastSubmittedTime": "2026-01-01T00:05:00Z",
            "answers": {},
        }],
        "nextPageToken": "NEXT",
    }
    result = tools.gforms_list_responses(form_id="F1", page_size=50)
    assert result["form_id"] == "F1"
    assert result["next_page_token"] == "NEXT"
    assert result["responses"][0]["response_id"] == "R1"


def test_gforms_list_responses_subunit_page_size_wrapped_as_toolerror(
    with_forms_stub,
):
    with pytest.raises(ToolError, match="page_size must be >= 1"):
        tools.gforms_list_responses(form_id="F1", page_size=0)


# ---------------------------------------------------------------------
# 7. gforms_get_response
# ---------------------------------------------------------------------


def test_gforms_get_response_happy_path(with_forms_stub):
    result = tools.gforms_get_response(form_id="F1", response_id="R1")
    assert result == {
        "form_id": "F1",
        "response_id": "R1",
        "create_time": "2026-01-01T00:00:00Z",
        "last_submitted_time": "2026-01-01T00:01:00Z",
        "answers": {},
    }


def test_gforms_get_response_empty_id_wrapped_as_toolerror(with_forms_stub):
    with pytest.raises(ToolError, match="response_id cannot be empty"):
        tools.gforms_get_response(form_id="F1", response_id="")


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: scoped creds path is exercised
# ---------------------------------------------------------------------


def test_gforms_get_form_resolves_creds_through_scoped_path(
    with_forms_stub, monkeypatch,
):
    """The Forms tools declare scopes=, so creds resolve via
    auth.load_credentials (stdio branch of _resolve_credentials_for_scopes)
    with the Forms scope asserted. Verify that path is hit (load_credentials
    called) rather than the bare _get_credentials_fn."""
    import appscriptly.auth as _auth

    calls = {"n": 0, "scopes": None}

    def counting_load(*_a, **kwargs):
        calls["n"] += 1
        calls["scopes"] = kwargs.get("extra_scopes")
        return MagicMock(name="scoped-creds")

    monkeypatch.setattr(_auth, "load_credentials", counting_load)
    tools.gforms_get_form(form_id="F1")
    assert calls["n"] == 1, (
        "auth.load_credentials was not called once — the scoped-creds "
        "decorator path may have changed."
    )
    assert calls["scopes"] == ["https://www.googleapis.com/auth/forms.body"]
