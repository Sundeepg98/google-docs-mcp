"""Co-located tests for services/slides/api.py (v2.3.2).

Mirrors ``tests/unit/services/sheets/test_api.py`` (PR #119): exercise
the module via ``with_google_api_client(InMemoryGoogleAPIClient)`` so
the real ``get_service`` chokepoint runs but Slides' HTTP boundary
is stubbed. No real OAuth, no real Slides round-trip.

Tests cover four surfaces:

1. **Pre-API validation** — ``replace_all_text``'s ``ValueError``
   branch for empty ``find_text``; ``create_presentation``'s blank-
   title rejection.
2. **``_extract_slide_text`` pure helper** — flattens text runs from
   the nested ``shape.text.textElements[].textRun`` structure;
   handles empty shapes, missing keys, and image-only slides.
3. **Slides call shape** — the right method chain receives the
   right kwargs: ``presentations.get(presentationId=...)``,
   ``presentations.batchUpdate(body={"requests": [...]})`` with the
   ``replaceAllText`` request type, ``presentations.create(body={
   "title": ...})``.
4. **Response envelope shape** — the flat ``{presentation_id, title,
   url, slides}`` / ``{presentation_id, occurrences_changed}`` /
   ``{presentation_id, url, title}`` envelopes the tool layer
   surfaces.

The empirical-validation framing of v2.3.2: this test file is the
3rd consecutive proof that the M2 chokepoint + per-service-folder
pattern + M4 ``@workspace_tool`` annotation surface scale to a NEW
Google service without infrastructure rework. After Drive sharing
(PR #117 — sub-module), Sheets (PR #119 — new service), this PR's
Slides is the triply-validated case.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.slides.api import (
    _extract_slide_text,
    create_presentation,
    get_outline,
    replace_all_text,
)


# ---------------------------------------------------------------------
# _extract_slide_text — pure helper exercised directly
# ---------------------------------------------------------------------


def test_extract_slide_text_returns_empty_for_slide_without_pageElements():
    """A slide with no ``pageElements`` (rare; e.g. a freshly-added
    blank slide) returns the empty string, not None / KeyError."""
    assert _extract_slide_text({}) == ""
    assert _extract_slide_text({"pageElements": []}) == ""
    assert _extract_slide_text({"pageElements": None}) == ""


def test_extract_slide_text_skips_elements_without_shape():
    """Elements without a ``shape`` (images, embedded charts, etc.)
    carry no slide-level copy text and must be skipped silently."""
    slide = {
        "pageElements": [
            {"image": {"imageProperties": {}}},  # no shape key
            {"sheetsChart": {}},                  # no shape key
            {"video": {}},                        # no shape key
        ],
    }
    assert _extract_slide_text(slide) == ""


def test_extract_slide_text_extracts_text_from_shape_text_runs():
    """The Slides text model is nested ~4 levels deep —
    pageElements[].shape.text.textElements[].textRun.content.
    Verify the helper walks all of it and concatenates the runs."""
    slide = {
        "pageElements": [
            {
                "shape": {
                    "text": {
                        "textElements": [
                            {"textRun": {"content": "Hello "}},
                            {"textRun": {"content": "World"}},
                        ],
                    },
                },
            },
        ],
    }
    assert _extract_slide_text(slide) == "Hello World"


def test_extract_slide_text_handles_textElements_without_textRun():
    """Slides puts other entry types in ``textElements`` too —
    paragraphMarker entries denote paragraph boundaries, not text.
    Entries without a ``textRun`` key must be skipped without error."""
    slide = {
        "pageElements": [
            {
                "shape": {
                    "text": {
                        "textElements": [
                            {"paragraphMarker": {}},
                            {"textRun": {"content": "Header"}},
                            {"paragraphMarker": {}},
                            {"textRun": {"content": " body"}},
                        ],
                    },
                },
            },
        ],
    }
    assert _extract_slide_text(slide) == "Header body"


def test_extract_slide_text_joins_text_across_multiple_shapes():
    """Multiple shapes per slide (title + body + footer) all
    contribute text; the helper concatenates across shapes."""
    slide = {
        "pageElements": [
            {"shape": {"text": {"textElements": [
                {"textRun": {"content": "Title\n"}}]}}},
            {"shape": {"text": {"textElements": [
                {"textRun": {"content": "Body content"}}]}}},
        ],
    }
    assert _extract_slide_text(slide) == "Title\nBody content"


def test_extract_slide_text_strips_trailing_whitespace():
    """Slides often appends a trailing ``\\n`` to text runs. The
    helper trims it so consumers don't have to."""
    slide = {
        "pageElements": [
            {"shape": {"text": {"textElements": [
                {"textRun": {"content": "Clean text   \n"}}]}}},
        ],
    }
    assert _extract_slide_text(slide) == "Clean text"


# ---------------------------------------------------------------------
# get_outline — Slides call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_slides_for_get():
    """A Slides v1 Resource stub whose
    presentations().get().execute() returns a plausible Slides
    response with two slides — one with text, one image-only."""
    slides = MagicMock(name="slides-v1-stub-get")
    slides.presentations().get().execute.return_value = {
        "presentationId": "DECK-1",
        "title": "Q2 Forecast",
        "slides": [
            {
                "objectId": "SLIDE_001",
                "slideProperties": {"layoutObjectId": "LAYOUT_TITLE"},
                "pageElements": [
                    {"shape": {"text": {"textElements": [
                        {"textRun": {"content": "Welcome to Q2"}},
                    ]}}},
                ],
            },
            {
                "objectId": "SLIDE_002",
                "slideProperties": {"layoutObjectId": "LAYOUT_IMAGE"},
                "pageElements": [
                    {"image": {"imageProperties": {}}},
                ],
            },
        ],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def _last_get_kwargs(slides: MagicMock) -> dict:
    """The kwargs of the most recent presentations().get(...) call
    that actually carried a ``presentationId``."""
    for call in reversed(slides.presentations().get.call_args_list):
        if "presentationId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no presentations().get() call captured presentationId")


def test_get_outline_passes_presentationId_to_slides(stub_slides_for_get):
    """The Slides call must target the presentation_id the caller passed."""
    get_outline(MagicMock(), "DECK-ABC")
    kw = _last_get_kwargs(stub_slides_for_get)
    assert kw["presentationId"] == "DECK-ABC"


def test_get_outline_returns_flat_envelope(stub_slides_for_get):
    """Maps Slides' ``title`` directly + synthesizes the canonical
    presentation URL from the ID. Each per-slide entry has
    ``object_id`` + ``layout`` + flattened ``text``."""
    result = get_outline(MagicMock(), "DECK-1")
    assert result["presentation_id"] == "DECK-1"
    assert result["title"] == "Q2 Forecast"
    assert result["url"] == "https://docs.google.com/presentation/d/DECK-1/edit"
    assert len(result["slides"]) == 2
    # First slide: title slide with text
    assert result["slides"][0] == {
        "object_id": "SLIDE_001",
        "layout": "LAYOUT_TITLE",
        "text": "Welcome to Q2",
    }
    # Second slide: image-only, text is empty string (not None)
    assert result["slides"][1] == {
        "object_id": "SLIDE_002",
        "layout": "LAYOUT_IMAGE",
        "text": "",
    }


def test_get_outline_returns_empty_slides_list_for_deckless_presentation(
    stub_slides_for_get,
):
    """A presentation with no slides (rare but possible) returns
    ``slides: []`` rather than missing the key."""
    stub_slides_for_get.presentations().get().execute.return_value = {
        "presentationId": "DECK-X",
        "title": "Empty",
        # No ``slides`` key
    }
    result = get_outline(MagicMock(), "DECK-X")
    assert result["slides"] == []


def test_get_outline_defaults_title_to_empty_when_omitted(stub_slides_for_get):
    """Defensive: if Slides ever omits ``title`` from the response
    (shouldn't, but the SDK contract permits it), the envelope
    falls back to empty string."""
    stub_slides_for_get.presentations().get().execute.return_value = {
        "presentationId": "DECK-Y",
        "slides": [],
    }
    result = get_outline(MagicMock(), "DECK-Y")
    assert result["title"] == ""


def test_get_outline_defaults_layout_when_slideProperties_missing(
    stub_slides_for_get,
):
    """A slide without ``slideProperties`` (or without
    ``layoutObjectId`` inside it) gets ``layout: ""`` rather than
    KeyError. Common in legacy presentations imported from
    PowerPoint."""
    stub_slides_for_get.presentations().get().execute.return_value = {
        "presentationId": "DECK-Z",
        "title": "T",
        "slides": [
            {"objectId": "S1"},  # no slideProperties at all
            {"objectId": "S2", "slideProperties": {}},  # empty slideProperties
        ],
    }
    result = get_outline(MagicMock(), "DECK-Z")
    assert result["slides"][0]["layout"] == ""
    assert result["slides"][1]["layout"] == ""


# ---------------------------------------------------------------------
# replace_all_text — pre-API validation + Slides call shape + envelope
# ---------------------------------------------------------------------


def test_replace_all_text_rejects_empty_find_text():
    """Empty ``find_text`` is a caller bug — Slides would 400 with a
    less helpful message. Reject client-side."""
    with pytest.raises(ValueError, match="find_text cannot be empty"):
        replace_all_text(MagicMock(), "DECK1", "", "Replacement")


@pytest.fixture
def stub_slides_for_replace():
    slides = MagicMock(name="slides-v1-stub-replace")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 3}},
        ],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def _last_batchUpdate_kwargs(slides: MagicMock) -> dict:
    for call in reversed(slides.presentations().batchUpdate.call_args_list):
        if "presentationId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no presentations().batchUpdate() call captured presentationId")


def test_replace_all_text_passes_presentationId_to_slides(
    stub_slides_for_replace,
):
    replace_all_text(MagicMock(), "DECK-XYZ", "old", "new")
    kw = _last_batchUpdate_kwargs(stub_slides_for_replace)
    assert kw["presentationId"] == "DECK-XYZ"


def test_replace_all_text_builds_single_replaceAllText_request(
    stub_slides_for_replace,
):
    """The body must wrap a SINGLE ``replaceAllText`` request inside
    ``{requests: [...]}`` — the carve-out from the full batchUpdate
    tagged-union. The ``containsText`` sub-object holds find_text +
    matchCase; ``replaceText`` is the destination string."""
    replace_all_text(
        MagicMock(), "DECK1", "{{Name}}", "Acme Corp",
    )
    kw = _last_batchUpdate_kwargs(stub_slides_for_replace)
    assert kw["body"] == {
        "requests": [
            {
                "replaceAllText": {
                    "containsText": {
                        "text": "{{Name}}",
                        "matchCase": True,
                    },
                    "replaceText": "Acme Corp",
                },
            },
        ],
    }


def test_replace_all_text_default_match_case_is_True(stub_slides_for_replace):
    """Default ``match_case=True`` → ``matchCase=True``. Slides'
    default is also True; passing it explicitly preserves intent."""
    replace_all_text(MagicMock(), "DECK1", "foo", "bar")
    kw = _last_batchUpdate_kwargs(stub_slides_for_replace)
    assert kw["body"]["requests"][0]["replaceAllText"]["containsText"]["matchCase"] is True


def test_replace_all_text_match_case_false_propagates(stub_slides_for_replace):
    """``match_case=False`` → ``matchCase=False`` — case-insensitive
    matching at the Slides API level."""
    replace_all_text(MagicMock(), "DECK1", "foo", "bar", match_case=False)
    kw = _last_batchUpdate_kwargs(stub_slides_for_replace)
    assert kw["body"]["requests"][0]["replaceAllText"]["containsText"]["matchCase"] is False


def test_replace_all_text_allows_empty_replace_text(stub_slides_for_replace):
    """Empty ``replace_text`` is valid — effectively deletes the
    matched text. Only ``find_text`` is forbidden from being empty."""
    replace_all_text(MagicMock(), "DECK1", "DELETE_ME", "")
    kw = _last_batchUpdate_kwargs(stub_slides_for_replace)
    assert kw["body"]["requests"][0]["replaceAllText"]["replaceText"] == ""


def test_replace_all_text_returns_flat_envelope(stub_slides_for_replace):
    """The returned dict is the flat ``{presentation_id,
    occurrences_changed}`` envelope. ``occurrences_changed`` sums
    the count across the batchUpdate's ``replies``."""
    result = replace_all_text(MagicMock(), "DECK-1", "old", "new")
    assert result == {
        "presentation_id": "DECK-1",
        "occurrences_changed": 3,
    }


def test_replace_all_text_returns_zero_occurrences_for_no_match(
    stub_slides_for_replace,
):
    """When Slides finds nothing to replace, ``occurrencesChanged``
    can be 0 OR Slides may omit the ``replyAllText`` block entirely.
    Both must yield ``occurrences_changed: 0`` (not KeyError)."""
    stub_slides_for_replace.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],  # Slides returned a reply without replaceAllText
    }
    result = replace_all_text(MagicMock(), "DECK-1", "nothing", "x")
    assert result["occurrences_changed"] == 0


def test_replace_all_text_handles_empty_replies_array(stub_slides_for_replace):
    """Defensive: if Slides ever omits ``replies`` entirely, the
    envelope falls back to 0 rather than KeyError."""
    stub_slides_for_replace.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
    }
    result = replace_all_text(MagicMock(), "DECK-1", "x", "y")
    assert result["occurrences_changed"] == 0


# ---------------------------------------------------------------------
# create_presentation — pre-API validation + Slides call shape + envelope
# ---------------------------------------------------------------------


def test_create_presentation_rejects_blank_title():
    """Empty / whitespace title rejected client-side."""
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_presentation(MagicMock(), "")
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_presentation(MagicMock(), "   ")


@pytest.fixture
def stub_slides_for_create():
    slides = MagicMock(name="slides-v1-stub-create")
    slides.presentations().create().execute.return_value = {
        "presentationId": "NEW-DECK-001",
        "title": "My Deck",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def _last_create_kwargs(slides: MagicMock) -> dict:
    for call in reversed(slides.presentations().create.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs
    raise AssertionError("no presentations().create() call captured body")


def test_create_presentation_builds_title_body(stub_slides_for_create):
    """The create body must wrap the title at the body root:
    ``{"title": "..."}``. Slides' shape is FLATTER than Sheets'
    create (which nests under ``properties.title``)."""
    create_presentation(MagicMock(), "My Deck")
    kw = _last_create_kwargs(stub_slides_for_create)
    assert kw["body"] == {"title": "My Deck"}


def test_create_presentation_strips_whitespace_from_title(stub_slides_for_create):
    """Leading / trailing whitespace stripped before the Slides
    call, so the created deck's Drive name doesn't have surprise
    spaces."""
    create_presentation(MagicMock(), "  My Deck  ")
    kw = _last_create_kwargs(stub_slides_for_create)
    assert kw["body"]["title"] == "My Deck"


def test_create_presentation_returns_flat_envelope(stub_slides_for_create):
    """Maps Slides' ``presentationId`` → ``presentation_id`` (snake_case)
    and synthesizes the canonical URL from the ID."""
    result = create_presentation(MagicMock(), "My Deck")
    assert result == {
        "presentation_id": "NEW-DECK-001",
        "url": "https://docs.google.com/presentation/d/NEW-DECK-001/edit",
        "title": "My Deck",
    }


def test_create_presentation_falls_back_to_input_title_when_omitted(
    stub_slides_for_create,
):
    """Defensive: if Slides ever omits the title from its response,
    the envelope falls back to the (stripped) input title."""
    stub_slides_for_create.presentations().create().execute.return_value = {
        "presentationId": "ABC123",
        # No ``title`` key.
    }
    result = create_presentation(MagicMock(), "  Fallback Title  ")
    assert result["title"] == "Fallback Title"
