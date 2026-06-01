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

import pytest

from appscriptly.services.docs.markdown_render import (
    _add_tab_request,
    _is_table_separator,
    _plain_text_requests,
    _rename_tab_request,
    _split_table_row,
    _tab_properties,
    parse_markdown_table,
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


# ---------------------------------------------------------------------
# Hypothesis property tests — render_content_to_requests
# (Round 1 prediction / R5 audit Gap #6)
#
# Test architect Round 1: "A hypothesis-based property test on
# docs_api.render_markdown would catch entire bug classes including
# R15 F7/F8 UTF-16 — for ~30 LOC."
#
# Pre-R14-#8-split the renderer was inlined in a 1,050-LOC api.py
# that pulled in Google SDK at import time, making strategy-driven
# tests prohibitive. PR #109 split it out as a pure module; this
# section cashes the prediction.
#
# The 5 properties below pin the renderer's contract:
#   1. Empty / whitespace-only inputs always return [] (idempotent base).
#   2. Insert indices are non-decreasing across the request list
#      (the load-bearing invariant for stable position math).
#   3. Every request targets the SAME tab_id the caller passed in
#      (no cross-tab leakage; protects against a future refactor that
#      threads tab_id through a shared ctx incorrectly).
#   4. ``starting_index`` offset shifts every insert's index by the
#      exact delta (linearity — the per-token index math must not
#      depend on starting_index itself).
#   5. Every request has exactly one of the documented operation keys
#      (insertText / updateTextStyle / updateParagraphStyle /
#      createParagraphBullets / deleteParagraphBullets) — catches a
#      future renderer adding an undocumented request type without
#      updating the consumer code in api.py.
#
# Strategy: ``st.text()`` generates arbitrary unicode (including
# above-BMP characters, control chars, emoji). The R6 UTF-16 bug is
# specifically the case where an above-BMP character drifts the
# index off-by-one; once the source is fixed, the monotonicity
# property pins the fix permanently. For now, this test passes
# because monotonicity holds even with the wrong unit — the bug
# manifests as misaligned styling, not as monotonicity violation.
# Test architect noted this same nuance in Round 1.
# ---------------------------------------------------------------------
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# Bounded text strategy. min_size=0 allows the empty-string case;
# max_size=200 keeps generated inputs fast. Hypothesis automatically
# shrinks to minimal failing examples on assertion failures.
_md_strategy = st.text(min_size=0, max_size=200)

# Known operation keys the renderer is allowed to emit. Any new key
# must be added here AND consumed appropriately in api.py's
# batchUpdate call site — the property test forces that paired update.
_ALLOWED_REQUEST_KEYS = frozenset({
    "insertText",
    "updateTextStyle",
    "updateParagraphStyle",
    "createParagraphBullets",
    "deleteParagraphBullets",
})


@given(content=st.one_of(st.just(""), st.text(alphabet=" \t\n\r", min_size=1, max_size=10)))
def test_property_render_empty_or_whitespace_returns_empty_list(content):
    """Property: empty string AND any string that .strip()s to empty
    yields ``[]`` — no requests, no errors."""
    requests = render_content_to_requests(content, "tab-1")
    assert requests == []


@given(content=_md_strategy)
def test_property_render_insert_indices_are_non_decreasing(content):
    """Property: across the insert-request stream, ``location.index``
    is monotonically non-decreasing.

    The renderer's index math threads through ``ctx.current_index``;
    a future refactor that re-orders inserts, or a bug that decrements
    the counter mid-walk, would violate this. Note: this property
    currently HOLDS even under the R6 UTF-16 bug because the bug
    miscounts magnitudes — it doesn't go backward. The R6 fix will
    keep this property green; only the ALIGNMENT of style ranges to
    inserted text was wrong.
    """
    requests = render_content_to_requests(content, "tab-1")
    insert_indices = [
        r["insertText"]["location"]["index"]
        for r in requests
        if "insertText" in r
    ]
    for prev, curr in zip(insert_indices, insert_indices[1:]):
        assert curr >= prev, (
            f"insertText index regression: {prev} -> {curr} for "
            f"content={content!r}"
        )


@given(content=_md_strategy, tab_id=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=30))
def test_property_render_every_request_targets_supplied_tab_id(content, tab_id):
    """Property: every emitted request — insertText OR formatting —
    carries ``tab_id`` equal to what the caller passed in.

    insertText carries it inside ``location.tabId``; formatting
    requests carry it inside the range dict's ``tabId`` field.
    Catches: a future refactor that drops tab_id from ``_loc``/``_range``
    helpers (e.g. shared global tab_id leaking from a different render
    call in the same process).
    """
    requests = render_content_to_requests(content, tab_id)
    for r in requests:
        # Each request is single-key (property #5 below pins that);
        # find the tab_id wherever the request type expects it.
        if "insertText" in r:
            assert r["insertText"]["location"]["tabId"] == tab_id
        else:
            # Formatting requests put tab_id inside their range dict.
            # The exact key varies (range / paragraphRange / etc.), so
            # walk the payload looking for any ``tabId`` field — and
            # assert every one matches.
            tab_ids_found = _all_tab_ids(r)
            assert tab_ids_found, (
                f"format request has no tabId anywhere in payload: {r!r}"
            )
            assert all(tid == tab_id for tid in tab_ids_found), (
                f"format request has wrong tabId(s): expected {tab_id!r}, "
                f"got {tab_ids_found!r} in {r!r}"
            )


def _all_tab_ids(obj) -> list[str]:
    """Walk a nested dict, collecting every ``tabId`` field value."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "tabId" and isinstance(v, str):
                out.append(v)
            else:
                out.extend(_all_tab_ids(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_all_tab_ids(item))
    return out


@given(content=_md_strategy, offset=st.integers(min_value=0, max_value=1000))
def test_property_render_starting_index_offset_is_linear(content, offset):
    """Property: increasing ``starting_index`` by N shifts every
    insertText location by exactly N.

    The math inside ``_insert`` adds ``len(text)`` to ``current_index``
    per insert; the per-call linearity in ``starting_index`` follows
    from that. Catches: a future refactor that introduces non-linear
    behaviour (e.g. an absolute reset at first newline) without
    updating consumers that rely on the offset semantics.
    """
    baseline = render_content_to_requests(content, "tab-1", starting_index=1)
    shifted = render_content_to_requests(content, "tab-1", starting_index=1 + offset)

    # Same number of requests in same positions.
    assert len(baseline) == len(shifted)

    baseline_inserts = [
        r["insertText"]["location"]["index"]
        for r in baseline if "insertText" in r
    ]
    shifted_inserts = [
        r["insertText"]["location"]["index"]
        for r in shifted if "insertText" in r
    ]
    # Every insert index shifted by exactly ``offset``.
    assert len(baseline_inserts) == len(shifted_inserts)
    for b, s in zip(baseline_inserts, shifted_inserts):
        assert s - b == offset, (
            f"non-linear offset: baseline index {b}, shifted index {s}, "
            f"expected delta {offset}; content={content!r}"
        )


@given(content=_md_strategy)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_render_every_request_has_exactly_one_allowed_op_key(content):
    """Property: every request dict has exactly ONE top-level key,
    and that key is in the documented allow-list.

    The Google Docs batchUpdate API uses the top-level key to dispatch
    the request type; a request with two keys is ambiguous AND a
    request with an unknown key fails the API call. Catches: a future
    renderer accidentally emitting a payload with TWO ops (e.g. via a
    typo'd dict-merge), AND an undocumented op type creeping in
    without updating the consumer side in api.py.
    """
    requests = render_content_to_requests(content, "tab-1")
    for r in requests:
        keys = set(r.keys())
        assert len(keys) == 1, (
            f"request has {len(keys)} keys, expected exactly 1: {r!r}"
        )
        op_key = next(iter(keys))
        assert op_key in _ALLOWED_REQUEST_KEYS, (
            f"request uses undocumented op key {op_key!r}; "
            f"allowed: {sorted(_ALLOWED_REQUEST_KEYS)}; full request={r!r}"
        )


# ---------------------------------------------------------------------
# parse_markdown_table — pure GFM-table parser
# ---------------------------------------------------------------------


def test_split_table_row_strips_pipes_and_whitespace():
    assert _split_table_row("| a | b | c |") == ["a", "b", "c"]
    assert _split_table_row("a|b|c") == ["a", "b", "c"]


def test_split_table_row_handles_escaped_pipe():
    assert _split_table_row(r"| a\|x | b |") == ["a|x", "b"]


def test_is_table_separator_recognizes_alignment_markers():
    assert _is_table_separator("|---|---|") is True
    assert _is_table_separator("| :--- | :---: | ---: |") is True
    assert _is_table_separator("| a | b |") is False
    assert _is_table_separator("|   |---|") is False  # empty cell


def test_parse_markdown_table_basic():
    md = "| Name | Qty |\n|------|-----|\n| Widget | 3 |\n| Gadget | 5 |"
    result = parse_markdown_table(md)
    assert result["rows"] == 3  # header + 2 body
    assert result["columns"] == 2
    assert result["cells"] == [
        ["Name", "Qty"],
        ["Widget", "3"],
        ["Gadget", "5"],
    ]


def test_parse_markdown_table_pads_short_and_truncates_long_rows():
    md = "| A | B | C |\n|---|---|---|\n| 1 |\n| x | y | z | extra |"
    result = parse_markdown_table(md)
    assert result["columns"] == 3
    assert result["cells"][1] == ["1", "", ""]      # padded
    assert result["cells"][2] == ["x", "y", "z"]    # truncated


def test_parse_markdown_table_ignores_blank_lines():
    md = "\n\n| H |\n|---|\n| v |\n\n"
    result = parse_markdown_table(md)
    assert result["rows"] == 2
    assert result["columns"] == 1


def test_parse_markdown_table_header_only_is_valid():
    md = "| H1 | H2 |\n|----|----|"
    result = parse_markdown_table(md)
    assert result["rows"] == 1
    assert result["cells"] == [["H1", "H2"]]


def test_parse_markdown_table_rejects_missing_separator():
    with pytest.raises(ValueError, match="separator row"):
        parse_markdown_table("| a | b |\n| c | d |")


def test_parse_markdown_table_rejects_single_line():
    with pytest.raises(ValueError, match="at least a header"):
        parse_markdown_table("| just a header |")
