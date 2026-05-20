"""Co-located tests for services/docs/api.py pure helpers.

Per test architect (Round 3 review of M3 Phase A — PR #94): the 503-stmt
``api.py`` module's coverage was being supplied entirely by tests that
don't live next to it (``test_retrofit_text_normalization.py`` plus
indirect paths via tool registration). This file establishes the
co-located mirror-layout claim so that future Phase B work for
``services/drive/`` and ``services/gas_deploy/`` can follow the same
pattern without re-arguing the layout.

Scope: the pure helpers that touch NO Google API surface. Consumer
paths (the public ``make_doc_with_tabs`` / ``add_tabs_to_doc`` / etc.
functions that call ``get_service(...)`` internally) belong in tests
that use ``with_google_api_client(InMemoryGoogleAPIClient({...}))``
per the M2 pattern (PR #92) — those are deliberately out of scope here
because they're a different kind of test (consumer-path) and aren't
where the "pure helpers have no co-located home" gap lived.

The helpers tested here:

  _flatten_tab_tree          — pre-order tree walk, returns (depth, path, spec)
  _find_tab_by_id            — recursive tabId lookup over server-shape tabs
  _get_tab_depth             — recursive depth calculation, -1 on not-found
  _find_tab_by_title         — title-based variant of _find_tab_by_id
  _summarize_body_paragraphs — extract (style, text) from a body content list
  _tab_properties            — build the tabProperties dict for a TabSpec
  _rename_tab_request        — build an updateDocumentTabProperties request
  _add_tab_request           — build an addDocumentTab request
  _plain_text_requests       — build an insertText request (or empty list)
  render_content_to_requests — smoke-test the markdown -> batchUpdate path

This is layout-claim correctness, not a coverage drive — the test
architect's flag was that the module had NO co-located tests, not that
its coverage was too low. Adding ~10-15 small tests covers the public
shape of each pure helper.
"""
from __future__ import annotations

from google_docs_mcp.services.docs.api import (
    _add_tab_request,
    _find_tab_by_id,
    _find_tab_by_title,
    _flatten_tab_tree,
    _get_tab_depth,
    _plain_text_requests,
    _rename_tab_request,
    _summarize_body_paragraphs,
    _tab_properties,
    render_content_to_requests,
)


# ---------------------------------------------------------------------
# _flatten_tab_tree — pre-order traversal yields (depth, path, spec)
# ---------------------------------------------------------------------


def test_flatten_tab_tree_empty_input_returns_empty_list():
    assert _flatten_tab_tree([]) == []


def test_flatten_tab_tree_single_level_yields_depth_zero_and_increasing_paths():
    tabs = [
        {"title": "A", "content": ""},
        {"title": "B", "content": ""},
        {"title": "C", "content": ""},
    ]
    out = _flatten_tab_tree(tabs)
    # Three nodes, all depth 0, paths (0,), (1,), (2,).
    assert [d for d, _, _ in out] == [0, 0, 0]
    assert [p for _, p, _ in out] == [(0,), (1,), (2,)]
    assert [s["title"] for _, _, s in out] == ["A", "B", "C"]


def test_flatten_tab_tree_nested_yields_depth_first_order_with_depths():
    """Pre-order means parent emitted before its children."""
    tabs = [
        {
            "title": "A",
            "content": "",
            "children": [
                {"title": "A.1", "content": ""},
                {
                    "title": "A.2",
                    "content": "",
                    "children": [{"title": "A.2.x", "content": ""}],
                },
            ],
        },
        {"title": "B", "content": ""},
    ]
    out = _flatten_tab_tree(tabs)
    titles = [s["title"] for _, _, s in out]
    depths = [d for d, _, _ in out]
    paths = [p for _, p, _ in out]
    assert titles == ["A", "A.1", "A.2", "A.2.x", "B"]
    assert depths == [0, 1, 1, 2, 0]
    # Path semantics: tuple of sibling indices from root.
    assert paths == [(0,), (0, 0), (0, 1), (0, 1, 0), (1,)]


def test_flatten_tab_tree_treats_missing_and_none_children_alike():
    """Both ``"children" not in spec`` and ``children=None`` mean leaf.

    The traversal shape (depths + paths + visited titles) must be
    identical even though the spec dicts themselves differ by one
    key. Assert on the shape, not on the spec dicts, because
    ``_flatten_tab_tree`` returns the original specs by reference.
    """
    tabs_missing = [{"title": "A", "content": ""}]
    tabs_none = [{"title": "A", "content": "", "children": None}]
    out_missing = _flatten_tab_tree(tabs_missing)
    out_none = _flatten_tab_tree(tabs_none)
    shape = lambda out: [(d, p, s["title"]) for d, p, s in out]  # noqa: E731
    assert shape(out_missing) == shape(out_none) == [(0, (0,), "A")]


# ---------------------------------------------------------------------
# _find_tab_by_id — recursive lookup over server-shape tabs
# ---------------------------------------------------------------------


def test_find_tab_by_id_returns_none_for_empty_list():
    assert _find_tab_by_id([], "any-id") is None


def test_find_tab_by_id_finds_root_level_tab():
    tabs = [
        {"tabProperties": {"tabId": "t1"}, "childTabs": []},
        {"tabProperties": {"tabId": "t2"}, "childTabs": []},
    ]
    found = _find_tab_by_id(tabs, "t2")
    assert found is not None
    assert found["tabProperties"]["tabId"] == "t2"


def test_find_tab_by_id_descends_into_nested_childTabs():
    tabs = [
        {
            "tabProperties": {"tabId": "root"},
            "childTabs": [
                {
                    "tabProperties": {"tabId": "mid"},
                    "childTabs": [
                        {"tabProperties": {"tabId": "leaf"}, "childTabs": []},
                    ],
                }
            ],
        }
    ]
    found = _find_tab_by_id(tabs, "leaf")
    assert found is not None
    assert found["tabProperties"]["tabId"] == "leaf"


def test_find_tab_by_id_returns_none_when_id_absent():
    tabs = [{"tabProperties": {"tabId": "t1"}, "childTabs": []}]
    assert _find_tab_by_id(tabs, "nonexistent") is None


# ---------------------------------------------------------------------
# _get_tab_depth — depth (0=root), -1 if not found
# ---------------------------------------------------------------------


def test_get_tab_depth_returns_zero_for_root_level_tab():
    tabs = [{"tabProperties": {"tabId": "t1"}, "childTabs": []}]
    assert _get_tab_depth(tabs, "t1") == 0


def test_get_tab_depth_returns_negative_one_for_absent_id():
    tabs = [{"tabProperties": {"tabId": "t1"}, "childTabs": []}]
    assert _get_tab_depth(tabs, "missing") == -1


def test_get_tab_depth_counts_nesting_correctly():
    tabs = [
        {
            "tabProperties": {"tabId": "root"},
            "childTabs": [
                {
                    "tabProperties": {"tabId": "mid"},
                    "childTabs": [
                        {"tabProperties": {"tabId": "leaf"}, "childTabs": []},
                    ],
                }
            ],
        }
    ]
    assert _get_tab_depth(tabs, "root") == 0
    assert _get_tab_depth(tabs, "mid") == 1
    assert _get_tab_depth(tabs, "leaf") == 2


# ---------------------------------------------------------------------
# _find_tab_by_title — title-based variant, exact match
# ---------------------------------------------------------------------


def test_find_tab_by_title_finds_at_root_and_in_nested():
    tabs = [
        {
            "tabProperties": {"tabId": "t1", "title": "Top"},
            "childTabs": [
                {
                    "tabProperties": {"tabId": "t2", "title": "Inner"},
                    "childTabs": [],
                }
            ],
        }
    ]
    assert _find_tab_by_title(tabs, "Top")["tabProperties"]["tabId"] == "t1"
    assert _find_tab_by_title(tabs, "Inner")["tabProperties"]["tabId"] == "t2"


def test_find_tab_by_title_exact_match_only_no_substring():
    tabs = [
        {
            "tabProperties": {"tabId": "t1", "title": "Hello World"},
            "childTabs": [],
        }
    ]
    assert _find_tab_by_title(tabs, "Hello") is None
    assert _find_tab_by_title(tabs, "Hello World") is not None


# ---------------------------------------------------------------------
# _summarize_body_paragraphs — extract style + text
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
