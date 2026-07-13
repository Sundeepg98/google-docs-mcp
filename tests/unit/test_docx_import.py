"""Tests for ``docx_import.py`` pure parsing helpers.

The docx-import pipeline (``convert_docx_to_tabbed_doc``) glues together
Drive uploads, REST tab-shell creation, and the REST content transplant.
The orchestration itself is covered in ``test_docx_import_pipeline.py``.

This file covers the **pure helpers** that decide *where* the splits
go — the parsing logic that walks Google's body-content shape and
emits ``_SplitPoint`` records. Those helpers touch no Google API
surface, so they're prime hypothesis targets.

Helpers covered:

  _extract_paragraph_text    — concat ``textRun.content`` across elements
  _max_depth                 — recursive depth of a SplitPoint forest
  _split_to_tabspec          — convert _SplitPoint → TabSpec for shells
  _docapp_children           - the sectionBreak filter (range index space)
  _flatten_splits            - pre-order forest flatten (tab alignment)
  _detect_splits             — the big one; walks body, emits splits

(``_splits_to_json``, the Apps Script POST payload serializer, was
deleted with the /exec step; its tests went with it.)

Round 1 test architect (R14 #8): "docx_import.py at 19% on 182
statements is a real gap. Hypothesis would close this exactly the way
it closed services/docs/api.py." Same pattern as PR #112's
tab_tree/markdown_render hypothesis tests — adapt the strategies to
the actual body-content shape that ``_detect_splits`` consumes.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from appscriptly.docx_import import (
    _MAX_TAB_TITLE,
    _SplitPoint,
    _dedupe_split_titles,
    _detect_splits,
    _docapp_children,
    _existing_tab_titles,
    _extract_paragraph_text,
    _flatten_splits,
    _max_depth,
    _split_to_tabspec,
    _unique_tab_title,
)

# ---------------------------------------------------------------------
# Unit examples — single-call sanity for each helper. Hypothesis tests
# follow each helper's example block.
# ---------------------------------------------------------------------


# _extract_paragraph_text ---------------------------------------------


def test_extract_paragraph_text_empty_paragraph_returns_empty_string():
    assert _extract_paragraph_text({"elements": []}) == ""


def test_extract_paragraph_text_missing_elements_key_returns_empty_string():
    """Function uses ``.get("elements", [])`` — missing key shouldn't blow up."""
    assert _extract_paragraph_text({}) == ""


def test_extract_paragraph_text_concatenates_text_runs_and_strips():
    para = {
        "elements": [
            {"textRun": {"content": "  Hello "}},
            {"textRun": {"content": "world  "}},
        ]
    }
    # The function ``.strip()``s the joined result.
    assert _extract_paragraph_text(para) == "Hello world"


def test_extract_paragraph_text_skips_non_textrun_elements():
    """pageBreak / inlineObject / etc. have no ``textRun`` — should be skipped."""
    para = {
        "elements": [
            {"textRun": {"content": "Before "}},
            {"pageBreak": {}},
            {"textRun": {"content": "after"}},
        ]
    }
    assert _extract_paragraph_text(para) == "Before after"


# _max_depth -----------------------------------------------------------


def test_max_depth_empty_list_returns_negative_one():
    """Documented sentinel: ``_max_depth([]) == -1``."""
    assert _max_depth([]) == -1


def test_max_depth_single_flat_split_returns_zero():
    split: _SplitPoint = {
        "title": "Only",
        "icon_emoji": None,
        "ranges": [(0, 0)],
        "children": [],
    }
    assert _max_depth([split]) == 0


def test_max_depth_one_nested_level_returns_one():
    leaf: _SplitPoint = {
        "title": "leaf", "icon_emoji": None, "ranges": [(1, 1)], "children": [],
    }
    parent: _SplitPoint = {
        "title": "parent", "icon_emoji": None, "ranges": [(0, 0)],
        "children": [leaf],
    }
    assert _max_depth([parent]) == 1


# _split_to_tabspec ----------------------------------------------------


def test_split_to_tabspec_omits_icon_and_children_when_absent():
    split: _SplitPoint = {
        "title": "T", "icon_emoji": None, "ranges": [(0, 0)], "children": [],
    }
    spec = _split_to_tabspec(split)
    assert spec == {"title": "T", "content": ""}


def test_split_to_tabspec_includes_icon_when_set():
    split: _SplitPoint = {
        "title": "T", "icon_emoji": "\U0001f4d1",
        "ranges": [(0, 0)], "children": [],
    }
    spec = _split_to_tabspec(split)
    # ``.get()`` for keys that are optional in TabSpec — pyright surfaces
    # the optionality via reportTypedDictNotRequiredAccess; the runtime
    # assertion is unchanged.
    assert spec.get("icon_emoji") == "\U0001f4d1"


def test_split_to_tabspec_recurses_into_children():
    leaf: _SplitPoint = {
        "title": "leaf", "icon_emoji": None, "ranges": [(1, 1)], "children": [],
    }
    parent: _SplitPoint = {
        "title": "parent", "icon_emoji": None, "ranges": [(0, 0)],
        "children": [leaf],
    }
    spec = _split_to_tabspec(parent)
    assert spec.get("children") == [{"title": "leaf", "content": ""}]


# _docapp_children / _flatten_splits -----------------------------------


def test_docapp_children_filters_only_section_breaks():
    body = [
        {"sectionBreak": {}},
        {"paragraph": {"elements": []}},
        {"table": {"tableRows": []}},
        {"sectionBreak": {}},
    ]
    filtered = _docapp_children(body)
    assert len(filtered) == 2
    # Same object identity: elements keep their startIndex/endIndex, so
    # split ranges can slice this list and still address the live doc.
    assert filtered[0] is body[1]
    assert filtered[1] is body[2]


def test_flatten_splits_is_preorder():
    leaf: _SplitPoint = {
        "title": "leaf", "icon_emoji": None, "ranges": [(2, 2)], "children": [],
    }
    parent: _SplitPoint = {
        "title": "parent", "icon_emoji": None, "ranges": [(0, 0)],
        "children": [leaf],
    }
    sibling: _SplitPoint = {
        "title": "sibling", "icon_emoji": None, "ranges": [(3, 3)], "children": [],
    }
    flat = _flatten_splits([parent, sibling])
    assert [s["title"] for s in flat] == ["parent", "leaf", "sibling"]


# _detect_splits — unit examples ---------------------------------------


def _heading_para(text: str, style: str = "HEADING_1") -> dict:
    """Build a body-content paragraph with the given namedStyleType."""
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": {"content": text}}],
        }
    }


def _body_para(text: str) -> dict:
    """Build a non-heading body paragraph (style NORMAL_TEXT)."""
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{"textRun": {"content": text}}],
        }
    }


def _section_break() -> dict:
    """Body-content element that _detect_splits MUST filter out."""
    return {"sectionBreak": {"sectionStyle": {}}}


def _page_break_para() -> dict:
    """A paragraph whose first element is a pageBreak."""
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{"pageBreak": {}}],
        }
    }


def _table_element() -> dict:
    """A body-content element that is NOT a paragraph and NOT a
    sectionBreak — survives the sectionBreak filter and goes down
    the ``para is None`` branch of the walker, which extends the
    current split's range. Real shape: ``{"table": {...}}``.
    """
    return {"table": {"rows": 0, "columns": 0, "tableRows": []}}


def test_detect_splits_empty_body_returns_no_splits():
    splits, strategy = _detect_splits([], "heading_1")
    assert splits == []
    assert strategy == "heading_1"


def test_detect_splits_finds_each_heading_1():
    body = [
        _heading_para("Intro"),
        _body_para("intro body"),
        _heading_para("Methods"),
        _body_para("methods body"),
    ]
    splits, strategy = _detect_splits(body, "heading_1")
    assert strategy == "heading_1"
    assert [s["title"] for s in splits] == ["Intro", "Methods"]


def test_detect_splits_ignores_heading_2_when_strategy_is_heading_1():
    body = [
        _heading_para("Big", style="HEADING_1"),
        _heading_para("Small", style="HEADING_2"),
    ]
    splits, _ = _detect_splits(body, "heading_1")
    assert [s["title"] for s in splits] == ["Big"]


def test_detect_splits_filters_section_break_so_indices_match_apps_script():
    """``sectionBreak`` elements MUST be excluded — Apps Script's
    Body.getChild() doesn't return them, and including them in the
    index space would push the trailing ranges out of bounds.
    """
    body = [
        _section_break(),
        _heading_para("H1"),
        _body_para("body"),
        _section_break(),
    ]
    splits, _ = _detect_splits(body, "heading_1")
    # docapp_children has 2 entries after filter: H1 at idx 0, body at idx 1.
    assert len(splits) == 1
    assert splits[0]["ranges"] == [(0, 1)]


def test_detect_splits_truncates_titles_over_50_chars():
    """Google's tab-title API caps at 50 chars; we truncate to match."""
    body = [_heading_para("X" * 100)]
    splits, _ = _detect_splits(body, "heading_1")
    assert len(splits[0]["title"]) == 50


def test_detect_splits_falls_back_to_section_n_when_heading_text_empty():
    body = [_heading_para("")]
    splits, _ = _detect_splits(body, "heading_1")
    assert splits[0]["title"] == "Section 1"


def test_detect_splits_extends_current_split_range_over_non_paragraph_elements():
    """A non-paragraph, non-sectionBreak element (e.g. a table) must
    extend the current split's range — Apps Script counts tables as
    Body children, so the index must advance.
    """
    body = [
        _heading_para("Section1"),
        _body_para("text"),
        _table_element(),
        _body_para("after table"),
    ]
    splits, _ = _detect_splits(body, "heading_1")
    assert len(splits) == 1
    # docapp_children = [H1@0, P@1, Table@2, P@3]; range covers all 4.
    assert splits[0]["ranges"] == [(0, 3)]


def test_detect_splits_non_paragraph_before_any_split_is_dropped():
    """Non-paragraph elements that appear BEFORE the first split have
    nowhere to attach (no current split) — they're silently dropped,
    not promoted to a synthetic split.
    """
    body = [
        _table_element(),
        _body_para("pre-content"),
        _heading_para("RealStart"),
    ]
    splits, _ = _detect_splits(body, "heading_1")
    # Only one split: the H1. The table + body before it are dropped.
    assert [s["title"] for s in splits] == ["RealStart"]
    # docapp_children = [Table@0, P@1, H1@2]; range starts at 2, no extension after.
    assert splits[0]["ranges"] == [(2, 2)]


def test_detect_splits_auto_strategy_returns_first_nonempty():
    """``auto`` tries heading_1, heading_2, page_break in order."""
    body = [
        _heading_para("H1Hit", style="HEADING_1"),
        _heading_para("H2Also", style="HEADING_2"),
    ]
    splits, strategy = _detect_splits(body, "auto")
    assert strategy == "heading_1"
    assert [s["title"] for s in splits] == ["H1Hit"]


def test_detect_splits_auto_strategy_falls_through_to_page_break():
    body = [
        _body_para("intro"),
        _page_break_para(),
        _body_para("after"),
    ]
    splits, strategy = _detect_splits(body, "auto")
    assert strategy == "page_break"
    # Page-break titles auto-generated as "Page N".
    assert splits[0]["title"] == "Page 2"


def test_detect_splits_auto_returns_empty_when_no_strategy_matches():
    body = [_body_para("nothing splittable here")]
    splits, strategy = _detect_splits(body, "auto")
    assert splits == []
    assert strategy == "auto"


# _detect_splits — nest_by="heading_2" (nested split tree) --------------


def test_detect_splits_nest_by_builds_depth_2_tree_with_per_node_ranges():
    """The core nested contract: H1 -> parent, H2 -> child of the
    current parent, content between an H1 and its first H2 stays in
    the parent's range, each child's range runs from its heading to
    the last element before the next heading."""
    body = [
        _heading_para("Part A"),                    # 0  parent
        _body_para("a intro"),                      # 1  parent content
        _heading_para("A.1", style="HEADING_2"),    # 2  child
        _body_para("a1 body"),                      # 3  child content
        _heading_para("A.2", style="HEADING_2"),    # 4  child
        _body_para("a2 body"),                      # 5  child content
        _heading_para("Part B"),                    # 6  parent
        _body_para("b body"),                       # 7  parent content
    ]
    splits, strategy = _detect_splits(body, "heading_1", nest_by="heading_2")
    assert strategy == "heading_1"
    assert [s["title"] for s in splits] == ["Part A", "Part B"]
    part_a, part_b = splits
    assert [c["title"] for c in part_a["children"]] == ["A.1", "A.2"]
    assert part_b["children"] == []
    assert part_a["ranges"] == [(0, 1)]
    assert part_a["children"][0]["ranges"] == [(2, 3)]
    assert part_a["children"][1]["ranges"] == [(4, 5)]
    assert part_b["ranges"] == [(6, 7)]


def test_detect_splits_nest_by_h2_before_first_h1_stays_in_placeholder():
    """DOCUMENTED DECISION: a Heading 2 before the first Heading 1 has
    no parent to attach to. It is treated exactly like any other
    pre-first-split content in flat mode — left behind in the
    placeholder tab. NOT promoted to a tab, NOT an error."""
    body = [
        _heading_para("Orphan", style="HEADING_2"),  # 0  placeholder-bound
        _body_para("orphan body"),                   # 1  placeholder-bound
        _heading_para("Real Part"),                  # 2
        _body_para("part body"),                     # 3
    ]
    splits, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    assert [s["title"] for s in splits] == ["Real Part"]
    assert splits[0]["children"] == []
    assert splits[0]["ranges"] == [(2, 3)]


def test_detect_splits_nest_by_consecutive_h2s_make_heading_only_children():
    body = [
        _heading_para("Part"),                       # 0
        _heading_para("One", style="HEADING_2"),     # 1  heading-only child
        _heading_para("Two", style="HEADING_2"),     # 2
        _body_para("two body"),                      # 3
    ]
    splits, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    (part,) = splits
    # H1 with no content before its first H2: the parent's range is
    # just its own heading paragraph.
    assert part["ranges"] == [(0, 0)]
    one, two = part["children"]
    assert one["ranges"] == [(1, 1)]
    assert two["ranges"] == [(2, 3)]


def test_detect_splits_nest_by_without_any_h2_matches_flat_output():
    """A doc with H1s but no H2s must behave exactly as today."""
    body = [
        _heading_para("Intro"),
        _body_para("intro body"),
        _heading_para("Methods"),
        _body_para("methods body"),
    ]
    nested, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    flat, _ = _detect_splits(body, "heading_1")
    assert nested == flat


def test_detect_splits_nest_by_non_paragraph_extends_the_open_child():
    """Tables and other non-paragraph elements after an H2 belong to
    that child (the walker extends the OPEN node, which is the child
    once one exists)."""
    body = [
        _heading_para("Part"),                       # 0
        _heading_para("Sub", style="HEADING_2"),     # 1
        _table_element(),                            # 2 -> extends child
        _body_para("after table"),                   # 3
    ]
    splits, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    assert splits[0]["ranges"] == [(0, 0)]
    assert splits[0]["children"][0]["ranges"] == [(1, 3)]


def test_detect_splits_nest_by_content_after_h1_following_children_starts_new_parent_range():
    """After a child section, the next H1 opens a fresh parent whose
    range is independent of the previous parent's children."""
    body = [
        _heading_para("A"),                          # 0
        _heading_para("A.1", style="HEADING_2"),     # 1
        _body_para("a1"),                            # 2
        _heading_para("B"),                          # 3
        _body_para("b"),                             # 4
        _heading_para("B.1", style="HEADING_2"),     # 5
    ]
    splits, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    a, b = splits
    assert a["ranges"] == [(0, 0)]
    assert a["children"][0]["ranges"] == [(1, 2)]
    assert b["ranges"] == [(3, 4)]
    assert b["children"][0]["ranges"] == [(5, 5)]


# ---------------------------------------------------------------------
# Hypothesis property tests — same pattern as PR #112 / R14 #8.
#
# Strategy: build random body-content lists from a 3-shape vocabulary
# (sectionBreak / heading_1 paragraph / non-heading paragraph), then
# assert structural invariants of _detect_splits.
#
# Properties pinned:
#   1. # splits == # heading_1 paragraphs in input (after sectionBreak filter)
#   2. every split has at least one (lo,hi) range with lo<=hi
#   3. ranges never escape the filtered-children index space
#   4. titles are bounded length (≤50 chars per Google's API limit)
#   5. ranges are non-overlapping and ordered ascending per split
# ---------------------------------------------------------------------

# Element strategies — each emits a body-content dict with a known shape.
_heading_1_strategy = st.builds(
    _heading_para,
    text=st.text(min_size=0, max_size=120),
    style=st.just("HEADING_1"),
)
_heading_2_strategy = st.builds(
    _heading_para,
    text=st.text(min_size=0, max_size=120),
    style=st.just("HEADING_2"),
)
_normal_paragraph_strategy = st.builds(
    _body_para, text=st.text(min_size=0, max_size=120),
)
_section_break_strategy = st.builds(_section_break)
_table_strategy = st.builds(_table_element)

# Mixed body — random selection across the 5 shapes. Skewed slightly
# toward normal paragraphs so we get realistic densities (heading-only
# docs are pathological and would slow shrinkage without buying signal).
# ``table`` covers the non-paragraph-non-sectionBreak walker branch.
_body_element_strategy = st.one_of(
    _heading_1_strategy,
    _heading_2_strategy,
    _normal_paragraph_strategy,
    _section_break_strategy,
    _table_strategy,
)


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_detect_splits_count_matches_heading_1_count(body):
    """Property: # of detected splits == # of HEADING_1 paragraphs.

    The function's contract: every paragraph whose ``namedStyleType``
    is the target style becomes one split. Verified by counting the
    inputs that match and comparing against the output length.

    Catches: double-counting, off-by-one (e.g., missing the first or
    last heading), accidentally counting sectionBreaks or other styles.
    """
    splits, _ = _detect_splits(body, "heading_1")
    # Count heading_1 paragraphs AFTER the sectionBreak filter (matches
    # what _detect_splits walks internally).
    expected_count = sum(
        1 for elem in body
        if "sectionBreak" not in elem
        and elem.get("paragraph", {}).get(
            "paragraphStyle", {}
        ).get("namedStyleType") == "HEADING_1"
    )
    assert len(splits) == expected_count


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_detect_splits_ranges_well_formed(body):
    """Property: every range is ``(lo, hi)`` with ``lo <= hi`` and both
    in ``[0, len(docapp_children))``.

    Catches: range start/end swap, negative indices from off-by-one,
    ranges escaping the filtered-children index space.
    """
    splits, _ = _detect_splits(body, "heading_1")
    docapp_children_count = sum(
        1 for elem in body if "sectionBreak" not in elem
    )
    for split in splits:
        assert split["ranges"], "every split must have at least one range"
        for lo, hi in split["ranges"]:
            assert 0 <= lo <= hi < docapp_children_count, (
                f"range ({lo}, {hi}) escapes [0, {docapp_children_count})"
            )


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_detect_splits_titles_bounded_by_api_limit(body):
    """Property: every split title is ≤50 chars (Google API hard limit).

    Catches: a future change that drops the ``[:50]`` truncation, or an
    edge case where the fallback ``Section N`` formula somehow overshoots.
    """
    splits, _ = _detect_splits(body, "heading_1")
    for split in splits:
        assert len(split["title"]) <= 50, (
            f"title {split['title']!r} exceeds 50-char API limit "
            f"(len={len(split['title'])})"
        )


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_detect_splits_ranges_non_overlapping_and_ordered(body):
    """Property: across all splits, ranges are non-overlapping and
    appear in ascending order (the first split's first range starts
    before the second split's first range, etc.).

    Catches: a split being inserted out of position, a range being
    extended across a later split's start point.
    """
    splits, _ = _detect_splits(body, "heading_1")
    # Collect (lo, hi, split_idx) across all ranges.
    all_ranges = [
        (lo, hi, i)
        for i, s in enumerate(splits)
        for lo, hi in s["ranges"]
    ]
    # Sort by lo. Assert the sort order matches the natural emit order
    # AND there are no overlaps between consecutive ranges.
    sorted_ranges = sorted(all_ranges, key=lambda r: r[0])
    assert all_ranges == sorted_ranges, (
        "splits/ranges not emitted in ascending range order"
    )
    for prev, curr in zip(sorted_ranges, sorted_ranges[1:]):
        prev_lo, prev_hi, _ = prev
        curr_lo, _curr_hi, _ = curr
        assert prev_hi < curr_lo, (
            f"ranges overlap: {prev[:2]} and {curr[:2]}"
        )


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_detect_splits_strategy_echo_matches_input(body):
    """Property: when ``split_by`` is a concrete strategy (not "auto"),
    the second return value equals the input.

    Catches: a future change to the strategy-string handling that
    silently substitutes a different label (e.g. logging a normalized
    form). The contract that callers depend on is identity.
    """
    splits, returned_strategy = _detect_splits(body, "heading_1")
    assert returned_strategy == "heading_1"
    splits2, returned_strategy2 = _detect_splits(body, "heading_2")
    assert returned_strategy2 == "heading_2"


# ---------------------------------------------------------------------
# Hypothesis property tests - nest_by="heading_2" (nested walk)
# ---------------------------------------------------------------------


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_nested_ranges_partition_the_flat_ranges(body):
    """Property: for every H1 section, [parent range + child ranges]
    is a contiguous ascending partition of the FLAT walk's range for
    that same section. Nesting re-buckets content between parent and
    children — it must never drop, duplicate, or reorder an element
    relative to the flat split.

    Catches: a parent range that swallows its first child's heading,
    a gap between a parent and its first child, child ranges that
    leak across the next H1 boundary.
    """
    flat, _ = _detect_splits(body, "heading_1")
    nested, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    assert len(nested) == len(flat)
    for flat_node, nested_node in zip(flat, nested):
        pieces = [nested_node, *nested_node["children"]]
        covered = [r for p in pieces for r in p["ranges"]]
        ((flat_lo, flat_hi),) = flat_node["ranges"]
        assert covered[0][0] == flat_lo
        assert covered[-1][1] == flat_hi
        for (_, prev_hi), (curr_lo, _) in zip(covered, covered[1:]):
            assert curr_lo == prev_hi + 1, "partition must be gapless"


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_nested_depth_capped_at_2_levels_and_child_count_exact(body):
    """Property: the nested walk emits at most parent+child (depth 1
    in _max_depth terms — v1's max-depth-2 contract), and exactly one
    child per HEADING_2 that appears after the first HEADING_1."""
    nested, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    assert _max_depth(nested) <= 1

    expected_children = 0
    seen_h1 = False
    for elem in body:
        if "sectionBreak" in elem:
            continue
        style = (
            elem.get("paragraph", {})
            .get("paragraphStyle", {})
            .get("namedStyleType")
        )
        if style == "HEADING_1":
            seen_h1 = True
        elif style == "HEADING_2" and seen_h1:
            expected_children += 1
    assert sum(len(p["children"]) for p in nested) == expected_children


@given(body=st.lists(_body_element_strategy, max_size=20))
def test_property_nested_flattened_ranges_well_formed_and_ascending(body):
    """Property: in pre-order (the order shells are created and
    transplant slices are planned), all ranges are well-formed, stay
    inside the filtered index space, and are strictly ascending —
    the same invariant the flat walk guarantees, extended to the tree.
    """
    nested, _ = _detect_splits(body, "heading_1", nest_by="heading_2")
    docapp_children_count = sum(
        1 for elem in body if "sectionBreak" not in elem
    )
    all_ranges = [
        r for node in _flatten_splits(nested) for r in node["ranges"]
    ]
    for lo, hi in all_ranges:
        assert 0 <= lo <= hi < docapp_children_count
    for (_, prev_hi), (curr_lo, _) in zip(all_ranges, all_ranges[1:]):
        assert curr_lo > prev_hi


# ---------------------------------------------------------------------
# Hypothesis property tests - _max_depth & _flatten_splits
# ---------------------------------------------------------------------

# Recursive SplitPoint strategy: title + optional icon + bounded ranges
# + bounded children. Depth typically ≤4 (max_leaves keeps trees tiny
# to shrink fast on failure).
_split_strategy = st.recursive(
    st.builds(
        lambda title, icon: _SplitPoint(
            title=title or "x",
            icon_emoji=icon,
            ranges=[(0, 0)],
            children=[],
        ),
        title=st.text(min_size=0, max_size=20),
        icon=st.one_of(st.none(), st.sampled_from(["\U0001f4d1", "*", "?"])),
    ),
    lambda children: st.builds(
        lambda title, icon, kids: _SplitPoint(
            title=title or "x",
            icon_emoji=icon,
            ranges=[(0, 0)],
            children=kids,
        ),
        title=st.text(min_size=0, max_size=20),
        icon=st.one_of(st.none(), st.sampled_from(["\U0001f4d1", "*", "?"])),
        kids=st.lists(children, max_size=3),
    ),
    max_leaves=8,
)


def _depth_via_count(splits: list[_SplitPoint]) -> int:
    """Independent reference implementation for cross-checking _max_depth."""
    if not splits:
        return -1
    best = 0
    for s in splits:
        child_depth = _depth_via_count(s["children"])
        if child_depth + 1 > best:
            best = child_depth + 1
    return best


@given(splits=st.lists(_split_strategy, max_size=4))
@settings(max_examples=50)
def test_property_max_depth_matches_independent_recursion(splits):
    """Property: ``_max_depth`` and an independent reference recursion
    agree on every shape.

    Cross-checks the production function against a re-derivation —
    catches: regression where one short-circuits early, regression
    where the empty-list sentinel (-1) shifts.
    """
    assert _max_depth(splits) == _depth_via_count(splits)


@given(splits=st.lists(_split_strategy, max_size=4))
@settings(max_examples=50)
def test_property_flatten_splits_preserves_node_count_and_parents_first(splits):
    """Property: ``_flatten_splits`` emits every node exactly once, and
    every parent appears before each of its children (pre-order). The
    transplant zips this flat list against ``add_tabs_to_doc``'s created
    tabs, so an ordering drift here would transplant sections into the
    WRONG tabs.
    """
    def count(forest: list) -> int:
        return sum(1 + count(s["children"]) for s in forest)

    flat = _flatten_splits(splits)
    assert len(flat) == count(splits)

    positions = {id(s): i for i, s in enumerate(flat)}

    def check_parents_first(forest: list) -> None:
        for node in forest:
            for child in node["children"]:
                assert positions[id(node)] < positions[id(child)]
            check_parents_first(node["children"])

    check_parents_first(splits)


# ---------------------------------------------------------------------
# N11 - tab-title de-dup helpers (native multi-tab source collision)
# ---------------------------------------------------------------------


def _sp(title: str, children: list | None = None) -> _SplitPoint:
    return _SplitPoint(
        title=title, icon_emoji=None, ranges=[(0, 0)], children=children or []
    )


def test_existing_tab_titles_collects_all_nesting_levels():
    tabs = [
        {
            "tabProperties": {"title": "A"},
            "childTabs": [{"tabProperties": {"title": "A1"}}],
        },
        {"tabProperties": {"title": "B"}},
        {"tabProperties": {}},  # an untitled tab contributes nothing
    ]
    assert _existing_tab_titles(tabs) == {"A", "A1", "B"}


def test_unique_tab_title_suffixes_only_on_collision():
    assert _unique_tab_title("Fresh", {"Taken"}) == "Fresh"
    assert _unique_tab_title("Dup", {"Dup"}) == "Dup (2)"
    assert _unique_tab_title("Dup", {"Dup", "Dup (2)"}) == "Dup (3)"


def test_unique_tab_title_suffix_stays_within_length_limit():
    base = "X" * _MAX_TAB_TITLE  # already at the API limit
    out = _unique_tab_title(base, {base})
    assert out != base
    assert len(out) <= _MAX_TAB_TITLE
    assert out.endswith(" (2)")


def test_dedupe_split_titles_makes_titles_unique_in_preorder():
    # Two shells collide with pre-existing tabs and the third collides
    # within the batch; in-place pre-order de-dup yields all-unique titles.
    splits = [_sp("Intro"), _sp("Methods"), _sp("Intro")]
    _dedupe_split_titles(splits, {"Intro", "Methods"})
    titles = [s["title"] for s in splits]
    assert titles == ["Intro (2)", "Methods (2)", "Intro (3)"]
    assert len(set(titles)) == 3
