"""Co-located tests for services/docs/tab_tree.py (R14 #8 split).

Pure tree-walking helpers extracted from ``api.py`` in v2.2.1.
The tests below were previously in ``test_api.py``; moved here as
part of the R14 #8 split to give the renderer / tree-walking modules
their own co-located test homes (test architect's Phase B lesson —
ship test_X.py from day one when extracting code into X.py).

Helpers tested here (all live in ``services/docs/tab_tree.py``):

  _flatten_tab_tree    — pre-order tree walk, returns (depth, path, spec)
  _find_tab_by_id      — recursive tabId lookup over server-shape tabs
  _get_tab_depth       — recursive depth calculation, -1 on not-found
  _find_tab_by_title   — title-based variant of _find_tab_by_id

These functions touch NO Google API surface, so the tests are pure
isolation — no ``with_google_api_client(...)`` needed.
"""
from __future__ import annotations

from google_docs_mcp.services.docs.tab_tree import (
    _find_tab_by_id,
    _find_tab_by_title,
    _flatten_tab_tree,
    _get_tab_depth,
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
