"""Per-tool behavior tests for services/slides/tools.py (v2.3.2).

Mirrors ``tests/unit/services/sheets/test_tools.py`` (PR #119) exactly:
canonical per-tool happy-path coverage at the decorator-envelope
boundary, using the same ``InMemoryGoogleAPIClient`` + monkeypatched
``_get_credentials_fn`` fixture pattern.

The 3 slides tools (v2.3.2 minimal start):

  1. gslides_get_outline         — presentations.get
  2. gslides_replace_all_text    — batchUpdate (replaceAllText)
  3. gslides_create_presentation — presentations.create

Per-tool API-shape coverage (body shapes, ``matchCase`` plumbing,
response envelope mapping) lives in ``test_api.py``; this file
covers the tool-layer envelope: decorator's ``_get_credentials_fn``
injection, ``@workspace_tool(creds=True)`` wrapping, parameter
forwarding from the decorated function into the api module.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.slides import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True) envelope doesn't try real OAuth.
    Sister to the same fixture in tests/unit/services/sheets/test_tools.py."""
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(tools, "_get_credentials", lambda: stub_creds)


@pytest.fixture
def slides_stub():
    """A Slides v1 Resource stub with all three method chains pre-wired
    to return plausible default responses. Individual tests override
    per-call as needed."""
    slides = MagicMock(name="slides-v1-stub")
    slides.presentations().get().execute.return_value = {
        "presentationId": "D1",
        "title": "Deck",
        "slides": [],
    }
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "D1",
        "replies": [{"replaceAllText": {"occurrencesChanged": 0}}],
    }
    slides.presentations().create().execute.return_value = {
        "presentationId": "NEW-1",
        "title": "T",
    }
    return slides


@pytest.fixture
def with_slides_stub(slides_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("slides", "v1"): slides_stub,
    })):
        yield slides_stub


# ---------------------------------------------------------------------
# 1. gslides_get_outline — happy path through the decorator envelope
# ---------------------------------------------------------------------


def test_gslides_get_outline_returns_envelope_for_empty_deck(with_slides_stub):
    """Default Slides stub returns an empty slides list; tool
    surfaces the ``{presentation_id, title, url, slides: []}``
    envelope through the standard ``@workspace_tool(creds=True)``
    boundary."""
    result = tools.gslides_get_outline(presentation_id="DECK1")
    assert result == {
        "presentation_id": "DECK1",
        "title": "Deck",
        "url": "https://docs.google.com/presentation/d/DECK1/edit",
        "slides": [],
    }


def test_gslides_get_outline_surfaces_slides_when_present(with_slides_stub):
    """When Slides returns slides with text shapes, the tool
    flattens text via ``_extract_slide_text`` and exposes the
    per-slide envelope to the agent."""
    with_slides_stub.presentations().get().execute.return_value = {
        "presentationId": "DECK1",
        "title": "Forecast",
        "slides": [
            {
                "objectId": "S001",
                "slideProperties": {"layoutObjectId": "L_TITLE"},
                "pageElements": [
                    {"shape": {"text": {"textElements": [
                        {"textRun": {"content": "Hello"}},
                    ]}}},
                ],
            },
        ],
    }
    result = tools.gslides_get_outline(presentation_id="DECK1")
    assert len(result["slides"]) == 1
    assert result["slides"][0]["object_id"] == "S001"
    assert result["slides"][0]["text"] == "Hello"


# ---------------------------------------------------------------------
# 2. gslides_replace_all_text — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_replace_all_text_happy_path(with_slides_stub):
    """Standard replace returns the ``{presentation_id,
    occurrences_changed}`` envelope. Tool layer pass-through of
    api function."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"replaceAllText": {"occurrencesChanged": 7}}],
    }
    result = tools.gslides_replace_all_text(
        presentation_id="DECK1",
        find_text="{{Name}}",
        replace_text="Acme Corp",
    )
    assert result == {"presentation_id": "DECK1", "occurrences_changed": 7}


def test_gslides_replace_all_text_validation_propagates_through_tool(
    with_slides_stub,
):
    """Pre-API validation (empty find_text) bubbles from the api
    module through the decorator envelope as ValueError. The
    decorator wraps it for cloud-mode callers, but raises the bare
    ValueError in test contexts."""
    with pytest.raises(ValueError, match="find_text cannot be empty"):
        tools.gslides_replace_all_text(
            presentation_id="DECK1",
            find_text="",
            replace_text="x",
        )


def test_gslides_replace_all_text_default_match_case_true(with_slides_stub):
    """Verify that the default ``match_case=True`` reaches the
    Slides API as ``matchCase=True``."""
    tools.gslides_replace_all_text(
        presentation_id="DECK1",
        find_text="foo",
        replace_text="bar",
    )
    last_call = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    request = last_call.kwargs["body"]["requests"][0]
    assert request["replaceAllText"]["containsText"]["matchCase"] is True


# ---------------------------------------------------------------------
# 3. gslides_create_presentation — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_create_presentation_happy_path(with_slides_stub):
    """Create returns the flat ``{presentation_id, url, title}``
    envelope ready for piping into get_outline / replace_all_text."""
    with_slides_stub.presentations().create().execute.return_value = {
        "presentationId": "DECK-NEW",
        "title": "Q3 Plan",
    }
    result = tools.gslides_create_presentation(title="Q3 Plan")
    assert result == {
        "presentation_id": "DECK-NEW",
        "url": "https://docs.google.com/presentation/d/DECK-NEW/edit",
        "title": "Q3 Plan",
    }


def test_gslides_create_presentation_rejects_blank_title(with_slides_stub):
    """Blank-title rejection from the api module bubbles up cleanly."""
    with pytest.raises(ValueError, match="title cannot be empty"):
        tools.gslides_create_presentation(title="   ")


# ---------------------------------------------------------------------
# 4. gslides_add_slide — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_add_slide_happy_path(with_slides_stub):
    """create+populate a slide → flat ``{presentation_id,
    slide_object_id, url}`` envelope through the decorator boundary."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"createSlide": {"objectId": "appscriptly_slide"}}, {}, {}],
    }
    result = tools.gslides_add_slide(
        presentation_id="DECK1", title="Overview", body="Details",
    )
    assert result == {
        "presentation_id": "DECK1",
        "slide_object_id": "appscriptly_slide",
        "url": (
            "https://docs.google.com/presentation/d/DECK1"
            "/edit#slide=id.appscriptly_slide"
        ),
    }


def test_gslides_add_slide_forwards_title_and_body_to_insertText(
    with_slides_stub,
):
    """The decorated tool forwards title + body into the api layer,
    which emits insertText requests carrying that exact text."""
    tools.gslides_add_slide(
        presentation_id="DECK1", title="T-text", body="B-text",
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    texts = {
        r["insertText"]["text"]
        for r in last.kwargs["body"]["requests"]
        if "insertText" in r
    }
    assert texts == {"T-text", "B-text"}


def test_gslides_add_slide_rejects_unsupported_layout(with_slides_stub):
    """Layout validation from the api module bubbles up cleanly."""
    with pytest.raises(ValueError, match="layout must be one of"):
        tools.gslides_add_slide(presentation_id="DECK1", layout="WRONG")


def test_gslides_add_slide_rejects_body_without_body_layout(with_slides_stub):
    """body + non-TITLE_AND_BODY layout is rejected before any API
    call, surfacing through the decorator envelope."""
    with pytest.raises(ValueError, match="body text requires a layout"):
        tools.gslides_add_slide(
            presentation_id="DECK1", body="x", layout="TITLE_ONLY",
        )


# ---------------------------------------------------------------------
# 5. gslides_create_image — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_create_image_happy_path(with_slides_stub):
    """Insert image → flat ``{presentation_id, slide_object_id,
    image_object_id, url}`` envelope through the decorator boundary."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"createImage": {"objectId": "appscriptly_image"}}],
    }
    result = tools.gslides_create_image(
        presentation_id="DECK1",
        slide_object_id="SLIDE1",
        image_url="https://example.com/x.png",
    )
    assert result == {
        "presentation_id": "DECK1",
        "slide_object_id": "SLIDE1",
        "image_object_id": "appscriptly_image",
        "url": (
            "https://docs.google.com/presentation/d/DECK1"
            "/edit#slide=id.SLIDE1"
        ),
    }


def test_gslides_create_image_forwards_url_to_createImage(with_slides_stub):
    tools.gslides_create_image(
        presentation_id="DECK1",
        slide_object_id="SLIDE1",
        image_url="https://example.com/logo.png",
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    req = last.kwargs["body"]["requests"][0]
    assert req["createImage"]["url"] == "https://example.com/logo.png"


def test_gslides_create_image_rejects_empty_url(with_slides_stub):
    with pytest.raises(ValueError, match="image_url cannot be empty"):
        tools.gslides_create_image(
            presentation_id="DECK1", slide_object_id="SLIDE1", image_url="",
        )


# ---------------------------------------------------------------------
# 6. gslides_create_table — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_create_table_happy_path(with_slides_stub):
    """Insert table → flat envelope echoing rows/columns + table id."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"createTable": {"objectId": "appscriptly_table"}}],
    }
    result = tools.gslides_create_table(
        presentation_id="DECK1",
        slide_object_id="SLIDE1",
        rows=3,
        columns=2,
    )
    assert result == {
        "presentation_id": "DECK1",
        "slide_object_id": "SLIDE1",
        "table_object_id": "appscriptly_table",
        "rows": 3,
        "columns": 2,
        "url": (
            "https://docs.google.com/presentation/d/DECK1"
            "/edit#slide=id.SLIDE1"
        ),
    }


def test_gslides_create_table_forwards_dimensions(with_slides_stub):
    tools.gslides_create_table(
        presentation_id="DECK1", slide_object_id="SLIDE1", rows=4, columns=5,
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    ct = last.kwargs["body"]["requests"][0]["createTable"]
    assert (ct["rows"], ct["columns"]) == (4, 5)


def test_gslides_create_table_rejects_subunit_dims(with_slides_stub):
    with pytest.raises(ValueError, match="rows and columns must each be >= 1"):
        tools.gslides_create_table(
            presentation_id="DECK1", slide_object_id="SLIDE1",
            rows=0, columns=2,
        )


# ---------------------------------------------------------------------
# 7. gslides_create_shape — happy path + validation (#155)
# ---------------------------------------------------------------------


def test_gslides_create_shape_happy_path(with_slides_stub):
    """Insert shape → flat envelope echoing shape_type + shape id."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"createShape": {"objectId": "appscriptly_shape"}}],
    }
    result = tools.gslides_create_shape(
        presentation_id="DECK1",
        slide_object_id="SLIDE1",
        shape_type="ELLIPSE",
    )
    assert result == {
        "presentation_id": "DECK1",
        "slide_object_id": "SLIDE1",
        "shape_object_id": "appscriptly_shape",
        "shape_type": "ELLIPSE",
        "url": (
            "https://docs.google.com/presentation/d/DECK1"
            "/edit#slide=id.SLIDE1"
        ),
    }


def test_gslides_create_shape_forwards_shape_type(with_slides_stub):
    tools.gslides_create_shape(
        presentation_id="DECK1", slide_object_id="SLIDE1",
        shape_type="ROUND_RECTANGLE",
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    cs = last.kwargs["body"]["requests"][0]["createShape"]
    assert cs["shapeType"] == "ROUND_RECTANGLE"


def test_gslides_create_shape_rejects_unsupported_type(with_slides_stub):
    with pytest.raises(ValueError, match="shape_type must be one of"):
        tools.gslides_create_shape(
            presentation_id="DECK1", slide_object_id="SLIDE1",
            shape_type="WRONG",
        )


# ---------------------------------------------------------------------
# 8. gslides_create_line — happy path + validation (#155)
# ---------------------------------------------------------------------


def test_gslides_create_line_happy_path(with_slides_stub):
    """Draw line → flat envelope echoing line_category + line id."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"createLine": {"objectId": "appscriptly_line"}}],
    }
    result = tools.gslides_create_line(
        presentation_id="DECK1",
        slide_object_id="SLIDE1",
    )
    assert result == {
        "presentation_id": "DECK1",
        "slide_object_id": "SLIDE1",
        "line_object_id": "appscriptly_line",
        "line_category": "STRAIGHT",
        "url": (
            "https://docs.google.com/presentation/d/DECK1"
            "/edit#slide=id.SLIDE1"
        ),
    }


def test_gslides_create_line_forwards_points_as_bbox(with_slides_stub):
    """start/end points reach the api layer, which emits a createLine
    with a bounding box derived from the point delta."""
    tools.gslides_create_line(
        presentation_id="DECK1", slide_object_id="SLIDE1",
        start_x_inches=2, start_y_inches=2,
        end_x_inches=4, end_y_inches=5,
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    ep = last.kwargs["body"]["requests"][0]["createLine"]["elementProperties"]
    _emu = 914400
    assert ep["transform"]["translateX"] == 2 * _emu
    assert ep["size"]["width"] == {"magnitude": 2 * _emu, "unit": "EMU"}
    assert ep["size"]["height"] == {"magnitude": 3 * _emu, "unit": "EMU"}


def test_gslides_create_line_rejects_zero_length(with_slides_stub):
    with pytest.raises(ValueError, match="start and end points are identical"):
        tools.gslides_create_line(
            presentation_id="DECK1", slide_object_id="SLIDE1",
            start_x_inches=1, start_y_inches=1,
            end_x_inches=1, end_y_inches=1,
        )


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: _get_credentials_fn is invoked
# ---------------------------------------------------------------------


def test_gslides_get_outline_invokes_get_credentials_fn(
    with_slides_stub, monkeypatch,
):
    """Canary identical to the sheets test_tools.py pattern: the
    @workspace_tool(creds=True) decorator MUST call
    _get_credentials_fn before delegating to the body."""
    call_count = {"n": 0}

    def counting_creds_fn():
        call_count["n"] += 1
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(
        decorators, "_get_credentials_fn", counting_creds_fn
    )
    tools.gslides_get_outline(presentation_id="DECK1")
    assert call_count["n"] == 1, (
        "_get_credentials_fn was not called exactly once — the "
        "decorator envelope may have changed or the fixture missed."
    )


# ---------------------------------------------------------------------
# gslides_set_speaker_notes — happy path through the decorator envelope
# ---------------------------------------------------------------------


def test_gslides_set_speaker_notes_happy_path(with_slides_stub):
    """The tool resolves the slide's notes shape and returns the
    {presentation_id, slide_object_id, speaker_notes_object_id,
    notes_text} envelope through the standard creds=True boundary."""
    with_slides_stub.presentations().get().execute.return_value = {
        "presentationId": "DECK1",
        "slides": [
            {
                "objectId": "SL1",
                "slideProperties": {
                    "notesPage": {
                        "notesProperties": {
                            "speakerNotesObjectId": "NOTES1",
                        },
                    },
                },
            },
        ],
    }
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "replies": [],
    }
    result = tools.gslides_set_speaker_notes(
        presentation_id="DECK1",
        slide_object_id="SL1",
        notes_text="Presenter script here",
    )
    assert result == {
        "presentation_id": "DECK1",
        "slide_object_id": "SL1",
        "speaker_notes_object_id": "NOTES1",
        "notes_text": "Presenter script here",
    }


# ---------------------------------------------------------------------
# 10. gslides_delete_object - happy path + validation
# ---------------------------------------------------------------------


def test_gslides_delete_object_happy_path(with_slides_stub):
    """Delete an element -> flat {presentation_id, deleted_object_id}
    envelope through the decorator boundary."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{}],
    }
    result = tools.gslides_delete_object(
        presentation_id="DECK1", object_id="SHAPE_9",
    )
    assert result == {
        "presentation_id": "DECK1",
        "deleted_object_id": "SHAPE_9",
    }


def test_gslides_delete_object_rejects_empty_object_id(with_slides_stub):
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        tools.gslides_delete_object(presentation_id="DECK1", object_id="")


# ---------------------------------------------------------------------
# 11. gslides_duplicate_object - happy path + validation
# ---------------------------------------------------------------------


def test_gslides_duplicate_object_happy_path(with_slides_stub):
    """Duplicate -> envelope carrying the new objectId + the id map."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"duplicateObject": {"objectId": "COPY_1"}}],
    }
    result = tools.gslides_duplicate_object(
        presentation_id="DECK1", object_id="SRC_5",
    )
    assert result == {
        "presentation_id": "DECK1",
        "source_object_id": "SRC_5",
        "new_object_id": "COPY_1",
        "id_map": {"SRC_5": "COPY_1"},
    }


def test_gslides_duplicate_object_rejects_empty_object_id(with_slides_stub):
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        tools.gslides_duplicate_object(presentation_id="DECK1", object_id="")


# ---------------------------------------------------------------------
# 12. gslides_update_element_transform - happy path + validation
# ---------------------------------------------------------------------


def test_gslides_update_element_transform_happy_path(with_slides_stub):
    """Move an element -> envelope echoing the resolved mode + matrix."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{}],
    }
    result = tools.gslides_update_element_transform(
        presentation_id="DECK1",
        object_id="OBJ_2",
        translate_x_emu=100000,
        apply_mode="RELATIVE",
    )
    assert result == {
        "presentation_id": "DECK1",
        "object_id": "OBJ_2",
        "apply_mode": "RELATIVE",
        "transform": {
            "scaleX": 1.0,
            "scaleY": 1.0,
            "translateX": 100000,
            "translateY": 0.0,
            "unit": "EMU",
        },
    }


def test_gslides_update_element_transform_forwards_apply_mode(with_slides_stub):
    """The apply_mode parameter reaches the updatePageElementTransform
    request unchanged (default is RELATIVE; ABSOLUTE forwards)."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{}],
    }
    tools.gslides_update_element_transform(
        presentation_id="DECK1", object_id="OBJ_2", apply_mode="ABSOLUTE",
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    req = last.kwargs["body"]["requests"][0]["updatePageElementTransform"]
    assert req["applyMode"] == "ABSOLUTE"


def test_gslides_update_element_transform_rejects_bad_apply_mode(
    with_slides_stub,
):
    with pytest.raises(ValueError, match="apply_mode must be one of"):
        tools.gslides_update_element_transform(
            presentation_id="DECK1", object_id="OBJ1", apply_mode="NOPE",
        )


# ---------------------------------------------------------------------
# 13. gslides_insert_text - happy path + forwarding + validation (Wave 5 S2)
# ---------------------------------------------------------------------


def test_gslides_insert_text_happy_path(with_slides_stub):
    """Insert text -> flat envelope naming the object + echoing the
    index + character count, through the @workspace_tool boundary."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{}],
    }
    result = tools.gslides_insert_text(
        presentation_id="DECK1",
        object_id="SHAPE_1",
        text="Hello",
    )
    assert result == {
        "presentation_id": "DECK1",
        "object_id": "SHAPE_1",
        "insertion_index": 0,
        "text_length": 5,
    }


def test_gslides_insert_text_forwards_args_to_insertText(with_slides_stub):
    """object_id, text, and insertion_index reach the insertText request
    unchanged."""
    tools.gslides_insert_text(
        presentation_id="DECK1", object_id="SHAPE_7", text="shape copy",
        insertion_index=3,
    )
    last = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    req = last.kwargs["body"]["requests"][0]["insertText"]
    assert req == {
        "objectId": "SHAPE_7",
        "text": "shape copy",
        "insertionIndex": 3,
    }


def test_gslides_insert_text_rejects_empty_object_id(with_slides_stub):
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        tools.gslides_insert_text(
            presentation_id="DECK1", object_id="", text="hi",
        )


def test_gslides_insert_text_rejects_empty_text(with_slides_stub):
    with pytest.raises(ValueError, match="text cannot be empty"):
        tools.gslides_insert_text(
            presentation_id="DECK1", object_id="SHAPE_1", text="",
        )
