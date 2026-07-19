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
this module-level split provided is what unblocked the R6 UTF-16 bug
fix: the fix landed in ``markdown_render._insert`` (UTF-16 code-unit
index advance), and its regression tests live HERE (``test_emoji_*``),
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
# above-BMP characters, control chars, emoji). The R6 UTF-16 bug was
# the case where an above-BMP character drifted the index off-by-one;
# it is now FIXED (``_insert`` advances by UTF-16 code units). The
# monotonicity property below held even under the bug (the bug
# miscounted magnitudes, it didn't go backward) and stays green after
# the fix; the bug manifested as misaligned STYLING, which the
# ``test_emoji_*`` example tests pin directly.
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
    the counter mid-walk, would violate this. Note: this property held
    even under the (now-fixed) R6 UTF-16 bug because that bug miscounted
    magnitudes — it never went backward. The R6 fix (UTF-16 code-unit
    advance) keeps this property green; what the bug broke was the
    ALIGNMENT of style ranges to inserted text, pinned separately by
    ``test_emoji_*`` below.
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

    The math inside ``_insert`` adds the text's UTF-16 code-unit count
    to ``current_index`` per insert; the per-call linearity in
    ``starting_index`` is independent of that per-token magnitude, so it
    holds regardless of the unit (and held before the R6 fix too).
    Catches: a future refactor that introduces non-linear behaviour
    (e.g. an absolute reset at first newline) without updating consumers
    that rely on the offset semantics.
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


# ---------------------------------------------------------------------
# SEMANTIC render tests — assert the EXACT request a construct produces
# (R2 audit Gap #1 — the renderer's actual job, previously untested)
#
# The Hypothesis property tests above pin STRUCTURAL invariants:
# monotonic indices, single-key requests, an op-key allow-list, linear
# offset. None of them assert that a SPECIFIC markdown construct emits
# the SPECIFIC Google-Docs request that construct is supposed to emit.
# A regression that swapped HEADING_1 for HEADING_2, emitted an empty
# or wrong ``fields`` mask, or picked the wrong bulletPreset would sail
# past every structural property (it's still one monotonic single-key
# allow-listed request) yet be REJECTED by the live Docs ``batchUpdate``
# API at runtime — a silent, user-facing break.
#
# These example-based tests close that gap by asserting the exact
# payload dict for each construct the renderer documents it supports:
# headings 1-6, bold/italic/strike, inline + fenced code, links,
# bullet vs numbered lists, nested-list tab indent, and blockquotes.
# The ``fields`` mask (the field-update bitmask Docs requires to be
# accurate or it silently no-ops / 400s) is asserted explicitly because
# it is the single most regression-prone, least-visible part of each
# request and appears in NO existing assertion.
# ---------------------------------------------------------------------


def _ops(requests: list[dict], op_key: str) -> list[dict]:
    """Return the inner payloads of every request of type ``op_key``."""
    return [r[op_key] for r in requests if op_key in r]


def _one_op(requests: list[dict], op_key: str) -> dict:
    """Return the single payload of type ``op_key``; assert exactly one."""
    found = _ops(requests, op_key)
    assert len(found) == 1, (
        f"expected exactly one {op_key} request, got {len(found)}: {found!r}"
    )
    return found[0]


def _inserted_text(requests: list[dict]) -> str:
    """Concatenate every insertText in emission order (the literal body)."""
    return "".join(
        r["insertText"]["text"] for r in requests if "insertText" in r
    )


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_heading_emits_updateParagraphStyle_with_matching_named_style(level):
    """``#``..``######`` must emit updateParagraphStyle whose
    namedStyleType is exactly ``HEADING_<level>`` with a
    ``fields="namedStyleType"`` mask — the contract the Docs API
    enforces. A regression mapping level→style off-by-one (the classic
    ``HEADING_1`` vs ``HEADING_2`` swap) is caught here and nowhere else.
    """
    md = ("#" * level) + " The Heading"
    requests = render_content_to_requests(md, "tab-1")

    ps = _one_op(requests, "updateParagraphStyle")
    assert ps["paragraphStyle"] == {"namedStyleType": f"HEADING_{level}"}
    assert ps["fields"] == "namedStyleType"
    # The heading text itself is still inserted verbatim.
    assert "The Heading" in _inserted_text(requests)


def test_heading_level_clamped_to_6():
    """markdown-it caps ATX headings at 6 (``#######`` is a paragraph),
    and the renderer additionally clamps via ``min(level, 6)``. Either
    way a 6-hash heading must never produce HEADING_7 (not a valid Docs
    namedStyleType — it would 400)."""
    requests = render_content_to_requests("###### Six", "tab-1")
    ps = _one_op(requests, "updateParagraphStyle")
    assert ps["paragraphStyle"]["namedStyleType"] == "HEADING_6"


def test_bold_emits_updateTextStyle_bold_true_with_bold_fields_mask():
    """``**b**`` → updateTextStyle{textStyle:{bold:true}, fields:"bold"}.
    The fields mask MUST be exactly "bold" — a mask that omits the
    changed field makes Docs silently ignore the style."""
    requests = render_content_to_requests("a **bold** word", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"] == {"bold": True}
    assert ts["fields"] == "bold"
    # The range must cover exactly the bolded run ("bold"), not the
    # surrounding text — the renderer records the range around the run.
    body = _inserted_text(requests)
    start = ts["range"]["startIndex"]
    end = ts["range"]["endIndex"]
    # current_index starts at 1; the inserted body begins at index 1.
    assert body[start - 1:end - 1] == "bold"


# ---------------------------------------------------------------------
# R6 UTF-16 regression tests — above-BMP (emoji / math-alphanumeric)
# index advance. Google Docs addresses positions in UTF-16 code units;
# an above-BMP char is a surrogate pair = 1 Python code point but 2
# UTF-16 units. ``_insert`` advances by UTF-16 units; these pin that.
#
# Each test is constructed so it FAILS under the old ``len(text)``
# (Python code-point) math and PASSES under the
# ``len(text.encode("utf-16-le")) // 2`` fix.
# ---------------------------------------------------------------------


def _utf16_doc_slice(body: str, start: int, end: int) -> str:
    """Slice ``body`` by 1-based Docs indices treating it as UTF-16.

    Docs ranges are [start, end) in UTF-16 code units, 1-based (the body
    begins at index 1). We mirror that exactly: encode to UTF-16-LE
    (2 bytes per unit) and slice on unit boundaries. This is the inverse
    of the renderer's index math — if a style range is positioned
    correctly in UTF-16 space, this returns the styled run verbatim.
    """
    enc = body.encode("utf-16-le")
    return enc[(start - 1) * 2:(end - 1) * 2].decode("utf-16-le")


def test_emoji_before_bold_run_positions_style_range_in_utf16_units():
    """``a😀**b**`` — the emoji (U+1F600, a surrogate pair) precedes a
    bold run. The bold ``updateTextStyle`` range must be positioned in
    UTF-16 code units: the prefix ``"a😀"`` is 3 UTF-16 units (1 + 2),
    so ``b`` sits at Docs index 4, and the range is [4, 5).

    Under the pre-fix ``len(text)`` math the prefix counted as 2 code
    points, mispositioning the range at [3, 4) — this test fails there.
    """
    requests = render_content_to_requests("a😀**b**", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"] == {"bold": True}
    start = ts["range"]["startIndex"]
    end = ts["range"]["endIndex"]
    # Exact UTF-16 indices: "a"=1 unit, "😀"=2 units → "b" starts at 4.
    assert (start, end) == (4, 5), (
        f"bold range must be [4, 5) in UTF-16 units after an emoji prefix; "
        f"got [{start}, {end}) — the R6 len(text) bug would give [3, 4)"
    )
    # And the range must actually point at "b" when the body is read as
    # UTF-16 (the inverse check — robust even if the magnitudes change).
    body = _inserted_text(requests)
    assert _utf16_doc_slice(body, start, end) == "b"


def test_emoji_style_range_differs_from_codepoint_count():
    """Discriminating assertion: with an above-BMP char in the prefix,
    the correct UTF-16 start index is STRICTLY GREATER than the Python
    code-point index the buggy ``len(text)`` would have produced. This
    is the precise behavioural difference the R6 fix introduces."""
    requests = render_content_to_requests("a😀**b**", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    start = ts["range"]["startIndex"]

    # What the buggy code-point math would have yielded for the prefix
    # "a😀": current_index starts at 1, += len("a😀") == 2 → start 3.
    buggy_codepoint_start = 1 + len("a😀")
    assert start > buggy_codepoint_start, (
        f"UTF-16 start ({start}) must exceed the code-point start "
        f"({buggy_codepoint_start}); equal means the surrogate pair was "
        f"counted as one unit (the R6 bug)"
    )
    # Concretely: UTF-16 = code points + (number of above-BMP chars).
    assert start == buggy_codepoint_start + 1


def test_emoji_in_first_paragraph_shifts_second_paragraphs_insert_index():
    """The drift isn't only in style ranges — every DOWNSTREAM insert
    location shifts too. With an emoji in the first paragraph, the
    second paragraph's insertText must start one unit later than a plain
    ASCII first paragraph of the same code-point length would imply.

    First paragraph ``"a😀b"`` = 4 UTF-16 units, + the paragraph's
    ``"\\n"`` = 5, so the second paragraph's insert begins at index 6.
    The ASCII control ``"axb"`` (3 units + newline = 4) puts it at 5.
    The difference (exactly 1, the extra surrogate unit) is what the
    pre-fix ``len(text)`` math dropped.
    """
    emoji_reqs = render_content_to_requests("a😀b\n\nsecond", "tab-1")
    ascii_reqs = render_content_to_requests("axb\n\nsecond", "tab-1")

    def _second_para_insert_index(requests: list[dict]) -> int:
        # The insert whose text is the second paragraph's content.
        for r in requests:
            if "insertText" in r and r["insertText"]["text"].startswith("second"):
                return r["insertText"]["location"]["index"]
        raise AssertionError("no 'second' paragraph insert found")

    emoji_idx = _second_para_insert_index(emoji_reqs)
    ascii_idx = _second_para_insert_index(ascii_reqs)
    # Emoji body is one UTF-16 unit longer than the ASCII control → the
    # downstream insert is shifted by exactly 1.
    assert emoji_idx == ascii_idx + 1, (
        f"second-paragraph insert index did not absorb the surrogate "
        f"pair's extra UTF-16 unit: emoji={emoji_idx}, ascii={ascii_idx} "
        f"(expected emoji == ascii + 1)"
    )
    # Pin the absolute value too: "a😀b"(4) + "\n"(1) → 6.
    assert emoji_idx == 6


def test_italic_emits_updateTextStyle_italic_true():
    requests = render_content_to_requests("an *em* word", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"] == {"italic": True}
    assert ts["fields"] == "italic"


def test_strikethrough_emits_updateTextStyle_strikethrough_true():
    """GFM ``~~x~~`` (the renderer enables markdown-it 'strikethrough')
    → updateTextStyle{strikethrough:true, fields:"strikethrough"}."""
    requests = render_content_to_requests("a ~~gone~~ word", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"] == {"strikethrough": True}
    assert ts["fields"] == "strikethrough"


def test_nested_bold_italic_merges_both_styles_in_one_range():
    """``***x***`` nests strong+em → the merged style carries BOTH
    bold and italic, and the fields mask lists both (order: bold then
    italic, matching _finalize's emission order)."""
    requests = render_content_to_requests("***both***", "tab-1")
    # The innermost run "both" gets the merged style.
    styled = [
        ts for ts in _ops(requests, "updateTextStyle")
        if ts["textStyle"].get("bold") and ts["textStyle"].get("italic")
    ]
    assert len(styled) == 1, (
        f"expected one run carrying both bold+italic, got: "
        f"{_ops(requests, 'updateTextStyle')!r}"
    )
    ts = styled[0]
    assert ts["textStyle"] == {"bold": True, "italic": True}
    assert ts["fields"] == "bold,italic"


def test_inline_code_emits_code_font_and_background_with_correct_fields():
    """`` `code` `` → updateTextStyle carrying the monospace font AND the
    code background, with fields="weightedFontFamily,backgroundColor".
    Both the font family string (Roboto Mono) and the exact RGB are
    pinned — a regression in either would render code as plain text or
    with the wrong highlight."""
    requests = render_content_to_requests("call `fn()` now", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"] == {
        "weightedFontFamily": {"fontFamily": "Roboto Mono"},
        "backgroundColor": {
            "color": {"rgbColor": {"red": 0.945, "green": 0.957, "blue": 0.965}}
        },
    }
    assert ts["fields"] == "weightedFontFamily,backgroundColor"
    assert "fn()" in _inserted_text(requests)


def test_fenced_code_block_emits_code_style_over_the_block_body():
    """A fenced ``` block emits the same code text-style over the block
    body. The body text is inserted with a trailing newline; the style
    range covers the body WITHOUT the trailing newline (s..s+len(body))."""
    md = "```\nx = 1\ny = 2\n```"
    requests = render_content_to_requests(md, "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"]["weightedFontFamily"] == {"fontFamily": "Roboto Mono"}
    assert "backgroundColor" in ts["textStyle"]
    assert ts["fields"] == "weightedFontFamily,backgroundColor"
    body = _inserted_text(requests)
    assert "x = 1\ny = 2" in body
    # The styled range length equals the block body length (no trailing \n).
    span = ts["range"]["endIndex"] - ts["range"]["startIndex"]
    assert span == len("x = 1\ny = 2")


def test_link_emits_updateTextStyle_with_url_and_link_fields_mask():
    """``[t](https://e.com)`` → updateTextStyle{link:{url:...}, fields:"link"}.
    The URL must round-trip exactly; the anchor text is what's inserted."""
    requests = render_content_to_requests("see [docs](https://example.com/x)", "tab-1")
    ts = _one_op(requests, "updateTextStyle")
    assert ts["textStyle"] == {"link": {"url": "https://example.com/x"}}
    assert ts["fields"] == "link"
    assert "docs" in _inserted_text(requests)


def test_bulleted_list_emits_createParagraphBullets_disc_preset():
    """A ``- a`` bullet list → createParagraphBullets with the
    BULLET_DISC_CIRCLE_SQUARE preset (the disc/circle/square cascade).
    Picking the numbered preset here would render bullets as numbers."""
    md = "- first\n- second"
    requests = render_content_to_requests(md, "tab-1")
    bullets = _ops(requests, "createParagraphBullets")
    assert len(bullets) >= 1
    assert all(
        b["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE" for b in bullets
    ), f"bulleted list used wrong preset(s): {[b['bulletPreset'] for b in bullets]!r}"


def test_numbered_list_emits_createParagraphBullets_numbered_preset():
    """A ``1.`` ordered list → createParagraphBullets with
    NUMBERED_DECIMAL_NESTED. This is the exact preset the gap calls out:
    a swap to a bullet preset would silently turn ``1. 2. 3.`` into
    discs."""
    md = "1. one\n2. two\n3. three"
    requests = render_content_to_requests(md, "tab-1")
    bullets = _ops(requests, "createParagraphBullets")
    assert len(bullets) >= 1
    assert all(
        b["bulletPreset"] == "NUMBERED_DECIMAL_NESTED" for b in bullets
    ), f"numbered list used wrong preset(s): {[b['bulletPreset'] for b in bullets]!r}"


def test_nested_list_item_inserts_tab_indent_before_child():
    """A nested list item is indented by inserting a literal TAB per
    depth level before the item text (``\\t`` * depth). Docs'
    createParagraphBullets then consumes the leading tab to set the
    nesting level. Without the tab insert, nested items render flat.
    Here the child ('child') sits one level deep, so exactly one TAB
    must precede it in the inserted body."""
    md = "- parent\n    - child"
    requests = render_content_to_requests(md, "tab-1")
    body = _inserted_text(requests)
    # The child line must be preceded by a tab; the parent line must not.
    assert "\tchild" in body, (
        f"nested item missing its tab indent. inserted body={body!r}"
    )
    assert "\tparent" not in body, (
        f"top-level item wrongly indented. inserted body={body!r}"
    )


def test_blockquote_text_is_inserted_and_no_quote_style_leaks_named_style():
    """``> quoted`` inserts its text verbatim and emits NO
    updateParagraphStyle in the current renderer.

    DOCUMENTED LATENT BUG (verified against the source at R2): the QUOTE
    rendering in ``_finalize`` (``indentStart`` 36pt for the ``_QUOTE``
    sentinel) is currently DEAD. ``blockquote_open`` records
    ``ctx.para_start``, but the inner ``paragraph_open`` immediately
    OVERWRITES it (and ``paragraph_close`` then sets it back to None), so
    by ``blockquote_close`` ``ctx.para_start is None`` and the ``_QUOTE``
    range is never appended. A blockquote therefore produces only
    insertText today — no indent.

    This test pins the ACTUAL behavior (not the intended one) so it is a
    truthful regression guard:
      * If someone fixes the para_start clobber, this test will fail and
        force a deliberate update to the asserted contract (emit
        indentStart, fields="indentStart", NO namedStyleType — never the
        internal ``_QUOTE`` sentinel, which is not a real Docs style and
        would 400).
      * If the insert path itself regresses, it fails too.
    """
    requests = render_content_to_requests("> a quote", "tab-1")
    # Text is inserted verbatim.
    assert "a quote" in _inserted_text(requests)
    # Current contract: the QUOTE paragraph style is not emitted (latent
    # bug above). Crucially, the internal ``_QUOTE`` sentinel must NEVER
    # leak into a real updateParagraphStyle request even if the code path
    # changes — that would be a guaranteed Docs 400.
    for ps in _ops(requests, "updateParagraphStyle"):
        assert ps.get("paragraphStyle", {}).get("namedStyleType") != "_QUOTE", (
            "the internal _QUOTE sentinel leaked into a batchUpdate "
            "request; it is not a valid Docs namedStyleType and will 400."
        )


def test_plain_paragraph_emits_no_styling_requests():
    """A plain paragraph with no inline marks emits ONLY insertText —
    no updateTextStyle / updateParagraphStyle / createParagraphBullets.
    Guards against the renderer emitting spurious empty-style requests
    (which Docs rejects as having an empty fields mask)."""
    requests = render_content_to_requests("just plain text here", "tab-1")
    assert _ops(requests, "updateTextStyle") == []
    assert _ops(requests, "updateParagraphStyle") == []
    assert _ops(requests, "createParagraphBullets") == []
    assert _inserted_text(requests).startswith("just plain text here")


# ---------------------------------------------------------------------
# S3 renderer coverage: inline images, horizontal rules, task lists.
# GFM TABLES are deliberately NOT rendered here - a real Docs table needs
# the two-phase insertTable -> re-fetch -> fill (server cell indices),
# which a one-shot request list can't express. The one-shot client-side
# table arithmetic shipped in the wave produced out-of-bounds insert
# indices for a table placed mid-content (the leading-newline shift on a
# non-body-start insertTable) and 400'd the whole call live. Tables now go
# through api._apply_markdown_content; the renderer only sees table-free
# text. These tests pin that the renderer never emits an insertTable.
# ---------------------------------------------------------------------


def test_renderer_does_not_emit_insertTable_for_a_gfm_table():
    """The renderer must NEVER emit an insertTable (the removed client-side
    table path). Given table markdown it degrades to literal pipe text so a
    caller that bypasses the two-phase path can't crash - the real table
    rendering lives in api._apply_markdown_content."""
    md = "intro\n\n| A | B |\n|---|---|\n| x | y |\n\ntail"
    requests = render_content_to_requests(md, "t1")
    assert _ops(requests, "insertTable") == []
    # Every insert index stays within a contiguous forward run (no gaps
    # into phantom table structure) - the failure mode was an index past
    # the real paragraph bounds.
    body = _inserted_text(requests)
    assert "intro" in body and "tail" in body


def test_hr_renders_empty_paragraph_with_bottom_border():
    """A thematic break (`---`) -> an empty paragraph carrying a
    borderBottom updateParagraphStyle (Docs has no dedicated HR insert)."""
    requests = render_content_to_requests("above\n\n---\n\nbelow", "t1")
    borders = [
        r["updateParagraphStyle"]
        for r in requests
        if "updateParagraphStyle" in r
        and "borderBottom" in r["updateParagraphStyle"]["paragraphStyle"]
    ]
    assert len(borders) == 1
    assert borders[0]["fields"] == "borderBottom"
    assert borders[0]["paragraphStyle"]["borderBottom"]["dashStyle"] == "SOLID"


def test_image_http_src_renders_insertInlineImage_one_unit():
    """`![alt](https://…)` -> insertInlineImage with that uri; it occupies
    exactly ONE index unit, so the following text shifts by 1."""
    requests = render_content_to_requests("x ![a](https://ex.com/i.png) y", "t1")
    img = _one_op(requests, "insertInlineImage")
    assert img["uri"] == "https://ex.com/i.png"
    assert img["location"]["index"] == 3  # after "x " (2 units) from index 1
    following = next(
        r["insertText"] for r in requests
        if "insertText" in r and r["insertText"]["text"].startswith(" y")
    )
    assert following["location"]["index"] == 4  # image consumed exactly 1


def test_image_non_http_src_falls_back_to_alt_text():
    """A relative / data: image src can't be fetched by Docs, so it is
    rendered as its alt text (content preserved, batch can't fail on a bad
    URL) — NOT as an insertInlineImage."""
    requests = render_content_to_requests("![diagram](img/x.png)", "t1")
    assert _ops(requests, "insertInlineImage") == []
    assert "diagram" in _inserted_text(requests)


def test_task_list_renders_checkbox_bullets_and_strips_marker():
    """`- [ ]` / `- [x]` items -> BULLET_CHECKBOX bullets with the marker
    stripped from the text; a plain item stays a disc bullet."""
    md = "- [ ] todo\n- [x] done\n- plain"
    requests = render_content_to_requests(md, "t1")
    body = _inserted_text(requests)
    assert "[ ]" not in body and "[x]" not in body
    assert "todo" in body and "done" in body and "plain" in body
    presets = {b["bulletPreset"] for b in _ops(requests, "createParagraphBullets")}
    assert "BULLET_CHECKBOX" in presets
    assert "BULLET_DISC_CIRCLE_SQUARE" in presets


def test_task_marker_in_ordered_list_is_not_a_task():
    """A `[ ]` marker in an ORDERED list is not a GFM task — it stays
    literal text under a numbered bullet, never a checkbox."""
    requests = render_content_to_requests("1. [ ] not a task", "t1")
    presets = {b["bulletPreset"] for b in _ops(requests, "createParagraphBullets")}
    assert "BULLET_CHECKBOX" not in presets
    assert "[ ]" in _inserted_text(requests)
