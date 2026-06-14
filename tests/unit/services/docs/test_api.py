"""Co-located tests for services/docs/api.py pure helpers.

**v2.2.1 (R14 #8 split)**: api.py was split into 3 modules. Tests
for the pure helpers moved alongside the source:

  test_tab_tree.py        — _flatten_tab_tree, _find_tab_by_id,
                            _get_tab_depth, _find_tab_by_title
  test_markdown_render.py — _tab_properties, _rename_tab_request,
                            _add_tab_request, _plain_text_requests,
                            render_content_to_requests
  test_api.py (THIS file) — _summarize_body_paragraphs (the one
                            pure helper that stayed in api.py)

The api.py module now contains REST calls only. Tests for those
public API entry points (``make_doc_with_tabs`` /
``add_tabs_to_doc`` / etc.) go through the M2 GoogleAPIClient port —
``with_google_api_client(InMemoryGoogleAPIClient({...}))``. Those
consumer-path tests are out of scope here.

This file also pins the **re-export back-compat invariant**: callers
that did ``from appscriptly.services.docs.api import _flatten_tab_tree``
or similar continue to work, because api.py re-exports the pure
helpers from their new homes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.docs.api import (
    _summarize_body_paragraphs,
    create_comment,
    insert_image,
    list_comments,
    read_all_tabs,
    read_tab_content,
    reply_to_comment,
)


# ---------------------------------------------------------------------
# _summarize_body_paragraphs — extract style + text (lives in api.py)
# ---------------------------------------------------------------------


def test_summarize_body_paragraphs_extracts_namedStyleType_and_visible_text():
    content = [
        {
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "HEADING_1"},
                "elements": [{"textRun": {"content": "Title\n"}}],
            }
        },
        {
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "elements": [
                    {"textRun": {"content": "Hello "}},
                    {"textRun": {"content": "world\n"}},
                ],
            }
        },
    ]
    summary = _summarize_body_paragraphs(content)
    assert summary == [
        {"style": "HEADING_1", "text": "Title"},
        {"style": "NORMAL_TEXT", "text": "Hello world"},
    ]


def test_summarize_body_paragraphs_defaults_missing_style_to_NORMAL_TEXT():
    content = [
        {
            "paragraph": {
                # No paragraphStyle at all.
                "elements": [{"textRun": {"content": "naked\n"}}],
            }
        }
    ]
    summary = _summarize_body_paragraphs(content)
    assert summary == [{"style": "NORMAL_TEXT", "text": "naked"}]


def test_summarize_body_paragraphs_emits_TABLE_and_TOC_sentinels():
    """Tables and ToCs surface as style-only entries with empty text."""
    content = [
        {"table": {"rows": []}},
        {"tableOfContents": {}},
    ]
    summary = _summarize_body_paragraphs(content)
    assert summary == [
        {"style": "TABLE", "text": ""},
        {"style": "TOC", "text": ""},
    ]


# ---------------------------------------------------------------------
# Re-export back-compat: pre-v2.2.1 imports still work
# ---------------------------------------------------------------------


def test_api_module_reexports_pure_helpers_for_backward_compat():
    """Callers that import the pure helpers from api.py (rather than
    the new module homes) must continue to work — the R14 #8 split is
    internal and shouldn't break any pre-v2.2.1 import path.

    This test is intentionally narrow: it only asserts the names exist
    in ``services.docs.api`` and refer to the same callables as the
    new homes. Behaviour tests live next to each helper's source file."""
    from appscriptly.services.docs import api as api_mod
    from appscriptly.services.docs import markdown_render, tab_tree

    # tab_tree re-exports
    assert api_mod._flatten_tab_tree is tab_tree._flatten_tab_tree
    assert api_mod._find_tab_by_id is tab_tree._find_tab_by_id
    assert api_mod._get_tab_depth is tab_tree._get_tab_depth
    assert api_mod._find_tab_by_title is tab_tree._find_tab_by_title

    # markdown_render re-exports
    assert api_mod._tab_properties is markdown_render._tab_properties
    assert api_mod._rename_tab_request is markdown_render._rename_tab_request
    assert api_mod._add_tab_request is markdown_render._add_tab_request
    assert api_mod._plain_text_requests is markdown_render._plain_text_requests
    assert api_mod.render_content_to_requests is markdown_render.render_content_to_requests

    # Constants + types
    assert api_mod.CODE_FONT == markdown_render.CODE_FONT
    assert api_mod.CODE_BG_RGB == markdown_render.CODE_BG_RGB
    assert api_mod.TabSpec is markdown_render.TabSpec


# ---------------------------------------------------------------------
# read_tab_content — body-content element-type sentinels (R2 audit Gap #5)
#
# read_tab_content walks a tab's body and maps each Docs structural
# element type to a text sentinel:
#   textRun             -> its content
#   inlineObjectElement -> "[image]"            (+ image_count)
#   person              -> "[person:<email>]"
#   richLink            -> "[link]"
#   table               -> "[table RxC]"         (+ table_count)
#   tableOfContents     -> "[table of contents]"
#
# The pure helper _summarize_body_paragraphs (tested above) only covers
# textRun + table + TOC — it does NOT have the person / richLink /
# inline-image branches at all. Those branches live ONLY in the inline
# walker inside read_tab_content, and read_tab_content had no direct
# test. A wrong sentinel or a dropped branch silently corrupts what the
# model "reads" from a doc (the parsing-side analogue of the
# markdown_render rendering-side risk). Also uncovered: the
# tab-not-found ValueError and the tab_title (vs tab_id) resolution.
#
# read_tab_content makes two API calls: docs.documents().get(...) for
# the body, and (via is_file_trashed) drive.files().get(...). Both are
# stubbed through the GoogleAPIClient port — same dual-stub pattern as
# test_tools.py's gdocs_read_doc tests.
# ---------------------------------------------------------------------


def _para(*elements: dict, style: str = "NORMAL_TEXT") -> dict:
    """Wrap paragraph elements in the Docs structural-element shape."""
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": list(elements),
        }
    }


def _docs_drive_stubs(tabs: list[dict], *, trashed: bool = False):
    """Build (docs, drive) stubs for a read_tab_content call.

    docs.documents().get().execute() -> a doc carrying ``tabs``.
    drive.files().get().execute()    -> {"trashed": trashed} for the
    is_file_trashed lookup read_tab_content performs at the end.
    """
    docs = MagicMock(name="docs-v1")
    docs.documents().get().execute.return_value = {"documentId": "DOC1", "tabs": tabs}
    drive = MagicMock(name="drive-v3")
    drive.files().get().execute.return_value = {"trashed": trashed}
    client = InMemoryGoogleAPIClient({("docs", "v1"): docs, ("drive", "v3"): drive})
    return docs, drive, client


def _read_tab(tabs, *, tab_id=None, tab_title=None, trashed=False):
    _docs, _drive, client = _docs_drive_stubs(tabs, trashed=trashed)
    with with_google_api_client(client):
        return read_tab_content(
            MagicMock(), "DOC1", tab_id=tab_id, tab_title=tab_title
        )


def test_read_tab_content_emits_image_sentinel_and_counts_images():
    """An inlineObjectElement renders as '[image]' inside the paragraph
    text AND increments image_count. Two images in one paragraph ->
    image_count == 2 and two '[image]' markers."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "Pics"},
        "documentTab": {
            "body": {
                "content": [
                    _para(
                        {"textRun": {"content": "before "}},
                        {"inlineObjectElement": {"inlineObjectId": "io1"}},
                        {"textRun": {"content": " mid "}},
                        {"inlineObjectElement": {"inlineObjectId": "io2"}},
                    )
                ]
            }
        },
    }
    result = _read_tab([tab], tab_id="T0")
    assert result["image_count"] == 2
    [para] = result["paragraphs"]
    assert para["text"] == "before [image] mid [image]"


def test_read_tab_content_emits_person_sentinel_with_email():
    """A person element renders as '[person:<email>]' using the
    personProperties.email. The email must be interpolated exactly."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "People"},
        "documentTab": {
            "body": {
                "content": [
                    _para(
                        {"textRun": {"content": "ping "}},
                        {"person": {"personProperties": {"email": "amy@example.com"}}},
                    )
                ]
            }
        },
    }
    result = _read_tab([tab], tab_id="T0")
    [para] = result["paragraphs"]
    assert para["text"] == "ping [person:amy@example.com]"


def test_read_tab_content_person_without_email_uses_question_mark():
    """A person element missing personProperties.email falls back to
    '[person:?]' rather than raising a KeyError."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "People"},
        "documentTab": {
            "body": {"content": [_para({"person": {"personProperties": {}}})]}
        },
    }
    result = _read_tab([tab], tab_id="T0")
    assert result["paragraphs"][0]["text"] == "[person:?]"


def test_read_tab_content_emits_richlink_sentinel():
    """A richLink element renders as the '[link]' sentinel."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "Links"},
        "documentTab": {
            "body": {
                "content": [
                    _para(
                        {"textRun": {"content": "see "}},
                        {"richLink": {"richLinkProperties": {"uri": "https://x"}}},
                    )
                ]
            }
        },
    }
    result = _read_tab([tab], tab_id="T0")
    assert result["paragraphs"][0]["text"] == "see [link]"


def test_read_tab_content_emits_table_sentinel_with_dimensions():
    """A table renders as a style='TABLE' entry whose text is
    '[table RxC]' with the real row/column counts, and increments
    table_count. table_count entries are excluded from paragraph_count."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "Grid"},
        "documentTab": {
            "body": {
                "content": [
                    _para({"textRun": {"content": "intro\n"}}),
                    {"table": {"rows": 3, "columns": 4}},
                ]
            }
        },
    }
    result = _read_tab([tab], tab_id="T0")
    assert result["table_count"] == 1
    table_entries = [p for p in result["paragraphs"] if p["style"] == "TABLE"]
    assert table_entries == [{"style": "TABLE", "text": "[table 3x4]"}]
    # paragraph_count counts only real paragraphs (not TABLE/TOC).
    assert result["paragraph_count"] == 1


def test_read_tab_content_emits_toc_sentinel():
    """A tableOfContents renders as style='TOC', text='[table of contents]'."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "Outline"},
        "documentTab": {"body": {"content": [{"tableOfContents": {}}]}},
    }
    result = _read_tab([tab], tab_id="T0")
    toc = [p for p in result["paragraphs"] if p["style"] == "TOC"]
    assert toc == [{"style": "TOC", "text": "[table of contents]"}]


def test_read_tab_content_raises_valueerror_when_tab_not_found():
    """A tab_id that matches no tab must raise ValueError naming the
    missing id — not return an empty/None result the caller would
    mishandle."""
    tab = {
        "tabProperties": {"tabId": "T0", "title": "Only"},
        "documentTab": {"body": {"content": []}},
    }
    with pytest.raises(ValueError, match="Tab not found"):
        _read_tab([tab], tab_id="DOES-NOT-EXIST")


def test_read_tab_content_resolves_by_tab_title():
    """When tab_title (not tab_id) is given, the correct tab is located
    by exact title match — the tab_title resolution branch. Two tabs
    present; selecting by title must return the matching one's content."""
    tabs = [
        {
            "tabProperties": {"tabId": "T0", "title": "First"},
            "documentTab": {
                "body": {"content": [_para({"textRun": {"content": "first body\n"}})]}
            },
        },
        {
            "tabProperties": {"tabId": "T1", "title": "Second"},
            "documentTab": {
                "body": {"content": [_para({"textRun": {"content": "second body\n"}})]}
            },
        },
    ]
    result = _read_tab(tabs, tab_title="Second")
    assert result["tab_id"] == "T1"
    assert result["title"] == "Second"
    assert result["paragraphs"][0]["text"] == "second body"


def test_read_tab_content_requires_an_identifier():
    """Neither tab_id nor tab_title -> ValueError (the up-front guard)."""
    with pytest.raises(ValueError, match="Provide either tab_id or tab_title"):
        _read_tab([], tab_id=None, tab_title=None)


# ---------------------------------------------------------------------
# edit_range — deleteContentRange [+ insertText], UTF-16 index contract
#
# edit_range is the location-indexed "delete/replace a span" primitive.
# It takes RAW UTF-16 code-unit indices (same address space as
# format_range and as Docs' own startIndex/endIndex) and emits a
# deleteContentRange over [start, end) followed by an optional insertText
# at start. The hard correctness property — and the reason this tool
# needs a dedicated above-BMP regression test — is that Google Docs
# measures positions in UTF-16 code units, NOT Python code points, so a
# range must be expressed in UTF-16 units (an emoji is 1 code point but
# 2 units; PR #184 / R6 fixed the renderer's _insert for the same
# reason). These tests stub the Docs Resource through the GoogleAPIClient
# port and assert on the batchUpdate body the function builds.
# ---------------------------------------------------------------------


def _edit_range_docs_stub():
    """A Docs v1 stub whose batchUpdate captures its request body."""
    docs = MagicMock(name="docs-v1")
    docs.documents().batchUpdate().execute.return_value = {"replies": []}
    client = InMemoryGoogleAPIClient({("docs", "v1"): docs})
    return docs, client


def _last_edit_batch_requests(docs) -> list[dict]:
    """The ``requests`` list of the most recent batchUpdate(body=...) call."""
    for call in reversed(docs.documents().batchUpdate.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs["body"]["requests"]
    raise AssertionError("no documents().batchUpdate(body=...) call captured")


def _utf16_doc_slice(body: str, start: int, end: int) -> str:
    """Slice ``body`` by 1-based Docs indices treating it as UTF-16.

    Mirrors the helper in test_markdown_render.py: Docs ranges are
    ``[start, end)`` in UTF-16 code units, 1-based (body content begins
    at index 1). Encode UTF-16-LE (2 bytes/unit) and slice on unit
    boundaries — the inverse of the index math, so if a deletion range
    is positioned correctly in UTF-16 space this returns the exact run
    the range is supposed to address.
    """
    enc = body.encode("utf-16-le")
    return enc[(start - 1) * 2:(end - 1) * 2].decode("utf-16-le")


def _call_edit_range(**kwargs):
    from appscriptly.services.docs.api import edit_range
    docs, client = _edit_range_docs_stub()
    with with_google_api_client(client):
        result = edit_range(MagicMock(), "DOC1", **kwargs)
    return result, docs


def test_edit_range_pure_delete_builds_single_deleteContentRange():
    """No text → exactly one deleteContentRange over [start, end); the
    return envelope reports deleted=True, inserted=False."""
    result, docs = _call_edit_range(start_index=5, end_index=12)
    assert result["deleted"] is True
    assert result["inserted"] is False
    assert result["inserted_units"] == 0
    assert _last_edit_batch_requests(docs) == [
        {"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 12}}}
    ]


def test_edit_range_replace_orders_delete_before_insert():
    """With text → deleteContentRange FIRST, then insertText at
    start_index. Order matters: the delete collapses [start, end) to a
    gap at start_index, and the insert at start_index fills it."""
    result, docs = _call_edit_range(start_index=4, end_index=9, text="new")
    reqs = _last_edit_batch_requests(docs)
    assert list(reqs[0]) == ["deleteContentRange"]
    assert list(reqs[1]) == ["insertText"]
    assert reqs[1]["insertText"]["location"]["index"] == 4
    assert reqs[1]["insertText"]["text"] == "new"
    assert result["inserted_units"] == 3


def test_edit_range_deletion_targets_correct_utf16_span_after_emoji():
    """R6/UTF-16 REGRESSION — the load-bearing correctness test.

    Reference body ``"a😀bcd"``: the emoji (U+1F600) is a surrogate pair
    = 1 Python code point but 2 UTF-16 code units. Suppose the caller
    wants to delete ``"bc"``. In Docs' UTF-16, 1-based addressing:

        index 1 → "a"          (1 unit)
        index 2,3 → "😀"        (2 units, surrogate pair)
        index 4 → "b"
        index 5 → "c"
        index 6 → "d"

    So ``"bc"`` is the half-open range [4, 6). The deleteContentRange the
    function emits MUST carry exactly those UTF-16 indices — and slicing
    the reference body by them (as UTF-16) must return ``"bc"``.

    The CALLER supplies UTF-16 indices and the tool passes them through
    verbatim. This test pins that pass-through is faithful AND documents
    the unit basis: [4, 6) is the correct range for ``"bc"``, whereas the
    code-point start a buggy ``len()``-based caller would compute (3) is
    STRICTLY SMALLER — it would target the wrong run.
    """
    body = "a\U0001f600bcd"  # "a" + U+1F600 emoji + "bcd"
    # The half-open UTF-16 range that addresses "bc".
    start, end = 4, 6
    # Sanity: in UTF-16, [4, 6) really is "bc" in this body.
    assert _utf16_doc_slice(body, start, end) == "bc"
    # And the code-point start the buggy len()-based math would pick is
    # STRICTLY SMALLER (the surrogate pair shifts the real index up by 1).
    # Same R6 discriminator as test_markdown_render.py — we don't slice
    # the buggy range (it would split the surrogate pair and raise); the
    # index inequality IS the behavioural difference.
    cp_start = 1 + len("a\U0001f600")  # 1 + 2 == 3 (code points)
    assert cp_start == 3 and start == 4
    assert cp_start < start  # code-point math under-counts the surrogate pair

    result, docs = _call_edit_range(start_index=start, end_index=end, text="XY")
    rng = _last_edit_batch_requests(docs)[0]["deleteContentRange"]["range"]
    assert rng == {"startIndex": 4, "endIndex": 6}, (
        "deleteContentRange must carry the UTF-16 indices verbatim so the "
        f"emoji-preceded span is targeted correctly; got {rng}"
    )
    assert result["start_index"] == 4 and result["end_index"] == 6


def test_edit_range_inserted_units_counts_above_bmp_as_two():
    """``inserted_units`` is the UTF-16 length of the inserted text, so an
    above-BMP char counts as 2. ``"x𝐀"`` (x + U+1D400 MATHEMATICAL BOLD
    CAPITAL A) is 2 code points but 3 UTF-16 units."""
    result, _docs = _call_edit_range(
        start_index=1, end_index=2, text="x\U0001d400",
    )
    assert result["inserted_units"] == 3  # NOT len("x𝐀") == 2


def test_edit_range_threads_tab_id_onto_range_and_location():
    result, docs = _call_edit_range(
        start_index=2, end_index=5, text="z", tab_id="t.xyz",
    )
    reqs = _last_edit_batch_requests(docs)
    assert reqs[0]["deleteContentRange"]["range"]["tabId"] == "t.xyz"
    assert reqs[1]["insertText"]["location"]["tabId"] == "t.xyz"
    assert result["tab_id"] == "t.xyz"


def test_edit_range_rejects_invalid_range():
    from appscriptly.services.docs.api import edit_range
    with pytest.raises(ValueError, match="start_index must be >= 1"):
        edit_range(MagicMock(), "DOC1", 0, 5)
    with pytest.raises(ValueError, match="end_index must be greater"):
        edit_range(MagicMock(), "DOC1", 5, 5)


def test_edit_range_rejects_empty_tab_id():
    from appscriptly.services.docs.api import edit_range
    with pytest.raises(ValueError, match="tab_id cannot be the empty string"):
        edit_range(MagicMock(), "DOC1", 1, 3, tab_id="   ")


# ---------------------------------------------------------------------
# insert_image — insertInlineImage batchUpdate
# ---------------------------------------------------------------------


@pytest.fixture
def stub_docs_for_image():
    docs = MagicMock(name="docs-v1-image")
    docs.documents().batchUpdate().execute.return_value = {
        "replies": [{"insertInlineImage": {"objectId": "IMG_AB12"}}],
    }
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs})
    ):
        yield docs


def _last_image_batch_body(docs: MagicMock) -> dict:
    for call in reversed(docs.documents().batchUpdate.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs["body"]
    raise AssertionError("no batchUpdate() call captured a body")


def test_insert_image_builds_insert_inline_image_request(stub_docs_for_image):
    insert_image(MagicMock(), "DOC1", "https://example.com/pic.png", index=5)
    body = _last_image_batch_body(stub_docs_for_image)
    assert body["requests"] == [
        {"insertInlineImage": {
            "location": {"index": 5},
            "uri": "https://example.com/pic.png",
        }}
    ]


def test_insert_image_includes_object_size_when_both_dims_given(
    stub_docs_for_image,
):
    insert_image(
        MagicMock(), "DOC1", "https://example.com/p.png",
        width_pt=120, height_pt=80,
    )
    req = _last_image_batch_body(stub_docs_for_image)["requests"][0]
    assert req["insertInlineImage"]["objectSize"] == {
        "width": {"magnitude": 120, "unit": "PT"},
        "height": {"magnitude": 80, "unit": "PT"},
    }


def test_insert_image_scopes_to_tab_when_tab_id_given(stub_docs_for_image):
    insert_image(
        MagicMock(), "DOC1", "https://example.com/p.png", tab_id="t.0",
    )
    req = _last_image_batch_body(stub_docs_for_image)["requests"][0]
    assert req["insertInlineImage"]["location"]["tabId"] == "t.0"


def test_insert_image_returns_object_id_from_reply(stub_docs_for_image):
    result = insert_image(MagicMock(), "DOC1", "https://example.com/p.png")
    assert result == {
        "doc_id": "DOC1",
        "image_object_id": "IMG_AB12",
        "index": 1,
        "tab_id": None,
        "uri": "https://example.com/p.png",
    }


def test_insert_image_rejects_non_http_uri():
    with pytest.raises(ValueError, match="must be a public http"):
        insert_image(MagicMock(), "DOC1", "ftp://example.com/p.png")


def test_insert_image_rejects_blank_uri():
    with pytest.raises(ValueError, match="image_uri cannot be empty"):
        insert_image(MagicMock(), "DOC1", "   ")


def test_insert_image_rejects_one_dimension_only():
    with pytest.raises(ValueError, match="must be supplied together"):
        insert_image(
            MagicMock(), "DOC1", "https://example.com/p.png", width_pt=100,
        )


def test_insert_image_rejects_index_below_one():
    with pytest.raises(ValueError, match="index must be >= 1"):
        insert_image(MagicMock(), "DOC1", "https://example.com/p.png", index=0)


# ---------------------------------------------------------------------
# read_*: suggestions_view_mode threading
# ---------------------------------------------------------------------


def test_read_tab_content_passes_suggestions_view_mode(monkeypatch):
    docs, drive, client = _docs_drive_stubs(
        [{"tabProperties": {"tabId": "t.0", "title": "T"},
          "documentTab": {"body": {"content": []}}}]
    )
    with with_google_api_client(client):
        read_tab_content(
            MagicMock(), "DOC1", tab_id="t.0",
            suggestions_view_mode="PREVIEW_WITHOUT_SUGGESTIONS",
        )
    # The most recent documents().get with a documentId carries the mode.
    got = None
    for call in reversed(docs.documents().get.call_args_list):
        if "documentId" in call.kwargs:
            got = call.kwargs
            break
    assert got is not None
    assert got["suggestionsViewMode"] == "PREVIEW_WITHOUT_SUGGESTIONS"


def test_read_tab_content_rejects_bad_suggestions_view_mode():
    with pytest.raises(ValueError, match="suggestions_view_mode must be one of"):
        read_tab_content(
            MagicMock(), "DOC1", tab_id="t.0", suggestions_view_mode="BOGUS",
        )


def test_read_all_tabs_passes_suggestions_view_mode():
    docs = MagicMock(name="docs-v1-all")
    docs.documents().get().execute.return_value = {"documentId": "DOC1", "tabs": []}
    drive = MagicMock(name="drive-v3-all")
    drive.files().get().execute.return_value = {"trashed": False}
    client = InMemoryGoogleAPIClient(
        {("docs", "v1"): docs, ("drive", "v3"): drive}
    )
    with with_google_api_client(client):
        read_all_tabs(
            MagicMock(), "DOC1",
            suggestions_view_mode="SUGGESTIONS_INLINE",
        )
    got = None
    for call in reversed(docs.documents().get.call_args_list):
        if "documentId" in call.kwargs:
            got = call.kwargs
            break
    assert got["suggestionsViewMode"] == "SUGGESTIONS_INLINE"


# ---------------------------------------------------------------------
# Comments — Drive v3 comments()/replies() on app-created docs
# ---------------------------------------------------------------------


@pytest.fixture
def stub_drive_for_comments():
    drive = MagicMock(name="drive-v3-comments")
    drive.comments().list().execute.return_value = {
        "comments": [{"id": "c1", "content": "hi", "replies": []}],
        "nextPageToken": "TOK",
    }
    drive.comments().create().execute.return_value = {
        "id": "c2", "content": "new comment",
    }
    drive.replies().create().execute.return_value = {
        "id": "r1", "content": "a reply",
    }
    with with_google_api_client(
        InMemoryGoogleAPIClient({("drive", "v3"): drive})
    ):
        yield drive


def test_list_comments_passes_fileId_and_fields(stub_drive_for_comments):
    result = list_comments(MagicMock(), "DOC-XYZ")
    # fileId targets the doc; a fields mask is mandatory for Drive comments.
    last = None
    for call in reversed(stub_drive_for_comments.comments().list.call_args_list):
        if "fileId" in call.kwargs:
            last = call.kwargs
            break
    assert last["fileId"] == "DOC-XYZ"
    assert last["fields"]  # non-empty mask
    assert result["doc_id"] == "DOC-XYZ"
    assert result["comments"] == [{"id": "c1", "content": "hi", "replies": []}]
    assert result["next_page_token"] == "TOK"


def test_create_comment_passes_content_and_returns_resource(
    stub_drive_for_comments,
):
    result = create_comment(MagicMock(), "DOC-XYZ", "Please review")
    last = None
    for call in reversed(stub_drive_for_comments.comments().create.call_args_list):
        if "fileId" in call.kwargs:
            last = call.kwargs
            break
    assert last["fileId"] == "DOC-XYZ"
    assert last["body"] == {"content": "Please review"}
    assert last["fields"]
    assert result == {"doc_id": "DOC-XYZ", "comment": {"id": "c2", "content": "new comment"}}


def test_create_comment_rejects_blank_content():
    with pytest.raises(ValueError, match="content cannot be empty"):
        create_comment(MagicMock(), "DOC1", "   ")


def test_reply_to_comment_passes_comment_id_and_content(stub_drive_for_comments):
    result = reply_to_comment(MagicMock(), "DOC-XYZ", "c1", "Thanks")
    last = None
    for call in reversed(stub_drive_for_comments.replies().create.call_args_list):
        if "commentId" in call.kwargs:
            last = call.kwargs
            break
    assert last["fileId"] == "DOC-XYZ"
    assert last["commentId"] == "c1"
    assert last["body"] == {"content": "Thanks"}
    assert result == {
        "doc_id": "DOC-XYZ",
        "comment_id": "c1",
        "reply": {"id": "r1", "content": "a reply"},
    }


def test_reply_to_comment_rejects_blank_comment_id():
    with pytest.raises(ValueError, match="comment_id cannot be empty"):
        reply_to_comment(MagicMock(), "DOC1", "  ", "hi")


def test_reply_to_comment_rejects_blank_content():
    with pytest.raises(ValueError, match="content cannot be empty"):
        reply_to_comment(MagicMock(), "DOC1", "c1", "   ")
