"""Co-located tests for services/docs/markdown_render.py (R14 #8 split).

Pure markdown → request-payload helpers extracted from ``api.py`` in
v2.2.1. The tests below were previously in ``test_api.py``; moved here
as part of the R14 #8 split.

Helpers tested here (all live in ``services/docs/markdown_render.py``):

  _tab_properties           — TabSpec -> tabProperties dict
  _rename_tab_request       — updateDocumentTabProperties batch request
  _add_tab_request          — addDocumentTab batch request
  _plain_text_requests      — insertText request (or empty list)
  render_content_to_requests — markdown -> batchUpdate request list

The renderer functions touch NO Google API surface — pure
markdown-it state machine + dict construction. The isolation that
this module-level split provides unblocks the R6 UTF-16 bug fix in
a future PR: the fix lands HERE, and the regression test lives HERE,
without needing Google API mocks.
"""
from __future__ import annotations

from google_docs_mcp.services.docs.markdown_render import (
    _add_tab_request,
    _plain_text_requests,
    _rename_tab_request,
    _tab_properties,
    render_content_to_requests,
)


# ---------------------------------------------------------------------
# _tab_properties — TabSpec -> tabProperties dict
# ---------------------------------------------------------------------


def test_tab_properties_includes_title_by_default():
    tab = {"title": "My Tab", "content": ""}
    assert _tab_properties(tab) == {"title": "My Tab"}


def test_tab_properties_omits_title_when_include_title_false():
    tab = {"title": "My Tab", "content": ""}
    assert _tab_properties(tab, include_title=False) == {}


def test_tab_properties_passes_through_icon_emoji_when_present():
    tab = {"title": "My Tab", "content": "", "icon_emoji": "[STAR]"}
    props = _tab_properties(tab)
    assert props == {"title": "My Tab", "iconEmoji": "[STAR]"}


def test_tab_properties_omits_icon_emoji_when_falsy():
    """The implementation guards against empty-string + None — neither emits iconEmoji."""
    assert "iconEmoji" not in _tab_properties({"title": "x", "content": "", "icon_emoji": ""})
    assert "iconEmoji" not in _tab_properties({"title": "x", "content": "", "icon_emoji": None})


# ---------------------------------------------------------------------
# _rename_tab_request + _add_tab_request — batchUpdate request builders
# ---------------------------------------------------------------------


def test_rename_tab_request_builds_updateDocumentTabProperties_with_fields_mask():
    tab = {"title": "Renamed", "content": "", "icon_emoji": "[STAR]"}
    req = _rename_tab_request("t1", tab)
    assert "updateDocumentTabProperties" in req
    body = req["updateDocumentTabProperties"]
    assert body["tabProperties"]["tabId"] == "t1"
    assert body["tabProperties"]["title"] == "Renamed"
    assert body["tabProperties"]["iconEmoji"] == "[STAR]"
    # Field mask must list every property except tabId (which is the key).
    fields = set(body["fields"].split(","))
    assert fields == {"title", "iconEmoji"}


def test_rename_tab_request_field_mask_excludes_unset_icon_emoji():
    tab = {"title": "Only Title", "content": ""}
    req = _rename_tab_request("t1", tab)
    body = req["updateDocumentTabProperties"]
    assert body["fields"] == "title"
    assert "iconEmoji" not in body["tabProperties"]


def test_add_tab_request_omits_parentTabId_at_top_level():
    tab = {"title": "Top", "content": ""}
    req = _add_tab_request(tab)
    props = req["addDocumentTab"]["tabProperties"]
    assert props == {"title": "Top"}
    assert "parentTabId" not in props


def test_add_tab_request_includes_parentTabId_when_provided():
    tab = {"title": "Child", "content": ""}
    req = _add_tab_request(tab, parent_tab_id="parent-id")
    props = req["addDocumentTab"]["tabProperties"]
    assert props == {"title": "Child", "parentTabId": "parent-id"}


# ---------------------------------------------------------------------
# _plain_text_requests — insertText request (or empty list)
# ---------------------------------------------------------------------


def test_plain_text_requests_returns_empty_list_for_empty_content():
    assert _plain_text_requests("", "tab-1") == []


def test_plain_text_requests_emits_insertText_at_index_1_for_nonempty():
    requests = _plain_text_requests("Hello", "tab-xyz")
    assert requests == [
        {
            "insertText": {
                "location": {"tabId": "tab-xyz", "index": 1},
                "text": "Hello",
            }
        }
    ]


# ---------------------------------------------------------------------
# render_content_to_requests — markdown -> batchUpdate smoke tests
# ---------------------------------------------------------------------


def test_render_content_to_requests_returns_empty_for_empty_or_whitespace():
    assert render_content_to_requests("", "tab-1") == []
    assert render_content_to_requests("   \n  \t", "tab-1") == []


def test_render_content_to_requests_emits_insertText_for_plain_paragraph():
    """The simplest non-empty input must produce at least one insertText
    request targeting the supplied tab_id at the starting index."""
    requests = render_content_to_requests("hello world", "tab-1")
    assert requests, "non-empty markdown must produce at least one request"
    inserts = [r for r in requests if "insertText" in r]
    assert inserts, "plain paragraph must emit an insertText"
    # Every insert request targets the supplied tab.
    for r in inserts:
        assert r["insertText"]["location"]["tabId"] == "tab-1"


def test_render_content_to_requests_respects_starting_index():
    """When appending into an existing body, ``starting_index`` shifts
    the FIRST insert's location forward — important so we insert before
    the body's trailing newline rather than at index 1."""
    requests_at_1 = render_content_to_requests("x", "tab-1", starting_index=1)
    requests_at_100 = render_content_to_requests("x", "tab-1", starting_index=100)
    assert requests_at_1, "baseline starting_index=1 should emit requests"
    assert requests_at_100, "starting_index=100 should also emit requests"
    first_at_1 = next(
        r for r in requests_at_1 if "insertText" in r
    )["insertText"]["location"]["index"]
    first_at_100 = next(
        r for r in requests_at_100 if "insertText" in r
    )["insertText"]["location"]["index"]
    assert first_at_100 - first_at_1 == 99, (
        "starting_index offset must propagate to the first insert's location"
    )
