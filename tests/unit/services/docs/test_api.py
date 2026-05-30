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

from appscriptly.services.docs.api import _summarize_body_paragraphs


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
