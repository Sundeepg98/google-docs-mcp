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
    read_tab_content,
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
