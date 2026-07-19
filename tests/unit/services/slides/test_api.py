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
from googleapiclient.errors import HttpError

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.slides.api import (
    _classify_page_element,
    _extract_slide_notes,
    _extract_slide_text,
    _list_slide_elements,
    add_slide,
    create_image,
    create_line,
    create_presentation,
    create_shape,
    create_table,
    delete_object,
    duplicate_object,
    get_outline,
    insert_text,
    replace_all_text,
    set_speaker_notes,
    update_element_transform,
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
    ``object_id`` + 0-based ``index`` + ``layout`` + flattened ``text``
    + an ``elements`` inventory + speaker ``notes``."""
    result = get_outline(MagicMock(), "DECK-1")
    assert result["presentation_id"] == "DECK-1"
    assert result["title"] == "Q2 Forecast"
    assert result["url"] == "https://docs.google.com/presentation/d/DECK-1/edit"
    assert len(result["slides"]) == 2
    # First slide: title slide with text. index 0; one shape element; no
    # notesPage in the stub -> notes empty string.
    assert result["slides"][0] == {
        "object_id": "SLIDE_001",
        "index": 0,
        "layout": "LAYOUT_TITLE",
        "text": "Welcome to Q2",
        "elements": [{"object_id": "", "type": "shape"}],
        "notes": "",
    }
    # Second slide: image-only, text is empty string (not None). index 1;
    # one image element classified as "image".
    assert result["slides"][1] == {
        "object_id": "SLIDE_002",
        "index": 1,
        "layout": "LAYOUT_IMAGE",
        "text": "",
        "elements": [{"object_id": "", "type": "image"}],
        "notes": "",
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
    # Defensive defaults for the enrichment fields too: a slide with no
    # pageElements / notesPage yields empty elements + empty notes, and
    # the index reflects position.
    assert result["slides"][0]["index"] == 0
    assert result["slides"][0]["elements"] == []
    assert result["slides"][0]["notes"] == ""
    assert result["slides"][1]["index"] == 1


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


# ---------------------------------------------------------------------
# add_slide — pre-API validation + Slides call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_slides_for_add():
    slides = MagicMock(name="slides-v1-stub-add")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [
            {"createSlide": {"objectId": "appscriptly_slide"}},
            {},  # insertText replies carry no objectId
            {},
        ],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_add_slide_rejects_unsupported_layout():
    """An unsupported ``layout`` enum is a caller bug — reject
    client-side with the supported set, rather than a Slides 400."""
    with pytest.raises(ValueError, match="layout must be one of"):
        add_slide(MagicMock(), "DECK1", layout="NONSENSE")


def test_add_slide_rejects_body_for_layout_without_body_placeholder():
    """``body`` text needs a BODY placeholder — only TITLE_AND_BODY has
    one. Passing body with TITLE_ONLY / BLANK is rejected up front."""
    with pytest.raises(ValueError, match="body text requires a layout"):
        add_slide(MagicMock(), "DECK1", body="Some body", layout="TITLE_ONLY")
    with pytest.raises(ValueError, match="body text requires a layout"):
        add_slide(MagicMock(), "DECK1", body="Some body", layout="BLANK")


def test_add_slide_passes_presentationId_to_slides(stub_slides_for_add):
    add_slide(MagicMock(), "DECK-XYZ", title="Hi")
    kw = _last_batchUpdate_kwargs(stub_slides_for_add)
    assert kw["presentationId"] == "DECK-XYZ"


def test_add_slide_builds_createSlide_with_placeholder_mappings(
    stub_slides_for_add,
):
    """TITLE_AND_BODY + title + body → one createSlide carrying BOTH
    placeholderIdMappings, then two insertText requests targeting the
    deterministic placeholder IDs."""
    add_slide(
        MagicMock(), "DECK1",
        title="My Title", body="My Body", layout="TITLE_AND_BODY",
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_add)["body"]["requests"]
    # createSlide first
    cs = reqs[0]["createSlide"]
    assert cs["slideLayoutReference"] == {"predefinedLayout": "TITLE_AND_BODY"}
    mappings = cs["placeholderIdMappings"]
    types = {m["layoutPlaceholder"]["type"] for m in mappings}
    assert types == {"TITLE", "BODY"}
    # two insertText requests, targeting the mapped object IDs
    insert_targets = {
        r["insertText"]["objectId"]: r["insertText"]["text"]
        for r in reqs if "insertText" in r
    }
    title_id = next(
        m["objectId"] for m in mappings
        if m["layoutPlaceholder"]["type"] == "TITLE"
    )
    body_id = next(
        m["objectId"] for m in mappings
        if m["layoutPlaceholder"]["type"] == "BODY"
    )
    assert insert_targets[title_id] == "My Title"
    assert insert_targets[body_id] == "My Body"


def test_add_slide_title_only_omits_body_placeholder_and_insert(
    stub_slides_for_add,
):
    """layout=TITLE_ONLY with a title → exactly one placeholder
    mapping (TITLE) and one insertText; no BODY anywhere."""
    add_slide(MagicMock(), "DECK1", title="Just a title", layout="TITLE_ONLY")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_add)["body"]["requests"]
    cs = reqs[0]["createSlide"]
    mappings = cs.get("placeholderIdMappings", [])
    assert [m["layoutPlaceholder"]["type"] for m in mappings] == ["TITLE"]
    inserts = [r for r in reqs if "insertText" in r]
    assert len(inserts) == 1
    assert inserts[0]["insertText"]["text"] == "Just a title"


def test_add_slide_blank_layout_has_no_placeholders_or_inserts(
    stub_slides_for_add,
):
    """layout=BLANK with no title/body → a bare createSlide, no
    placeholderIdMappings, no insertText requests."""
    add_slide(MagicMock(), "DECK1", layout="BLANK")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_add)["body"]["requests"]
    assert len(reqs) == 1
    assert "placeholderIdMappings" not in reqs[0]["createSlide"]


def test_add_slide_empty_title_skips_title_insert(stub_slides_for_add):
    """A falsy title (None / "") does not produce a TITLE placeholder
    or insertText — even on a TITLE_AND_BODY layout."""
    add_slide(MagicMock(), "DECK1", title="", body="Body only")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_add)["body"]["requests"]
    cs = reqs[0]["createSlide"]
    types = [m["layoutPlaceholder"]["type"] for m in cs.get("placeholderIdMappings", [])]
    assert types == ["BODY"]
    inserts = [r for r in reqs if "insertText" in r]
    assert len(inserts) == 1
    assert inserts[0]["insertText"]["text"] == "Body only"


def test_add_slide_returns_flat_envelope(stub_slides_for_add):
    """Flat ``{presentation_id, slide_object_id, url}`` envelope; the
    slide_object_id comes from the createSlide reply and the url
    deep-links to that slide."""
    result = add_slide(MagicMock(), "DECK-1", title="X")
    assert result == {
        "presentation_id": "DECK-1",
        "slide_object_id": "appscriptly_slide",
        "url": (
            "https://docs.google.com/presentation/d/DECK-1"
            "/edit#slide=id.appscriptly_slide"
        ),
    }


def test_add_slide_falls_back_to_requested_id_when_reply_omits_it(
    stub_slides_for_add,
):
    """Defensive: if Slides' reply omits the createSlide objectId, the
    envelope falls back to the deterministic ID we requested."""
    stub_slides_for_add.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],  # no createSlide objectId echoed
    }
    result = add_slide(MagicMock(), "DECK-1", title="X")
    # The requested id is now unique-per-call (appscriptly_slide_<hex>);
    # the fallback must echo that requested id, not a constant.
    assert result["slide_object_id"].startswith("appscriptly_slide_")


def _created_object_ids(slides: MagicMock, request_key: str) -> list[str]:
    """All requested objectIds for ``request_key`` (e.g. 'createSlide')
    across EVERY batchUpdate call captured on the stub — so a 2nd call's
    id can be compared against the 1st's."""
    ids: list[str] = []
    for call in slides.presentations().batchUpdate.call_args_list:
        if "presentationId" not in call.kwargs:
            continue
        for req in call.kwargs["body"]["requests"]:
            if request_key in req:
                ids.append(req[request_key]["objectId"])
    return ids


def test_add_slide_twice_on_same_deck_uses_unique_object_ids(
    stub_slides_for_add,
):
    """REGRESSION (HIGH): a constant slide objectId 400s 'object ID already
    in use' on the 2nd add_slide against the same deck. Two calls must
    request DISTINCT createSlide objectIds (and distinct placeholder ids),
    so repeated calls succeed."""
    add_slide(MagicMock(), "DECK-SAME", title="First", body="B1",
              layout="TITLE_AND_BODY")
    add_slide(MagicMock(), "DECK-SAME", title="Second", body="B2",
              layout="TITLE_AND_BODY")

    slide_ids = _created_object_ids(stub_slides_for_add, "createSlide")
    assert len(slide_ids) == 2
    assert slide_ids[0] != slide_ids[1], (
        f"both add_slide calls requested the SAME slide objectId "
        f"({slide_ids[0]!r}) — the 2nd would 400 'object ID already in use'."
    )
    assert all(sid.startswith("appscriptly_slide_") for sid in slide_ids)

    # Placeholder ids must also differ across the two calls (they live in
    # the same presentation namespace and would collide too).
    call_lists = [
        c.kwargs["body"]["requests"]
        for c in stub_slides_for_add.presentations().batchUpdate.call_args_list
        if "presentationId" in c.kwargs
    ]
    ph_ids_call1 = {
        m["objectId"]
        for m in call_lists[0][0]["createSlide"].get("placeholderIdMappings", [])
    }
    ph_ids_call2 = {
        m["objectId"]
        for m in call_lists[1][0]["createSlide"].get("placeholderIdMappings", [])
    }
    assert ph_ids_call1.isdisjoint(ph_ids_call2), (
        f"placeholder ids collide across calls: {ph_ids_call1 & ph_ids_call2}"
    )


# ---------------------------------------------------------------------
# create_image — validation + Slides call shape + envelope
# ---------------------------------------------------------------------

_EMU = 914400  # EMU per inch — mirrors api._EMU_PER_INCH


@pytest.fixture
def stub_slides_for_image():
    slides = MagicMock(name="slides-v1-stub-image")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{"createImage": {"objectId": "appscriptly_image"}}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_create_image_rejects_empty_url():
    with pytest.raises(ValueError, match="image_url cannot be empty"):
        create_image(MagicMock(), "DECK1", "SLIDE1", "")


def test_create_image_rejects_empty_slide_object_id():
    with pytest.raises(ValueError, match="slide_object_id cannot be empty"):
        create_image(MagicMock(), "DECK1", "", "https://x/y.png")


def test_create_image_rejects_nonpositive_dimensions():
    with pytest.raises(ValueError, match="must be positive"):
        create_image(
            MagicMock(), "DECK1", "SLIDE1", "https://x/y.png", width_inches=0,
        )
    with pytest.raises(ValueError, match="must be positive"):
        create_image(
            MagicMock(), "DECK1", "SLIDE1", "https://x/y.png", height_inches=-1,
        )


def test_create_image_passes_presentationId(stub_slides_for_image):
    create_image(MagicMock(), "DECK-XYZ", "SLIDE1", "https://x/y.png")
    kw = _last_batchUpdate_kwargs(stub_slides_for_image)
    assert kw["presentationId"] == "DECK-XYZ"


def test_create_image_builds_createImage_request_with_url_and_placement(
    stub_slides_for_image,
):
    """The createImage request carries the url + an elementProperties
    block pinning the image to the slide with EMU size + transform."""
    create_image(
        MagicMock(), "DECK1", "SLIDE_7", "https://example.com/logo.png",
        width_inches=2, height_inches=1, x_inches=3, y_inches=4,
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_image)["body"]["requests"]
    assert len(reqs) == 1
    ci = reqs[0]["createImage"]
    assert ci["url"] == "https://example.com/logo.png"
    ep = ci["elementProperties"]
    assert ep["pageObjectId"] == "SLIDE_7"
    assert ep["size"]["width"] == {"magnitude": 2 * _EMU, "unit": "EMU"}
    assert ep["size"]["height"] == {"magnitude": 1 * _EMU, "unit": "EMU"}
    assert ep["transform"]["translateX"] == 3 * _EMU
    assert ep["transform"]["translateY"] == 4 * _EMU
    assert ep["transform"]["unit"] == "EMU"


def test_create_image_returns_flat_envelope(stub_slides_for_image):
    result = create_image(
        MagicMock(), "DECK-1", "SLIDE_1", "https://x/y.png",
    )
    assert result == {
        "presentation_id": "DECK-1",
        "slide_object_id": "SLIDE_1",
        "image_object_id": "appscriptly_image",
        "url": (
            "https://docs.google.com/presentation/d/DECK-1"
            "/edit#slide=id.SLIDE_1"
        ),
    }


def test_create_image_strips_whitespace_from_url(stub_slides_for_image):
    create_image(MagicMock(), "DECK1", "S1", "  https://x/y.png  ")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_image)["body"]["requests"]
    assert reqs[0]["createImage"]["url"] == "https://x/y.png"


def test_create_image_falls_back_to_requested_id_when_reply_omits_it(
    stub_slides_for_image,
):
    stub_slides_for_image.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    result = create_image(MagicMock(), "DECK-1", "S1", "https://x/y.png")
    # Unique-per-call requested id (appscriptly_image_<hex>).
    assert result["image_object_id"].startswith("appscriptly_image_")


def test_create_image_twice_on_same_deck_uses_unique_object_ids(
    stub_slides_for_image,
):
    """REGRESSION (HIGH): a constant image objectId 400s on the 2nd
    create_image against the same deck. Two calls must request DISTINCT
    createImage objectIds."""
    create_image(MagicMock(), "DECK-SAME", "S1", "https://x/a.png")
    create_image(MagicMock(), "DECK-SAME", "S1", "https://x/b.png")
    image_ids = _created_object_ids(stub_slides_for_image, "createImage")
    assert len(image_ids) == 2
    assert image_ids[0] != image_ids[1], (
        f"both create_image calls requested the SAME objectId "
        f"({image_ids[0]!r}) — the 2nd would 400 'object ID already in use'."
    )
    assert all(iid.startswith("appscriptly_image_") for iid in image_ids)


# ---------------------------------------------------------------------
# create_table — validation + Slides call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_slides_for_table():
    slides = MagicMock(name="slides-v1-stub-table")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{"createTable": {"objectId": "appscriptly_table"}}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_create_table_rejects_empty_slide_object_id():
    with pytest.raises(ValueError, match="slide_object_id cannot be empty"):
        create_table(MagicMock(), "DECK1", "", rows=2, columns=2)


def test_create_table_rejects_subunit_rows_or_columns():
    with pytest.raises(ValueError, match="rows and columns must each be >= 1"):
        create_table(MagicMock(), "DECK1", "S1", rows=0, columns=3)
    with pytest.raises(ValueError, match="rows and columns must each be >= 1"):
        create_table(MagicMock(), "DECK1", "S1", rows=3, columns=0)


def test_create_table_rejects_nonpositive_dimensions():
    with pytest.raises(ValueError, match="must be positive"):
        create_table(
            MagicMock(), "DECK1", "S1", rows=2, columns=2, width_inches=0,
        )


def test_create_table_builds_createTable_request(stub_slides_for_table):
    """The createTable request carries rows + columns + an
    elementProperties block pinning it to the slide."""
    create_table(
        MagicMock(), "DECK1", "SLIDE_9", rows=3, columns=4,
        width_inches=5, height_inches=2, x_inches=1, y_inches=1,
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_table)["body"]["requests"]
    assert len(reqs) == 1
    ct = reqs[0]["createTable"]
    assert ct["rows"] == 3
    assert ct["columns"] == 4
    assert ct["elementProperties"]["pageObjectId"] == "SLIDE_9"
    assert ct["elementProperties"]["size"]["width"] == {
        "magnitude": 5 * _EMU, "unit": "EMU",
    }


def test_create_table_returns_flat_envelope(stub_slides_for_table):
    result = create_table(MagicMock(), "DECK-1", "SLIDE_1", rows=2, columns=2)
    assert result == {
        "presentation_id": "DECK-1",
        "slide_object_id": "SLIDE_1",
        "table_object_id": "appscriptly_table",
        "rows": 2,
        "columns": 2,
        "url": (
            "https://docs.google.com/presentation/d/DECK-1"
            "/edit#slide=id.SLIDE_1"
        ),
    }


def test_create_table_falls_back_to_requested_id_when_reply_omits_it(
    stub_slides_for_table,
):
    stub_slides_for_table.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    result = create_table(MagicMock(), "DECK-1", "S1", rows=1, columns=1)
    # Unique-per-call requested id (appscriptly_table_<hex>).
    assert result["table_object_id"].startswith("appscriptly_table_")


def test_create_table_twice_on_same_deck_uses_unique_object_ids(
    stub_slides_for_table,
):
    """REGRESSION (HIGH): a constant table objectId 400s on the 2nd
    create_table against the same deck. Two calls must request DISTINCT
    createTable objectIds."""
    create_table(MagicMock(), "DECK-SAME", "S1", rows=2, columns=2)
    create_table(MagicMock(), "DECK-SAME", "S1", rows=3, columns=3)
    table_ids = _created_object_ids(stub_slides_for_table, "createTable")
    assert len(table_ids) == 2
    assert table_ids[0] != table_ids[1], (
        f"both create_table calls requested the SAME objectId "
        f"({table_ids[0]!r}) — the 2nd would 400 'object ID already in use'."
    )
    assert all(tid.startswith("appscriptly_table_") for tid in table_ids)


# ---------------------------------------------------------------------
# create_shape — validation + Slides call shape + envelope (#155)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_slides_for_shape():
    slides = MagicMock(name="slides-v1-stub-shape")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{"createShape": {"objectId": "appscriptly_shape"}}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_create_shape_rejects_empty_slide_object_id():
    with pytest.raises(ValueError, match="slide_object_id cannot be empty"):
        create_shape(MagicMock(), "DECK1", "", shape_type="RECTANGLE")


def test_create_shape_rejects_unsupported_shape_type():
    """An unsupported ``shape_type`` enum is a caller bug — reject
    client-side with the supported set, rather than a Slides 400."""
    with pytest.raises(ValueError, match="shape_type must be one of"):
        create_shape(MagicMock(), "DECK1", "S1", shape_type="NONSENSE")


def test_create_shape_rejects_nonpositive_dimensions():
    with pytest.raises(ValueError, match="must be positive"):
        create_shape(
            MagicMock(), "DECK1", "S1", shape_type="ELLIPSE", width_inches=0,
        )
    with pytest.raises(ValueError, match="must be positive"):
        create_shape(
            MagicMock(), "DECK1", "S1", shape_type="ELLIPSE", height_inches=-2,
        )


def test_create_shape_passes_presentationId(stub_slides_for_shape):
    create_shape(MagicMock(), "DECK-XYZ", "S1", shape_type="RECTANGLE")
    kw = _last_batchUpdate_kwargs(stub_slides_for_shape)
    assert kw["presentationId"] == "DECK-XYZ"


def test_create_shape_builds_createShape_request(stub_slides_for_shape):
    """The createShape request carries shapeType + an elementProperties
    block pinning the shape to the slide with EMU size + transform."""
    create_shape(
        MagicMock(), "DECK1", "SLIDE_9", shape_type="ELLIPSE",
        width_inches=3, height_inches=2, x_inches=1, y_inches=4,
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_shape)["body"]["requests"]
    assert len(reqs) == 1
    cs = reqs[0]["createShape"]
    assert cs["shapeType"] == "ELLIPSE"
    ep = cs["elementProperties"]
    assert ep["pageObjectId"] == "SLIDE_9"
    assert ep["size"]["width"] == {"magnitude": 3 * _EMU, "unit": "EMU"}
    assert ep["size"]["height"] == {"magnitude": 2 * _EMU, "unit": "EMU"}
    assert ep["transform"]["translateX"] == 1 * _EMU
    assert ep["transform"]["translateY"] == 4 * _EMU
    assert ep["transform"]["unit"] == "EMU"


def test_create_shape_defaults_to_rectangle(stub_slides_for_shape):
    """The default shape_type is RECTANGLE — the most common box."""
    create_shape(MagicMock(), "DECK1", "S1")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_shape)["body"]["requests"]
    assert reqs[0]["createShape"]["shapeType"] == "RECTANGLE"


def test_create_shape_returns_flat_envelope(stub_slides_for_shape):
    result = create_shape(
        MagicMock(), "DECK-1", "SLIDE_1", shape_type="TEXT_BOX",
    )
    assert result == {
        "presentation_id": "DECK-1",
        "slide_object_id": "SLIDE_1",
        "shape_object_id": "appscriptly_shape",
        "shape_type": "TEXT_BOX",
        "url": (
            "https://docs.google.com/presentation/d/DECK-1"
            "/edit#slide=id.SLIDE_1"
        ),
    }


def test_create_shape_falls_back_to_requested_id_when_reply_omits_it(
    stub_slides_for_shape,
):
    stub_slides_for_shape.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    result = create_shape(MagicMock(), "DECK-1", "S1", shape_type="DIAMOND")
    # Unique-per-call requested id (appscriptly_shape_<hex>).
    assert result["shape_object_id"].startswith("appscriptly_shape_")


def test_create_shape_twice_on_same_deck_uses_unique_object_ids(
    stub_slides_for_shape,
):
    """REGRESSION (HIGH): a constant shape objectId 400s on the 2nd
    create_shape against the same deck. Two calls must request DISTINCT
    createShape objectIds."""
    create_shape(MagicMock(), "DECK-SAME", "S1", shape_type="RECTANGLE")
    create_shape(MagicMock(), "DECK-SAME", "S1", shape_type="ELLIPSE")
    shape_ids = _created_object_ids(stub_slides_for_shape, "createShape")
    assert len(shape_ids) == 2
    assert shape_ids[0] != shape_ids[1], (
        f"both create_shape calls requested the SAME objectId "
        f"({shape_ids[0]!r}) — the 2nd would 400 'object ID already in use'."
    )
    assert all(sid.startswith("appscriptly_shape_") for sid in shape_ids)


# ---------------------------------------------------------------------
# create_line — validation + Slides call shape + envelope (#155)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_slides_for_line():
    slides = MagicMock(name="slides-v1-stub-line")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{"createLine": {"objectId": "appscriptly_line"}}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_create_line_rejects_empty_slide_object_id():
    with pytest.raises(ValueError, match="slide_object_id cannot be empty"):
        create_line(MagicMock(), "DECK1", "")


def test_create_line_rejects_unsupported_line_category():
    with pytest.raises(ValueError, match="line_category must be one of"):
        create_line(MagicMock(), "DECK1", "S1", line_category="ZIGZAG")


def test_create_line_rejects_zero_length_line():
    """Identical start + end is a degenerate (zero-length) line — Slides
    would 400 on the zero-area transform. Reject client-side."""
    with pytest.raises(ValueError, match="start and end points are identical"):
        create_line(
            MagicMock(), "DECK1", "S1",
            start_x_inches=2, start_y_inches=2,
            end_x_inches=2, end_y_inches=2,
        )


def test_create_line_passes_presentationId(stub_slides_for_line):
    create_line(MagicMock(), "DECK-XYZ", "S1")
    kw = _last_batchUpdate_kwargs(stub_slides_for_line)
    assert kw["presentationId"] == "DECK-XYZ"


def test_create_line_builds_createLine_request_with_bbox_from_points(
    stub_slides_for_line,
):
    """start (1,1) → end (5,3) ⇒ bounding box top-left at (1,1), size
    4in × 2in (the point delta). lineCategory rides on the request."""
    create_line(
        MagicMock(), "DECK1", "SLIDE_3",
        start_x_inches=1, start_y_inches=1,
        end_x_inches=5, end_y_inches=3,
        line_category="STRAIGHT",
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_line)["body"]["requests"]
    assert len(reqs) == 1
    cl = reqs[0]["createLine"]
    assert cl["lineCategory"] == "STRAIGHT"
    ep = cl["elementProperties"]
    assert ep["pageObjectId"] == "SLIDE_3"
    assert ep["size"]["width"] == {"magnitude": 4 * _EMU, "unit": "EMU"}
    assert ep["size"]["height"] == {"magnitude": 2 * _EMU, "unit": "EMU"}
    assert ep["transform"]["translateX"] == 1 * _EMU
    assert ep["transform"]["translateY"] == 1 * _EMU


def test_create_line_bbox_topleft_is_min_of_endpoints(stub_slides_for_line):
    """A line drawn 'backwards' (end above-left of start) still produces
    a positive-size box whose top-left is min(start, end) on each axis."""
    create_line(
        MagicMock(), "DECK1", "S1",
        start_x_inches=5, start_y_inches=4,
        end_x_inches=2, end_y_inches=1,
    )
    ep = _last_batchUpdate_kwargs(
        stub_slides_for_line
    )["body"]["requests"][0]["createLine"]["elementProperties"]
    assert ep["transform"]["translateX"] == 2 * _EMU
    assert ep["transform"]["translateY"] == 1 * _EMU
    assert ep["size"]["width"] == {"magnitude": 3 * _EMU, "unit": "EMU"}
    assert ep["size"]["height"] == {"magnitude": 3 * _EMU, "unit": "EMU"}


def test_create_line_horizontal_floors_height_to_one_emu(stub_slides_for_line):
    """A perfectly horizontal line has zero Y-delta; the height axis is
    floored to 1 EMU so Slides accepts the (otherwise zero-area) size."""
    create_line(
        MagicMock(), "DECK1", "S1",
        start_x_inches=1, start_y_inches=2,
        end_x_inches=5, end_y_inches=2,
    )
    ep = _last_batchUpdate_kwargs(
        stub_slides_for_line
    )["body"]["requests"][0]["createLine"]["elementProperties"]
    assert ep["size"]["height"] == {"magnitude": 1, "unit": "EMU"}
    assert ep["size"]["width"] == {"magnitude": 4 * _EMU, "unit": "EMU"}


def test_create_line_returns_flat_envelope(stub_slides_for_line):
    result = create_line(
        MagicMock(), "DECK-1", "SLIDE_1", line_category="BENT",
    )
    assert result == {
        "presentation_id": "DECK-1",
        "slide_object_id": "SLIDE_1",
        "line_object_id": "appscriptly_line",
        "line_category": "BENT",
        "url": (
            "https://docs.google.com/presentation/d/DECK-1"
            "/edit#slide=id.SLIDE_1"
        ),
    }


def test_create_line_falls_back_to_requested_id_when_reply_omits_it(
    stub_slides_for_line,
):
    stub_slides_for_line.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    result = create_line(MagicMock(), "DECK-1", "S1")
    # Unique-per-call requested id (appscriptly_line_<hex>).
    assert result["line_object_id"].startswith("appscriptly_line_")


def test_create_line_twice_on_same_deck_uses_unique_object_ids(
    stub_slides_for_line,
):
    """REGRESSION (HIGH): a constant line objectId 400s on the 2nd
    create_line against the same deck. Two calls must request DISTINCT
    createLine objectIds."""
    create_line(MagicMock(), "DECK-SAME", "S1")
    create_line(MagicMock(), "DECK-SAME", "S1", end_x_inches=6)
    line_ids = _created_object_ids(stub_slides_for_line, "createLine")
    assert len(line_ids) == 2
    assert line_ids[0] != line_ids[1], (
        f"both create_line calls requested the SAME objectId "
        f"({line_ids[0]!r}) — the 2nd would 400 'object ID already in use'."
    )
    assert all(lid.startswith("appscriptly_line_") for lid in line_ids)


# ---------------------------------------------------------------------
# _classify_page_element — pure helper (element-kind tagged union)
# ---------------------------------------------------------------------


def test_classify_page_element_recognizes_each_kind():
    """Each discriminating key maps to its snake_case label."""
    assert _classify_page_element({"shape": {}}) == "shape"
    assert _classify_page_element({"table": {}}) == "table"
    assert _classify_page_element({"image": {}}) == "image"
    assert _classify_page_element({"line": {}}) == "line"
    assert _classify_page_element({"video": {}}) == "video"
    assert _classify_page_element({"wordArt": {}}) == "word_art"
    assert _classify_page_element({"sheetsChart": {}}) == "sheets_chart"
    assert _classify_page_element({"elementGroup": {}}) == "group"


def test_classify_page_element_unknown_kind_returns_unknown():
    """An element with none of the known discriminators (forward-compat:
    Slides may add element kinds) is reported as unknown, not dropped."""
    assert _classify_page_element({"objectId": "X"}) == "unknown"
    assert _classify_page_element({}) == "unknown"


# ---------------------------------------------------------------------
# _list_slide_elements — pure helper (per-element inventory)
# ---------------------------------------------------------------------


def test_list_slide_elements_empty_for_slide_without_pageElements():
    """A blank slide (no pageElements) yields an empty list, not None."""
    assert _list_slide_elements({}) == []
    assert _list_slide_elements({"pageElements": []}) == []
    assert _list_slide_elements({"pageElements": None}) == []


def test_list_slide_elements_classifies_and_preserves_order():
    """Walks pageElements in document order, returning {object_id, type}
    classified per element kind."""
    slide = {
        "pageElements": [
            {"objectId": "SH1", "shape": {}},
            {"objectId": "IMG1", "image": {}},
            {"objectId": "TBL1", "table": {}},
            {"objectId": "LN1", "line": {}},
        ],
    }
    assert _list_slide_elements(slide) == [
        {"object_id": "SH1", "type": "shape"},
        {"object_id": "IMG1", "type": "image"},
        {"object_id": "TBL1", "type": "table"},
        {"object_id": "LN1", "type": "line"},
    ]


def test_list_slide_elements_defaults_missing_object_id_to_empty():
    """An element missing objectId (shouldn't happen, but the contract
    permits) gets object_id empty rather than KeyError."""
    assert _list_slide_elements({"pageElements": [{"shape": {}}]}) == [
        {"object_id": "", "type": "shape"},
    ]


# ---------------------------------------------------------------------
# _extract_slide_notes — pure helper (speaker-notes BODY placeholder)
# ---------------------------------------------------------------------


def test_extract_slide_notes_empty_when_no_notesPage():
    """No slideProperties / notesPage gives an empty string (not None)."""
    assert _extract_slide_notes({}) == ""
    assert _extract_slide_notes({"slideProperties": {}}) == ""
    assert _extract_slide_notes({"slideProperties": {"notesPage": {}}}) == ""


def test_extract_slide_notes_reads_body_placeholder_text():
    """Notes text comes from the notesPage's BODY placeholder shape."""
    slide = {
        "slideProperties": {
            "notesPage": {
                "pageElements": [
                    {
                        "shape": {
                            "placeholder": {"type": "BODY"},
                            "text": {"textElements": [
                                {"textRun": {"content": "Talk slowly here.\n"}},
                            ]},
                        }
                    },
                ],
            }
        }
    }
    assert _extract_slide_notes(slide) == "Talk slowly here."


def test_extract_slide_notes_skips_non_body_placeholders():
    """The slide-number placeholder on the notesPage must NOT leak into
    the returned notes text — only the BODY placeholder counts."""
    slide = {
        "slideProperties": {
            "notesPage": {
                "pageElements": [
                    {
                        "shape": {
                            "placeholder": {"type": "SLIDE_NUMBER"},
                            "text": {"textElements": [
                                {"textRun": {"content": "12"}},
                            ]},
                        }
                    },
                    {
                        "shape": {
                            "placeholder": {"type": "BODY"},
                            "text": {"textElements": [
                                {"textRun": {"content": "Real notes"}},
                            ]},
                        }
                    },
                ],
            }
        }
    }
    assert _extract_slide_notes(slide) == "Real notes"


def test_get_outline_surfaces_notes_and_elements_end_to_end():
    """get_outline threads the per-slide elements inventory + notes text
    through from a presentations.get response."""
    slides = MagicMock(name="slides-v1-stub-enriched")
    slides.presentations().get().execute.return_value = {
        "presentationId": "DECK-E",
        "title": "Enriched",
        "slides": [
            {
                "objectId": "S1",
                "slideProperties": {
                    "layoutObjectId": "L1",
                    "notesPage": {
                        "pageElements": [
                            {
                                "shape": {
                                    "placeholder": {"type": "BODY"},
                                    "text": {"textElements": [
                                        {"textRun": {"content": "Note 1"}},
                                    ]},
                                }
                            },
                        ],
                    },
                },
                "pageElements": [
                    {"objectId": "SH1", "shape": {"text": {"textElements": [
                        {"textRun": {"content": "Body"}},
                    ]}}},
                    {"objectId": "IMG1", "image": {}},
                ],
            },
        ],
    }
    with with_google_api_client(
        InMemoryGoogleAPIClient({("slides", "v1"): slides})
    ):
        result = get_outline(MagicMock(), "DECK-E")
    slide = result["slides"][0]
    assert slide["index"] == 0
    assert slide["notes"] == "Note 1"
    assert slide["elements"] == [
        {"object_id": "SH1", "type": "shape"},
        {"object_id": "IMG1", "type": "image"},
    ]


# ---------------------------------------------------------------------
# set_speaker_notes — resolve notes shape + deleteText/insertText batch
# ---------------------------------------------------------------------


@pytest.fixture
def stub_slides_for_notes():
    """A Slides v1 stub whose presentations().get() resolves a slide's
    speakerNotesObjectId and whose batchUpdate() echoes a reply."""
    slides = MagicMock(name="slides-v1-stub-notes")
    slides.presentations().get().execute.return_value = {
        "presentationId": "DECK-N",
        "slides": [
            {
                "objectId": "SLIDE_N1",
                "slideProperties": {
                    "notesPage": {
                        "notesProperties": {
                            "speakerNotesObjectId": "NOTES_SHAPE_1",
                        },
                    },
                },
            },
        ],
    }
    slides.presentations().batchUpdate().execute.return_value = {"replies": []}
    with with_google_api_client(
        InMemoryGoogleAPIClient({("slides", "v1"): slides})
    ):
        yield slides


def _last_batch_update_requests(slides: MagicMock) -> list:
    """The requests list from the most recent batchUpdate(...) call that
    actually carried a body."""
    for call in reversed(slides.presentations().batchUpdate.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs["body"]["requests"]
    raise AssertionError("no batchUpdate() call captured a body")


def test_set_speaker_notes_resolves_notes_shape_and_replaces_text(
    stub_slides_for_notes,
):
    """A non-empty notes_text emits deleteText(ALL) THEN insertText on
    the resolved speakerNotesObjectId."""
    result = set_speaker_notes(
        MagicMock(), "DECK-N", "SLIDE_N1", "New presenter notes"
    )
    requests = _last_batch_update_requests(stub_slides_for_notes)
    assert requests == [
        {"deleteText": {
            "objectId": "NOTES_SHAPE_1",
            "textRange": {"type": "ALL"},
        }},
        {"insertText": {
            "objectId": "NOTES_SHAPE_1",
            "insertionIndex": 0,
            "text": "New presenter notes",
        }},
    ]
    assert result == {
        "presentation_id": "DECK-N",
        "slide_object_id": "SLIDE_N1",
        "speaker_notes_object_id": "NOTES_SHAPE_1",
        "notes_text": "New presenter notes",
    }


def test_set_speaker_notes_empty_text_clears_notes_with_delete_only(
    stub_slides_for_notes,
):
    """Empty notes_text CLEARS the notes: only deleteText is emitted
    (insertText rejects an empty string)."""
    set_speaker_notes(MagicMock(), "DECK-N", "SLIDE_N1", "")
    requests = _last_batch_update_requests(stub_slides_for_notes)
    assert requests == [
        {"deleteText": {
            "objectId": "NOTES_SHAPE_1",
            "textRange": {"type": "ALL"},
        }},
    ]


def test_set_speaker_notes_raises_when_slide_not_found(stub_slides_for_notes):
    """A slide objectId not present in the deck is a caller bug -> ValueError."""
    with pytest.raises(ValueError, match="not found in presentation"):
        set_speaker_notes(MagicMock(), "DECK-N", "MISSING_SLIDE", "x")


def test_set_speaker_notes_raises_when_no_notes_shape():
    """A slide whose notesPage carries no speakerNotesObjectId raises a
    clear ValueError rather than building a malformed request."""
    slides = MagicMock(name="slides-v1-stub-no-notes-shape")
    slides.presentations().get().execute.return_value = {
        "presentationId": "DECK-NN",
        "slides": [
            {"objectId": "S1", "slideProperties": {"notesPage": {}}},
        ],
    }
    with with_google_api_client(
        InMemoryGoogleAPIClient({("slides", "v1"): slides})
    ):
        with pytest.raises(ValueError, match="no resolvable speaker-notes"):
            set_speaker_notes(MagicMock(), "DECK-NN", "S1", "x")


# ---------------------------------------------------------------------
# Wave 4 (S1) element-management verbs: delete / duplicate / transform
# ---------------------------------------------------------------------


def _http_error(status: int) -> HttpError:
    """Build a googleapiclient HttpError with the given status, matching
    the construction used across the apps_script error-path tests."""
    resp = MagicMock()
    resp.status = status
    return HttpError(
        resp=resp,
        content=b'{"error": {"message": "Invalid requests[0].deleteObject: '
        b'The object (BOGUS) could not be found."}}',
    )


# --- delete_object ---------------------------------------------------


@pytest.fixture
def stub_slides_for_delete():
    slides = MagicMock(name="slides-v1-stub-delete")
    # deleteObject has no reply payload - Slides returns an empty reply.
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_delete_object_rejects_empty_object_id():
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        delete_object(MagicMock(), "DECK1", "")


def test_delete_object_rejects_whitespace_object_id():
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        delete_object(MagicMock(), "DECK1", "   ")


def test_delete_object_passes_presentationId(stub_slides_for_delete):
    delete_object(MagicMock(), "DECK-XYZ", "OBJ_7")
    kw = _last_batchUpdate_kwargs(stub_slides_for_delete)
    assert kw["presentationId"] == "DECK-XYZ"


def test_delete_object_builds_deleteObject_request(stub_slides_for_delete):
    """The batch carries exactly ONE deleteObject request targeting the
    given objectId - a bug that deleted the wrong id (or built a
    different request type) fails here."""
    delete_object(MagicMock(), "DECK1", "SHAPE_42")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_delete)["body"]["requests"]
    assert reqs == [{"deleteObject": {"objectId": "SHAPE_42"}}]


def test_delete_object_returns_flat_envelope(stub_slides_for_delete):
    """Result NAMES the objectId it acted on (deleted_object_id)."""
    result = delete_object(MagicMock(), "DECK-1", "IMG_9")
    assert result == {
        "presentation_id": "DECK-1",
        "deleted_object_id": "IMG_9",
    }


def test_delete_object_propagates_http_error_for_bogus_object_id(
    stub_slides_for_delete,
):
    """A bogus objectId surfaces Slides' 400 - the error must PROPAGATE
    (not be swallowed / mapped to a success envelope)."""
    stub_slides_for_delete.presentations().batchUpdate().execute.side_effect = (
        _http_error(400)
    )
    with pytest.raises(HttpError) as exc:
        delete_object(MagicMock(), "DECK1", "BOGUS")
    assert exc.value.resp.status == 400


# --- duplicate_object ------------------------------------------------


@pytest.fixture
def stub_slides_for_duplicate():
    slides = MagicMock(name="slides-v1-stub-duplicate")
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{"duplicateObject": {"objectId": "COPY_1"}}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_duplicate_object_rejects_empty_object_id():
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        duplicate_object(MagicMock(), "DECK1", "")


def test_duplicate_object_passes_presentationId(stub_slides_for_duplicate):
    duplicate_object(MagicMock(), "DECK-XYZ", "OBJ_7")
    kw = _last_batchUpdate_kwargs(stub_slides_for_duplicate)
    assert kw["presentationId"] == "DECK-XYZ"


def test_duplicate_object_builds_duplicateObject_request(
    stub_slides_for_duplicate,
):
    duplicate_object(MagicMock(), "DECK1", "TABLE_3")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_duplicate)["body"]["requests"]
    assert reqs == [{"duplicateObject": {"objectId": "TABLE_3"}}]


def test_duplicate_object_returns_id_map_and_new_id(stub_slides_for_duplicate):
    """duplicate returns the {source: new} id map AND names the new copy's
    objectId - the reply's new id is surfaced, not the source echoed back."""
    result = duplicate_object(MagicMock(), "DECK-1", "SRC_5")
    assert result == {
        "presentation_id": "DECK-1",
        "source_object_id": "SRC_5",
        "new_object_id": "COPY_1",
        "id_map": {"SRC_5": "COPY_1"},
    }


def test_duplicate_object_empty_reply_yields_empty_id_map(
    stub_slides_for_duplicate,
):
    """If Slides returns no duplicateObject reply, new_object_id is the
    empty string and id_map is empty (honest about the missing id rather
    than fabricating one) - still schema-valid."""
    stub_slides_for_duplicate.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    result = duplicate_object(MagicMock(), "DECK-1", "SRC_5")
    assert result["new_object_id"] == ""
    assert result["id_map"] == {}
    assert result["source_object_id"] == "SRC_5"


def test_duplicate_object_propagates_http_error_for_bogus_object_id(
    stub_slides_for_duplicate,
):
    stub_slides_for_duplicate.presentations().batchUpdate().execute.side_effect = (
        _http_error(400)
    )
    with pytest.raises(HttpError) as exc:
        duplicate_object(MagicMock(), "DECK1", "BOGUS")
    assert exc.value.resp.status == 400


# --- update_element_transform ----------------------------------------


@pytest.fixture
def stub_slides_for_transform():
    slides = MagicMock(name="slides-v1-stub-transform")
    # updatePageElementTransform has no reply payload.
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_update_element_transform_rejects_empty_object_id():
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        update_element_transform(MagicMock(), "DECK1", "")


def test_update_element_transform_rejects_unsupported_apply_mode():
    with pytest.raises(ValueError, match="apply_mode must be one of"):
        update_element_transform(
            MagicMock(), "DECK1", "OBJ1", apply_mode="SIDEWAYS",
        )


def test_update_element_transform_rejects_zero_scale():
    with pytest.raises(ValueError, match="must be non-zero"):
        update_element_transform(MagicMock(), "DECK1", "OBJ1", scale_x=0)
    with pytest.raises(ValueError, match="must be non-zero"):
        update_element_transform(MagicMock(), "DECK1", "OBJ1", scale_y=0)


def test_update_element_transform_passes_presentationId(
    stub_slides_for_transform,
):
    update_element_transform(MagicMock(), "DECK-XYZ", "OBJ_7")
    kw = _last_batchUpdate_kwargs(stub_slides_for_transform)
    assert kw["presentationId"] == "DECK-XYZ"


def test_update_element_transform_builds_request_with_exact_matrix_and_mode(
    stub_slides_for_transform,
):
    """CORE discriminating check: the request carries the EXACT applyMode
    and the EXACT affine matrix given, with translate in EMU."""
    update_element_transform(
        MagicMock(), "DECK1", "SHAPE_9",
        scale_x=2.0, scale_y=3.0,
        translate_x_emu=914400, translate_y_emu=457200,
        apply_mode="ABSOLUTE",
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_transform)["body"]["requests"]
    assert len(reqs) == 1
    upt = reqs[0]["updatePageElementTransform"]
    assert upt["objectId"] == "SHAPE_9"
    assert upt["applyMode"] == "ABSOLUTE"
    assert upt["transform"] == {
        "scaleX": 2.0,
        "scaleY": 3.0,
        "translateX": 914400,
        "translateY": 457200,
        "unit": "EMU",
    }


def test_update_element_transform_defaults_to_relative_identity_noop(
    stub_slides_for_transform,
):
    """Safe default: apply_mode defaults to RELATIVE and the default
    matrix is the identity (scale 1, translate 0), so a bare call is a
    no-op that cannot collapse or teleport the element."""
    update_element_transform(MagicMock(), "DECK1", "OBJ1")
    upt = _last_batchUpdate_kwargs(
        stub_slides_for_transform
    )["body"]["requests"][0]["updatePageElementTransform"]
    assert upt["applyMode"] == "RELATIVE"
    assert upt["transform"] == {
        "scaleX": 1.0,
        "scaleY": 1.0,
        "translateX": 0.0,
        "translateY": 0.0,
        "unit": "EMU",
    }


def test_update_element_transform_allows_negative_scale(
    stub_slides_for_transform,
):
    """Negative scale mirrors/flips the element - a legitimate use, NOT
    rejected (only exact zero is)."""
    update_element_transform(
        MagicMock(), "DECK1", "OBJ1", scale_x=-1.0, apply_mode="ABSOLUTE",
    )
    upt = _last_batchUpdate_kwargs(
        stub_slides_for_transform
    )["body"]["requests"][0]["updatePageElementTransform"]
    assert upt["transform"]["scaleX"] == -1.0


def test_update_element_transform_returns_envelope_echoing_matrix(
    stub_slides_for_transform,
):
    """Result NAMES the objectId acted on + echoes the resolved mode and
    the exact matrix sent."""
    result = update_element_transform(
        MagicMock(), "DECK-1", "OBJ_2",
        scale_x=1.0, scale_y=1.0,
        translate_x_emu=100000, translate_y_emu=0,
        apply_mode="RELATIVE",
    )
    assert result == {
        "presentation_id": "DECK-1",
        "object_id": "OBJ_2",
        "apply_mode": "RELATIVE",
        "transform": {
            "scaleX": 1.0,
            "scaleY": 1.0,
            "translateX": 100000,
            "translateY": 0,
            "unit": "EMU",
        },
    }


def test_update_element_transform_propagates_http_error_for_bogus_object_id(
    stub_slides_for_transform,
):
    stub_slides_for_transform.presentations().batchUpdate().execute.side_effect = (
        _http_error(400)
    )
    with pytest.raises(HttpError) as exc:
        update_element_transform(MagicMock(), "DECK1", "BOGUS")
    assert exc.value.resp.status == 400


# --- insert_text (Wave 5 S2) -----------------------------------------


@pytest.fixture
def stub_slides_for_insert_text():
    slides = MagicMock(name="slides-v1-stub-insert-text")
    # insertText has no reply payload - Slides returns an empty reply.
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK-1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("slides", "v1"): slides})):
        yield slides


def test_insert_text_rejects_empty_object_id():
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        insert_text(MagicMock(), "DECK1", "", "hello")


def test_insert_text_rejects_whitespace_object_id():
    with pytest.raises(ValueError, match="object_id cannot be empty"):
        insert_text(MagicMock(), "DECK1", "   ", "hello")


def test_insert_text_rejects_empty_text():
    """Empty text is a no-op that Slides 400s - reject client-side."""
    with pytest.raises(ValueError, match="text cannot be empty"):
        insert_text(MagicMock(), "DECK1", "SHAPE_1", "")


def test_insert_text_rejects_negative_insertion_index():
    with pytest.raises(ValueError, match="insertion_index cannot be negative"):
        insert_text(MagicMock(), "DECK1", "SHAPE_1", "hi", insertion_index=-1)


def test_insert_text_passes_presentationId(stub_slides_for_insert_text):
    insert_text(MagicMock(), "DECK-XYZ", "SHAPE_1", "hi")
    kw = _last_batchUpdate_kwargs(stub_slides_for_insert_text)
    assert kw["presentationId"] == "DECK-XYZ"


def test_insert_text_builds_insertText_request(stub_slides_for_insert_text):
    """The batch carries exactly ONE insertText request targeting the
    given objectId, text, and insertionIndex - a bug that sent the wrong
    objectId, dropped the index, or built a different request type fails
    here (this is the discriminating payload test)."""
    insert_text(
        MagicMock(), "DECK1", "SHAPE_42", "Quarterly results", insertion_index=5,
    )
    reqs = _last_batchUpdate_kwargs(stub_slides_for_insert_text)["body"]["requests"]
    assert reqs == [
        {
            "insertText": {
                "objectId": "SHAPE_42",
                "text": "Quarterly results",
                "insertionIndex": 5,
            },
        },
    ]


def test_insert_text_default_insertion_index_is_zero(stub_slides_for_insert_text):
    """Omitting insertion_index inserts at the start (index 0)."""
    insert_text(MagicMock(), "DECK1", "SHAPE_1", "hi")
    reqs = _last_batchUpdate_kwargs(stub_slides_for_insert_text)["body"]["requests"]
    assert reqs[0]["insertText"]["insertionIndex"] == 0


def test_insert_text_returns_flat_envelope(stub_slides_for_insert_text):
    """Result NAMES the object it acted on + echoes the index and the
    character count (insertText has no reply, so no new objectId)."""
    result = insert_text(MagicMock(), "DECK-1", "SHAPE_9", "abcde", insertion_index=2)
    assert result == {
        "presentation_id": "DECK-1",
        "object_id": "SHAPE_9",
        "insertion_index": 2,
        "text_length": 5,
    }


def test_insert_text_propagates_http_error_for_bogus_object_id(
    stub_slides_for_insert_text,
):
    """A bogus (or non-text) objectId surfaces Slides' 400 - the error
    must PROPAGATE, not be swallowed or mapped to a success envelope."""
    stub_slides_for_insert_text.presentations().batchUpdate().execute.side_effect = (
        _http_error(400)
    )
    with pytest.raises(HttpError) as exc:
        insert_text(MagicMock(), "DECK1", "BOGUS", "hi")
    assert exc.value.resp.status == 400
