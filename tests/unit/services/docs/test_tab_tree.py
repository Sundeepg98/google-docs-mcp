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

from appscriptly.services.docs.tab_tree import (
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


# ---------------------------------------------------------------------
# Hypothesis property tests — _flatten_tab_tree (R1 prediction / R5 Gap #6)
#
# Round 1 test architect: "A hypothesis-based property test on
# _flatten_tab_tree would catch entire bug classes including any
# off-by-one in the path-tracking — for ~30 LOC." R14 #8 split now
# unblocks this (pre-split api.py at 1,050 LOC made strategy-driven
# tests prohibitive; the 99-LOC tab_tree.py is the right granularity).
#
# The 4 properties below pin the function's full contract:
#   1. flat-list length == total tree node count
#   2. every emitted path is unique (no duplicates)
#   3. emitted depth == len(path) - 1 (the docstring's stated invariant)
#   4. pre-order = parent immediately precedes its first child
#
# Strategy: recursive dict with random ASCII titles + a small child
# fanout. ``max_leaves=10`` keeps each generated tree bounded so the
# 100-example default runs in <1s per test.
# ---------------------------------------------------------------------
from hypothesis import given, settings  # noqa: E402 — keep imports near use
from hypothesis import strategies as st  # noqa: E402

# Bounded recursive strategy: depth typically ≤4, fanout ≤3, leaf
# count ≤10. Hypothesis shrinks failing trees to their minimum form
# (e.g. a 2-node tree) on failure, which makes diagnosis trivial.
_tab_strategy = st.recursive(
    st.fixed_dictionaries({"title": st.text(min_size=1, max_size=10)}),
    lambda children: st.fixed_dictionaries({
        "title": st.text(min_size=1, max_size=10),
        "children": st.lists(children, max_size=3),
    }),
    max_leaves=10,
)


def _count_nodes(tree: dict) -> int:
    """Count every node in a tree (including the root)."""
    return 1 + sum(_count_nodes(c) for c in tree.get("children") or [])


@given(tree=_tab_strategy)
def test_property_flatten_tab_tree_preserves_node_count(tree):
    """Property: ``len(flatten) == total node count`` for any input tree.

    Catches: missing/double-yielding nodes in the recursion, off-by-one
    in the ``for i, spec in enumerate(specs)`` loop, accidental filter.
    """
    flat = _flatten_tab_tree([tree])
    assert len(flat) == _count_nodes(tree)


@given(tree=_tab_strategy)
def test_property_flatten_tab_tree_no_duplicate_paths(tree):
    """Property: every emitted ``path`` tuple is unique.

    Paths are constructed from sibling-index tuples; a duplicate would
    mean the walker visited the same node twice. Catches: parent_path
    not being threaded correctly, indices being reused across siblings.
    """
    flat = _flatten_tab_tree([tree])
    paths = [path for _depth, path, _spec in flat]
    assert len(paths) == len(set(paths)), (
        f"duplicate path(s) in flatten output: paths={paths!r}"
    )


@given(tree=_tab_strategy)
def test_property_flatten_tab_tree_depth_matches_path_length(tree):
    """Property: the docstring states ``depth == len(path) - 1``.

    The implementation computes both independently (``depth`` from the
    recursion's depth tracker, ``len(path)`` from the parent_path chain);
    a divergence between the two would be a stale-state bug. Pinning
    the equality forces both code paths to agree forever.
    """
    flat = _flatten_tab_tree([tree])
    for depth, path, _spec in flat:
        assert depth == len(path) - 1, (
            f"depth={depth} but len(path)={len(path)} for path={path!r}"
        )


@given(tree=_tab_strategy)
def test_property_flatten_tab_tree_is_pre_order(tree):
    """Property: pre-order traversal — every child's emission position
    comes AFTER its parent's. Catches: post-order regression, breadth-
    first regression, accidental sort.

    Concretely: for every emitted (path), check that every strict
    prefix of that path (i.e. its ancestors) appears EARLIER in the
    flat list. ``()`` (the synthetic root) isn't emitted, so the only
    prefix that matters for a root-level path ``(i,)`` is empty —
    auto-satisfied.
    """
    flat = _flatten_tab_tree([tree])
    positions = {path: idx for idx, (_depth, path, _spec) in enumerate(flat)}
    for path, pos in positions.items():
        # Every non-empty prefix of ``path`` must already have been
        # emitted before ``pos``. Skip the empty prefix (synthetic root).
        for prefix_len in range(1, len(path)):
            ancestor = path[:prefix_len]
            assert ancestor in positions, (
                f"ancestor {ancestor!r} of {path!r} missing from output"
            )
            assert positions[ancestor] < pos, (
                f"ancestor {ancestor!r} (pos {positions[ancestor]}) "
                f"emitted AFTER descendant {path!r} (pos {pos}) — "
                f"pre-order violated"
            )


# Hypothesis @settings for the multi-tree variant: bumping to 50
# examples gives broader coverage on the list-of-trees shape without
# blowing up runtime. Default 100 × multi-tree fanout would be slow.
@given(forest=st.lists(_tab_strategy, max_size=5))
@settings(max_examples=50)
def test_property_flatten_tab_tree_handles_multi_root_forests(forest):
    """Property: passing a LIST of top-level trees yields the union of
    their per-tree flattens with sibling-index paths starting at the
    forest position. ``_flatten_tab_tree`` is documented as accepting
    a list; this checks the documented contract on multi-root input.
    """
    flat = _flatten_tab_tree(forest)
    total = sum(_count_nodes(t) for t in forest)
    assert len(flat) == total

    # Root-level paths must be ``(0,), (1,), ...`` in order — no gaps,
    # no duplicates, monotonically increasing root indices.
    root_paths = [path for _d, path, _s in flat if len(path) == 1]
    expected_roots = [(i,) for i in range(len(forest))]
    assert root_paths == expected_roots, (
        f"root-level paths expected {expected_roots!r}, got {root_paths!r}"
    )
