"""Co-located tests for services/forms/api.py (new service).

Mirrors ``tests/unit/services/slides/test_api.py``: exercise the module
via ``with_google_api_client(InMemoryGoogleAPIClient)`` so the real
``get_service`` chokepoint runs but Forms' HTTP boundary is stubbed. No
real OAuth, no real Forms round-trip.

Tests cover four surfaces:

1. **Pre-API validation** — empty title (create / add_question),
   unknown question_type, choice-without-options, scale ``high <= low``,
   negative ``index``, update with no fields, empty response_id.
2. **Pure helpers** — ``_classify_item`` (item-union → coarse kind),
   ``_summarize_response`` (FormResponse → flat envelope),
   ``_build_question`` (via add_question's request shape).
3. **Forms call shape** — the right method chain receives the right
   kwargs: ``forms.create(body={info.title})``, ``forms.get(formId)``,
   ``forms.batchUpdate(formId, body={requests:[...]})`` with createItem /
   updateItem / deleteItem, ``forms.responses.list/get``.
4. **Response envelope shape** — the flat dicts the tool layer surfaces.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.forms.api import (
    _classify_item,
    _summarize_response,
    add_question,
    create_form,
    delete_item,
    get_form,
    get_response,
    list_responses,
    update_item,
)


# ---------------------------------------------------------------------
# _classify_item — pure helper exercised directly
# ---------------------------------------------------------------------


def test_classify_item_maps_each_union_field_to_a_label():
    assert _classify_item({"questionItem": {}}) == "question"
    assert _classify_item({"questionGroupItem": {}}) == "question_group"
    assert _classify_item({"pageBreakItem": {}}) == "page_break"
    assert _classify_item({"textItem": {}}) == "text"
    assert _classify_item({"imageItem": {}}) == "image"
    assert _classify_item({"videoItem": {}}) == "video"


def test_classify_item_unknown_when_no_recognized_field():
    assert _classify_item({}) == "unknown"
    assert _classify_item({"somethingNew": {}}) == "unknown"


# ---------------------------------------------------------------------
# _summarize_response — pure helper
# ---------------------------------------------------------------------


def test_summarize_response_flattens_and_defaults_missing_fields():
    out = _summarize_response({
        "responseId": "R1",
        "createTime": "2026-01-01T00:00:00Z",
        "lastSubmittedTime": "2026-01-02T00:00:00Z",
        "answers": {"q1": {"textAnswers": {"answers": [{"value": "x"}]}}},
    })
    assert out == {
        "response_id": "R1",
        "create_time": "2026-01-01T00:00:00Z",
        "last_submitted_time": "2026-01-02T00:00:00Z",
        "answers": {"q1": {"textAnswers": {"answers": [{"value": "x"}]}}},
    }


def test_summarize_response_defaults_to_empty_when_fields_missing():
    out = _summarize_response({})
    assert out == {
        "response_id": "",
        "create_time": "",
        "last_submitted_time": "",
        "answers": {},
    }


# ---------------------------------------------------------------------
# create_form — validation + Forms call shape + envelope
# ---------------------------------------------------------------------


def test_create_form_rejects_blank_title():
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_form(MagicMock(), "")
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_form(MagicMock(), "   ")


@pytest.fixture
def stub_forms_for_create():
    forms = MagicMock(name="forms-v1-stub-create")
    forms.forms().create().execute.return_value = {
        "formId": "FORM-NEW-1",
        "info": {"title": "Survey"},
        "responderUri": "https://docs.google.com/forms/d/FORM-NEW-1/viewform",
    }
    forms.forms().batchUpdate().execute.return_value = {"replies": [{}]}
    with with_google_api_client(InMemoryGoogleAPIClient({("forms", "v1"): forms})):
        yield forms


def _last_kwargs(mock_method) -> dict:
    """Most recent call's kwargs that carried a meaningful key."""
    for call in reversed(mock_method.call_args_list):
        if call.kwargs:
            return call.kwargs
    raise AssertionError("no call captured kwargs")


def test_create_form_builds_info_title_body(stub_forms_for_create):
    """forms.create body must wrap the title under ``info.title``."""
    create_form(MagicMock(), "  Survey  ")
    kw = _last_kwargs(stub_forms_for_create.forms().create)
    assert kw["body"] == {"info": {"title": "Survey"}}


def test_create_form_returns_flat_envelope_with_responder_uri(
    stub_forms_for_create,
):
    result = create_form(MagicMock(), "Survey")
    assert result == {
        "form_id": "FORM-NEW-1",
        "url": "https://docs.google.com/forms/d/FORM-NEW-1/viewform",
        "title": "Survey",
        "description": "",
    }


def test_create_form_without_description_skips_batchUpdate(
    stub_forms_for_create,
):
    """No description → no follow-up batchUpdate call."""
    stub_forms_for_create.forms().batchUpdate.reset_mock()
    create_form(MagicMock(), "Survey")
    # No batchUpdate call carried a formId (the create-only path).
    bu_calls = [
        c for c in stub_forms_for_create.forms().batchUpdate.call_args_list
        if "formId" in c.kwargs
    ]
    assert bu_calls == []


def test_create_form_with_description_issues_updateFormInfo(
    stub_forms_for_create,
):
    """A non-empty description triggers a batchUpdate updateFormInfo with
    a description updateMask."""
    result = create_form(MagicMock(), "Survey", description="  All about X  ")
    assert result["description"] == "All about X"
    bu = next(
        c for c in stub_forms_for_create.forms().batchUpdate.call_args_list
        if "formId" in c.kwargs
    )
    assert bu.kwargs["formId"] == "FORM-NEW-1"
    req = bu.kwargs["body"]["requests"][0]["updateFormInfo"]
    assert req["info"]["description"] == "All about X"
    assert req["updateMask"] == "description"


def test_create_form_falls_back_to_synth_url_when_no_responder_uri(
    stub_forms_for_create,
):
    stub_forms_for_create.forms().create().execute.return_value = {
        "formId": "ABC",
        "info": {"title": "T"},
        # no responderUri
    }
    result = create_form(MagicMock(), "T")
    assert result["url"] == "https://docs.google.com/forms/d/ABC/edit"


# ---------------------------------------------------------------------
# get_form — Forms call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_forms_for_get():
    forms = MagicMock(name="forms-v1-stub-get")
    forms.forms().get().execute.return_value = {
        "formId": "FORM-1",
        "info": {"title": "Feedback", "description": "Tell us"},
        "responderUri": "https://docs.google.com/forms/d/FORM-1/viewform",
        "items": [
            {"itemId": "i1", "title": "Name", "questionItem": {}},
            {"itemId": "i2", "title": "Section", "pageBreakItem": {}},
        ],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("forms", "v1"): forms})):
        yield forms


def test_get_form_passes_formId(stub_forms_for_get):
    get_form(MagicMock(), "FORM-XYZ")
    kw = _last_kwargs(stub_forms_for_get.forms().get)
    assert kw["formId"] == "FORM-XYZ"


def test_get_form_returns_flat_envelope_with_classified_items(
    stub_forms_for_get,
):
    result = get_form(MagicMock(), "FORM-1")
    assert result["form_id"] == "FORM-1"
    assert result["title"] == "Feedback"
    assert result["description"] == "Tell us"
    assert result["url"] == "https://docs.google.com/forms/d/FORM-1/viewform"
    assert result["items"] == [
        {"item_id": "i1", "title": "Name", "type": "question"},
        {"item_id": "i2", "title": "Section", "type": "page_break"},
    ]


def test_get_form_returns_empty_items_for_itemless_form(stub_forms_for_get):
    stub_forms_for_get.forms().get().execute.return_value = {
        "formId": "FORM-2",
        "info": {"title": "Empty"},
    }
    result = get_form(MagicMock(), "FORM-2")
    assert result["items"] == []
    assert result["description"] == ""


# ---------------------------------------------------------------------
# add_question — validation + Forms call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_forms_for_batch():
    forms = MagicMock(name="forms-v1-stub-batch")
    forms.forms().batchUpdate().execute.return_value = {
        "replies": [{"createItem": {"itemId": "NEW-ITEM-1"}}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("forms", "v1"): forms})):
        yield forms


def _last_batch_kwargs(forms: MagicMock) -> dict:
    for call in reversed(forms.forms().batchUpdate.call_args_list):
        if "formId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate call captured formId")


def test_add_question_rejects_blank_title():
    with pytest.raises(ValueError, match="title cannot be empty"):
        add_question(MagicMock(), "FORM1", "")


def test_add_question_rejects_unknown_type():
    with pytest.raises(ValueError, match="question_type must be one of"):
        add_question(MagicMock(), "FORM1", "Q", question_type="ranking")


def test_add_question_rejects_negative_index():
    with pytest.raises(ValueError, match="index must be >= 0"):
        add_question(MagicMock(), "FORM1", "Q", index=-1)


def test_add_question_text_builds_textQuestion(stub_forms_for_batch):
    add_question(
        MagicMock(), "FORM1", "Your name?",
        question_type="text", paragraph=True, required=True, index=2,
    )
    req = _last_batch_kwargs(stub_forms_for_batch)["body"]["requests"][0]
    ci = req["createItem"]
    assert ci["location"] == {"index": 2}
    assert ci["item"]["title"] == "Your name?"
    q = ci["item"]["questionItem"]["question"]
    assert q["required"] is True
    assert q["textQuestion"] == {"paragraph": True}


def test_add_question_choice_requires_options():
    with pytest.raises(ValueError, match="choice questions require"):
        add_question(MagicMock(), "FORM1", "Pick", question_type="choice")


def test_add_question_choice_rejects_bad_choice_type():
    with pytest.raises(ValueError, match="choice_type must be one of"):
        add_question(
            MagicMock(), "FORM1", "Pick", question_type="choice",
            options=["a", "b"], choice_type="GRID",
        )


def test_add_question_choice_builds_choiceQuestion(stub_forms_for_batch):
    add_question(
        MagicMock(), "FORM1", "Pick one",
        question_type="choice", options=["Red", "Blue"], choice_type="RADIO",
    )
    req = _last_batch_kwargs(stub_forms_for_batch)["body"]["requests"][0]
    cq = req["createItem"]["item"]["questionItem"]["question"]["choiceQuestion"]
    assert cq["type"] == "RADIO"
    assert cq["options"] == [{"value": "Red"}, {"value": "Blue"}]


def test_add_question_scale_rejects_high_not_greater_than_low():
    with pytest.raises(ValueError, match="must be greater than low"):
        add_question(
            MagicMock(), "FORM1", "Rate", question_type="scale", low=5, high=5,
        )


def test_add_question_scale_builds_scaleQuestion_with_labels(
    stub_forms_for_batch,
):
    add_question(
        MagicMock(), "FORM1", "Rate us",
        question_type="scale", low=1, high=10,
        low_label="Bad", high_label="Great",
    )
    req = _last_batch_kwargs(stub_forms_for_batch)["body"]["requests"][0]
    sq = req["createItem"]["item"]["questionItem"]["question"]["scaleQuestion"]
    assert sq == {"low": 1, "high": 10, "lowLabel": "Bad", "highLabel": "Great"}


def test_add_question_returns_flat_envelope_with_item_id(stub_forms_for_batch):
    result = add_question(MagicMock(), "FORM-1", "Q", question_type="text")
    assert result == {
        "form_id": "FORM-1",
        "item_id": "NEW-ITEM-1",
        "question_type": "text",
        "index": 0,
    }


def test_add_question_item_id_empty_when_reply_omits_it(stub_forms_for_batch):
    stub_forms_for_batch.forms().batchUpdate().execute.return_value = {
        "replies": [{}],
    }
    result = add_question(MagicMock(), "FORM-1", "Q")
    assert result["item_id"] == ""


# ---------------------------------------------------------------------
# update_item — validation + Forms call shape + envelope
# ---------------------------------------------------------------------


def test_update_item_rejects_negative_index():
    with pytest.raises(ValueError, match="index must be >= 0"):
        update_item(MagicMock(), "FORM1", -1, title="x")


def test_update_item_rejects_no_fields():
    with pytest.raises(ValueError, match="no fields supplied"):
        update_item(MagicMock(), "FORM1", 0)


def test_update_item_builds_updateItem_with_mask(stub_forms_for_batch):
    update_item(MagicMock(), "FORM1", 3, title="New title", description="d")
    req = _last_batch_kwargs(stub_forms_for_batch)["body"]["requests"][0]
    ui = req["updateItem"]
    assert ui["location"] == {"index": 3}
    assert ui["item"] == {"title": "New title", "description": "d"}
    assert ui["updateMask"] == "title,description"


def test_update_item_mask_only_includes_supplied_fields(stub_forms_for_batch):
    update_item(MagicMock(), "FORM1", 0, description="only desc")
    req = _last_batch_kwargs(stub_forms_for_batch)["body"]["requests"][0]
    assert req["updateItem"]["updateMask"] == "description"
    assert req["updateItem"]["item"] == {"description": "only desc"}


def test_update_item_returns_envelope(stub_forms_for_batch):
    result = update_item(MagicMock(), "FORM-1", 2, title="t")
    assert result == {
        "form_id": "FORM-1",
        "index": 2,
        "updated_fields": ["title"],
    }


# ---------------------------------------------------------------------
# delete_item — validation + Forms call shape + envelope
# ---------------------------------------------------------------------


def test_delete_item_rejects_negative_index():
    with pytest.raises(ValueError, match="index must be >= 0"):
        delete_item(MagicMock(), "FORM1", -2)


def test_delete_item_builds_deleteItem_request(stub_forms_for_batch):
    delete_item(MagicMock(), "FORM1", 4)
    req = _last_batch_kwargs(stub_forms_for_batch)["body"]["requests"][0]
    assert req == {"deleteItem": {"location": {"index": 4}}}


def test_delete_item_returns_envelope(stub_forms_for_batch):
    result = delete_item(MagicMock(), "FORM-1", 1)
    assert result == {"form_id": "FORM-1", "deleted_index": 1}


# ---------------------------------------------------------------------
# list_responses — validation + Forms call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_forms_for_responses():
    forms = MagicMock(name="forms-v1-stub-responses")
    forms.forms().responses().list().execute.return_value = {
        "responses": [
            {
                "responseId": "R1",
                "createTime": "2026-01-01T00:00:00Z",
                "lastSubmittedTime": "2026-01-01T00:05:00Z",
                "answers": {"q1": {"textAnswers": {"answers": [{"value": "Yes"}]}}},
            },
        ],
        "nextPageToken": "TOK2",
    }
    forms.forms().responses().get().execute.return_value = {
        "responseId": "R9",
        "createTime": "2026-02-01T00:00:00Z",
        "lastSubmittedTime": "2026-02-01T00:01:00Z",
        "answers": {"q1": {"textAnswers": {"answers": [{"value": "No"}]}}},
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("forms", "v1"): forms})):
        yield forms


def test_list_responses_rejects_subunit_page_size():
    with pytest.raises(ValueError, match="page_size must be >= 1"):
        list_responses(MagicMock(), "FORM1", page_size=0)


def _last_responses_list_kwargs(forms: MagicMock) -> dict:
    for call in reversed(forms.forms().responses().list.call_args_list):
        if "formId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no responses().list call captured formId")


def test_list_responses_passes_formId_and_paging(stub_forms_for_responses):
    list_responses(MagicMock(), "FORM-XYZ", page_size=10, page_token="TOK1")
    kw = _last_responses_list_kwargs(stub_forms_for_responses)
    assert kw["formId"] == "FORM-XYZ"
    assert kw["pageSize"] == 10
    assert kw["pageToken"] == "TOK1"


def test_list_responses_clamps_page_size_to_5000(stub_forms_for_responses):
    list_responses(MagicMock(), "FORM1", page_size=999999)
    kw = _last_responses_list_kwargs(stub_forms_for_responses)
    assert kw["pageSize"] == 5000


def test_list_responses_returns_flat_envelope(stub_forms_for_responses):
    result = list_responses(MagicMock(), "FORM-1")
    assert result["form_id"] == "FORM-1"
    assert result["next_page_token"] == "TOK2"
    assert result["responses"] == [
        {
            "response_id": "R1",
            "create_time": "2026-01-01T00:00:00Z",
            "last_submitted_time": "2026-01-01T00:05:00Z",
            "answers": {"q1": {"textAnswers": {"answers": [{"value": "Yes"}]}}},
        },
    ]


def test_list_responses_empty_envelope_when_no_responses(
    stub_forms_for_responses,
):
    stub_forms_for_responses.forms().responses().list().execute.return_value = {}
    result = list_responses(MagicMock(), "FORM-1")
    assert result["responses"] == []
    assert result["next_page_token"] == ""


# ---------------------------------------------------------------------
# get_response — validation + Forms call shape + envelope
# ---------------------------------------------------------------------


def test_get_response_rejects_empty_response_id():
    with pytest.raises(ValueError, match="response_id cannot be empty"):
        get_response(MagicMock(), "FORM1", "")


def _last_responses_get_kwargs(forms: MagicMock) -> dict:
    for call in reversed(forms.forms().responses().get.call_args_list):
        if "formId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no responses().get call captured formId")


def test_get_response_passes_form_and_response_id(stub_forms_for_responses):
    get_response(MagicMock(), "FORM-A", "R9")
    kw = _last_responses_get_kwargs(stub_forms_for_responses)
    assert kw["formId"] == "FORM-A"
    assert kw["responseId"] == "R9"


def test_get_response_returns_flat_envelope(stub_forms_for_responses):
    result = get_response(MagicMock(), "FORM-1", "R9")
    assert result == {
        "form_id": "FORM-1",
        "response_id": "R9",
        "create_time": "2026-02-01T00:00:00Z",
        "last_submitted_time": "2026-02-01T00:01:00Z",
        "answers": {"q1": {"textAnswers": {"answers": [{"value": "No"}]}}},
    }
