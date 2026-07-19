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
    _apply_markdown_content,
    _content_has_table,
    _split_content_segments,
    _summarize_body_content,
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


def test_summarize_body_paragraphs_emits_TABLE_and_TOC_markers():
    """Tables surface as a ``[table RxC]`` marker, ToCs as
    ``[table of contents]``. (Before the S3 read-fidelity unification the
    bulk wrapper emitted empty strings for both — this pins the new,
    consistent-with-single-tab emission.)"""
    content = [
        {"table": {"rows": 1, "columns": 2}},  # no tableRows -> no cell text
        {"tableOfContents": {}},
    ]
    summary = _summarize_body_paragraphs(content)
    assert summary == [
        {"style": "TABLE", "text": "[table 1x2]"},
        {"style": "TOC", "text": "[table of contents]"},
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
# S3 read-fidelity: ONE element-summarizer feeds BOTH read paths, so
# their per-element markers are identical, tables carry extracted cell
# text, and read_all_tabs no longer silently drops images/persons/links
# (it pulled only textRun content before, emitting empty text for tables).
# ---------------------------------------------------------------------


def _cell(text: str) -> dict:
    """A table cell whose single paragraph holds ``text``."""
    return {"content": [_para({"textRun": {"content": text + "\n"}})]}


def _rich_tab() -> dict:
    """A tab exercising every element type: a styled heading, a paragraph
    mixing inline image + person + rich link, a 2x2 table with real cell
    text, and a table of contents."""
    return {
        "tabProperties": {"tabId": "T0", "title": "Rich"},
        "documentTab": {
            "body": {
                "content": [
                    _para({"textRun": {"content": "Heading\n"}}, style="HEADING_1"),
                    _para(
                        {"textRun": {"content": "see "}},
                        {"inlineObjectElement": {"inlineObjectId": "io1"}},
                        {"person": {"personProperties": {"email": "amy@x.com"}}},
                        {"richLink": {"richLinkProperties": {"uri": "https://x"}}},
                    ),
                    {"table": {"rows": 2, "columns": 2, "tableRows": [
                        {"tableCells": [_cell("Name"), _cell("Age")]},
                        {"tableCells": [_cell("Al"), _cell("30")]},
                    ]}},
                    {"tableOfContents": {}},
                ]
            }
        },
    }


def test_read_paths_emit_identical_per_element_output():
    """DISCRIMINATING (S3a): the same tab read via read_tab_content
    (single tab) and read_all_tabs (bulk) yields BYTE-IDENTICAL per-element
    paragraphs, the table carries extracted cell text, and the counts
    agree. On main this fails: the bulk path dropped image/person/link and
    emitted empty text for the table, and the single-tab table had no cell
    text."""
    tab = _rich_tab()
    _d, _dr, client = _docs_drive_stubs([tab])
    with with_google_api_client(client):
        single = read_tab_content(MagicMock(), "DOC1", tab_id="T0")
    _d2, _dr2, client2 = _docs_drive_stubs([tab])
    with with_google_api_client(client2):
        bulk = read_all_tabs(MagicMock(), "DOC1")

    # Per-element output is identical between the two paths.
    assert single["paragraphs"] == bulk["tabs"][0]["paragraphs"]
    # The table entry carries the RxC marker AND the extracted cell text.
    table_entry = next(p for p in single["paragraphs"] if p["style"] == "TABLE")
    assert table_entry["text"] == "[table 2x2] Name | Age || Al | 30"
    # Counts are consistent across the two (different) envelopes.
    assert single["table_count"] == bulk["tabs"][0]["table_count"] == 1
    assert single["image_count"] == bulk["tabs"][0]["image_count"] == 1
    assert single["paragraph_count"] == bulk["tabs"][0]["paragraph_count"] == 2


def test_read_all_tabs_now_emits_image_person_link_and_cell_text():
    """Revert-check (S3a): the bulk path used to pull only textRun content,
    silently dropping [image]/[person]/[link] and emitting empty text for
    tables/ToCs. It now emits them all via the shared summarizer."""
    tab = _rich_tab()
    _d, _dr, client = _docs_drive_stubs([tab])
    with with_google_api_client(client):
        bulk = read_all_tabs(MagicMock(), "DOC1")
    texts = [p["text"] for p in bulk["tabs"][0]["paragraphs"]]
    assert "see [image][person:amy@x.com][link]" in texts
    assert "[table 2x2] Name | Age || Al | 30" in texts
    assert "[table of contents]" in texts


def test_summarize_body_content_returns_counts_and_recurses_cells():
    """The shared summarizer returns (paragraphs, table_count, image_count)
    and recurses tableRows[].tableCells[].content for cell text."""
    content = [
        _para({"inlineObjectElement": {"inlineObjectId": "io1"}}),
        {"table": {"rows": 1, "columns": 2, "tableRows": [
            {"tableCells": [_cell("A"), _cell("B")]},
        ]}},
    ]
    paras, table_count, image_count = _summarize_body_content(content)
    assert (table_count, image_count) == (1, 1)
    table_entry = next(p for p in paras if p["style"] == "TABLE")
    assert table_entry["text"] == "[table 1x2] A | B"


def test_summarize_body_content_empty_table_stays_marker_only():
    """A table with no cell content keeps just its [table RxC] marker —
    no trailing separators."""
    paras, _tc, _ic = _summarize_body_content(
        [{"table": {"rows": 3, "columns": 2}}]
    )
    assert paras == [{"style": "TABLE", "text": "[table 3x2]"}]


# ---------------------------------------------------------------------
# Table-in-content two-phase (regression fix for the mid-content
# insertTable leading-newline shift that 400'd make_tabbed_doc LIVE:
# "insertText index must be inside the bounds of an existing paragraph").
# _apply_markdown_content splits content at tables and applies each
# segment at a RE-FETCHED index, so there is NO client-side table
# arithmetic - cell starts AND post-table content come from the server.
# ---------------------------------------------------------------------


def test_content_has_table_detects_gfm_table():
    assert _content_has_table("intro\n\n| A | B |\n|---|---|\n| x | y |")
    assert _content_has_table("| A |\n|---|\n| v |")
    assert not _content_has_table("no table here\njust prose")
    assert not _content_has_table("a | b without a separator row\nmore")
    # A thematic break is NOT a table separator (no pipe on the rule line).
    assert not _content_has_table("intro\n\n---\n\nmore")


def test_content_has_table_rejects_column_count_mismatch():
    """D3: a header/separator pair whose column counts DISAGREE is not a
    GFM table. _content_has_table now applies the same column-count clause
    as _split_content_segments, so a mismatched pair is NOT routed to the
    table path - both agree it renders as prose."""
    # Header has 2 columns, separator declares 3 -> not a table.
    mismatch = "| A | B |\n|---|---|---|\n| x | y |"
    assert not _content_has_table(mismatch)
    # _split_content_segments already agrees: no 'table' segment isolated.
    assert "table" not in [k for k, _ in _split_content_segments(mismatch)]
    # A matching column count is still detected AND split as a table.
    match = "| A | B |\n|---|---|\n| x | y |"
    assert _content_has_table(match)
    assert "table" in [k for k, _ in _split_content_segments(match)]


def test_split_content_segments_isolates_table_from_prose():
    content = "intro\n\n| A | B |\n|---|---|\n| x | y |\n\ntail"
    segments = _split_content_segments(content)
    assert [k for k, _ in segments] == ["text", "table", "text"]
    assert segments[0][1].strip() == "intro"
    assert segments[1][1] == "| A | B |\n|---|---|\n| x | y |"
    assert segments[2][1].strip() == "tail"


def test_split_content_segments_plain_text_is_one_segment():
    assert _split_content_segments("just prose\n\nmore prose") == [
        ("text", "just prose\n\nmore prose")
    ]


def test_table_like_text_inside_a_code_fence_is_not_a_table():
    """Pipes inside a fenced code block are code, not a GFM table - they
    must NOT be detected or split out (that would corrupt the fence)."""
    fenced = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    assert not _content_has_table(fenced)
    # The whole fence stays a single text segment (no 'table' segment).
    assert _split_content_segments(fenced) == [("text", fenced)]
    # A REAL table after a fenced example is still found + split.
    mixed = fenced + "\n\n| R | S |\n|---|---|\n| 3 | 4 |"
    assert _content_has_table(mixed)
    kinds = [k for k, _ in _split_content_segments(mixed)]
    assert kinds == ["text", "table"]


def _doc_body(elements: list[dict]) -> dict:
    """A documents.get() response: one tab whose body is ``elements``."""
    return {
        "tabs": [
            {
                "tabProperties": {"tabId": "T0"},
                "documentTab": {"body": {"content": elements}},
            }
        ]
    }


def test_apply_markdown_content_places_post_table_text_at_refetched_index():
    """THE FIX: content after a table is inserted at an index READ BACK
    from the live doc (post-fill), never a client-computed table span.

    A get() side_effect models the evolving doc: empty body -> body after
    'intro' -> body with the inserted table (for cell location) -> body
    after the filled table. On the reverted one-shot code the trailing
    'tail' would sit at a client-arithmetic index (the live 400)."""
    docs = MagicMock(name="docs-v1")
    table_rows = [
        {"tableCells": [{"content": [{"startIndex": 11}]},
                        {"content": [{"startIndex": 13}]}]},
        {"tableCells": [{"content": [{"startIndex": 16}]},
                        {"content": [{"startIndex": 18}]}]},
    ]
    docs.documents().get().execute.side_effect = [
        _doc_body([{"startIndex": 1, "endIndex": 2}]),          # seg1 -> insert at 1
        _doc_body([{"startIndex": 1, "endIndex": 8}]),          # seg2 -> table at 7
        _doc_body([{"startIndex": 1, "endIndex": 8},            # locate: table at 8
                   {"startIndex": 8, "endIndex": 20,
                    "table": {"tableRows": table_rows}}]),
        _doc_body([{"startIndex": 1, "endIndex": 8},            # seg3 -> tail at 29
                   {"startIndex": 8, "endIndex": 30, "table": {}}]),
    ]
    docs.documents().batchUpdate().execute.return_value = {"replies": []}

    content = "intro\n\n| A | B |\n|---|---|\n| x | y |\n\ntail"
    _apply_markdown_content(docs, "DOC1", "T0", content)

    batches = [
        c.kwargs["body"]["requests"]
        for c in docs.documents().batchUpdate.call_args_list
        if "body" in c.kwargs
    ]
    flat = [(op, req[op]) for reqs in batches for req in reqs for op in req]

    # insertTable at the RE-FETCHED body end (7), not a client index.
    insert_tables = [p for op, p in flat if op == "insertTable"]
    assert len(insert_tables) == 1
    assert insert_tables[0]["location"]["index"] == 7
    assert (insert_tables[0]["rows"], insert_tables[0]["columns"]) == (2, 2)

    # Cell fills use SERVER cell indices, reverse-ordered.
    cell_fills = [
        p["location"]["index"]
        for op, p in flat
        if op == "insertText" and p["text"] in ("A", "B", "x", "y")
    ]
    assert cell_fills == sorted(cell_fills, reverse=True)
    assert set(cell_fills) == {11, 13, 16, 18}

    # Post-table 'tail' lands at the RE-FETCHED post-table index (29) -
    # the exact position the one-shot client arithmetic got wrong live.
    tail = [p for op, p in flat
            if op == "insertText" and p["text"].startswith("tail")]
    assert len(tail) == 1
    assert tail[0]["location"]["index"] == 29


def test_apply_markdown_content_exact_live_failing_payload():
    """Regression fixture for the LIVE failure: one tab = intro paragraph,
    a 2x2 GFM table with an emoji cell, a line after the table, an image,
    and a final line. Live this 400'd atomically at request 9 ("insertion
    index must be inside the bounds of an existing paragraph"), so the
    whole batchUpdate was rejected and the tab was left with ZERO content.

    Here the get() side_effect models the evolving doc; the fix applies
    the intro, the table (server-indexed), and the WHOLE post-table run
    (line + image + final line) each at a re-fetched index - the emoji
    cell and the post-table content that broke the one-shot arithmetic."""
    docs = MagicMock(name="docs-v1")
    table_rows = [
        {"tableCells": [{"content": [{"startIndex": 21}]},
                        {"content": [{"startIndex": 23}]}]},
        {"tableCells": [{"content": [{"startIndex": 26}]},
                        {"content": [{"startIndex": 28}]}]},
    ]
    docs.documents().get().execute.side_effect = [
        _doc_body([{"startIndex": 1, "endIndex": 2}]),            # seg1 -> intro at 1
        _doc_body([{"startIndex": 1, "endIndex": 18}]),           # seg2 -> table at 17
        _doc_body([{"startIndex": 1, "endIndex": 18},             # locate table
                   {"startIndex": 18, "endIndex": 40,
                    "table": {"tableRows": table_rows}}]),
        _doc_body([{"startIndex": 1, "endIndex": 18},             # seg3 -> after-run at 59
                   {"startIndex": 18, "endIndex": 60, "table": {}}]),
    ]
    docs.documents().batchUpdate().execute.return_value = {"replies": []}

    payload = (
        "intro paragraph\n\n"
        "| H1 | H2 |\n|----|----|\n| a | Beta \U0001F389 |\n\n"
        "Line after the table.\n\n"
        "![diagram](https://example.com/pic.png)\n\n"
        "final line"
    )
    _apply_markdown_content(docs, "DOC1", "T0", payload)

    flat = [
        (op, req[op])
        for c in docs.documents().batchUpdate.call_args_list if "body" in c.kwargs
        for req in c.kwargs["body"]["requests"]
        for op in req
    ]

    # Exactly one table, created at the re-fetched body end (17).
    insert_tables = [p for op, p in flat if op == "insertTable"]
    assert len(insert_tables) == 1
    assert insert_tables[0]["location"]["index"] == 17

    # The emoji cell was filled (server index), proving the cell walk +
    # utf-16 content survive the round trip.
    emoji_fill = [p for op, p in flat
                  if op == "insertText" and p["text"] == "Beta \U0001F389"]
    assert len(emoji_fill) == 1
    assert emoji_fill[0]["location"]["index"] in {21, 23, 26, 28}

    # The post-table run - the request-9 failure - lands at the RE-FETCHED
    # index (59): the line, the image, and the final line all applied
    # AFTER the table, none arithmetic'd into a phantom paragraph.
    line_after = [p for op, p in flat
                  if op == "insertText" and p["text"].startswith("Line after")]
    assert len(line_after) == 1
    assert line_after[0]["location"]["index"] == 59
    assert any(op == "insertInlineImage" for op, _p in flat)
    assert any(op == "insertText" and p["text"].startswith("final line")
               for op, p in flat)


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


# ---------------------------------------------------------------------
# add_tabs_to_doc return shape - the gdocs_add_tabs false-error fix
# ---------------------------------------------------------------------
#
# GDOCS_ADD_TABS_OUTPUT_SCHEMA requires doc_id + url + tabs, but
# add_tabs_to_doc used to return only {"tabs": ...}. FastMCP output
# validation therefore failed EVERY gdocs_add_tabs call AFTER both
# mutating batchUpdates had landed, and a client retrying the "failed"
# call duplicated the tabs (observed live in the 2026-07-02 demo run).
# These tests pin the schema-required keys on BOTH return paths.


def _stub_docs_for_add_tabs() -> MagicMock:
    docs_stub = MagicMock(name="docs-v1-stub")
    docs_stub.documents().batchUpdate.return_value.execute.return_value = {}
    docs_stub.documents().get.return_value.execute.return_value = {
        "tabs": [
            {"tabProperties": {"tabId": "t.0", "title": "Tab 1"}},
            {"tabProperties": {"tabId": "t.new", "title": "Added"}},
        ]
    }
    return docs_stub


def test_add_tabs_to_doc_returns_schema_required_envelope():
    from appscriptly.services.docs.api import add_tabs_to_doc
    from appscriptly.tool_schemas import GDOCS_ADD_TABS_OUTPUT_SCHEMA

    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): _stub_docs_for_add_tabs()})
    ):
        result = add_tabs_to_doc(
            MagicMock(), "DOC-ADD", [{"title": "Added", "content": ""}]
        )

    for key in GDOCS_ADD_TABS_OUTPUT_SCHEMA["required"]:
        assert key in result, f"schema-required key {key!r} missing"
    assert result["doc_id"] == "DOC-ADD"
    assert result["url"] == "https://docs.google.com/document/d/DOC-ADD/edit"
    assert result["tabs"][0]["tab_id"] == "t.new"


def test_add_tabs_to_doc_empty_input_still_returns_required_keys():
    """The early-return (nothing to add) path must satisfy the same
    schema: a naive {"tabs": []} would fail output validation exactly
    like the main path used to."""
    from appscriptly.services.docs.api import add_tabs_to_doc
    from appscriptly.tool_schemas import GDOCS_ADD_TABS_OUTPUT_SCHEMA

    result = add_tabs_to_doc(MagicMock(), "DOC-EMPTY", [])
    for key in GDOCS_ADD_TABS_OUTPUT_SCHEMA["required"]:
        assert key in result, f"schema-required key {key!r} missing"
    assert result["tabs"] == []
