"""Co-located tests for services/docs/content_transplant.py.

The transplant is the REST replacement for the Apps Script /exec step
of ``gdocs_tab_existing_doc`` (see the module docstring and
``_audit/2026-07-08-tabs-architecture-decision.md``). Coverage here:

1. Planner element coverage - one docs-JSON fixture per element type
   asserting the emitted request program (the read-shape to
   write-shape passthrough, whitelist filtering, and the explicit
   detect-and-warn handling of the non-representable tail).
2. Fidelity preflight - every dropped/degraded kind fires its counter;
   nothing is skipped silently.
3. Hypothesis property tests (repo convention, same family as
   test_markdown_render.py): text reconstruction, UTF-16 offset
   discipline, tabId discipline, rebase roundtrip.
4. Executor tests against a scripted fake Docs service: rebasing,
   chunking, the table sync-point flow, verification, and the
   carve-source-last helper.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from appscriptly.services.docs import content_transplant as ct
from appscriptly.services.docs.content_transplant import (
    DEGRADED_KINDS,
    DROPPED_KINDS,
    FidelityReport,
    SegmentPhase,
    TablePhase,
    _rebase_requests,
    carve_source_ranges,
    execute_tab_transplant,
    plan_tab_transplant,
    scan_source_fidelity,
    verify_tab_transplant,
)

TAB = "t.dest"


# ---------------------------------------------------------------------
# Fixture builders - the docs-JSON shapes documents.get returns
# ---------------------------------------------------------------------


def _para(text: str, style: str | None = None, text_style: dict | None = None) -> dict:
    p: dict = {"elements": [{"textRun": {"content": text}}]}
    if text_style:
        p["elements"][0]["textRun"]["textStyle"] = text_style
    if style:
        p["paragraphStyle"] = {"namedStyleType": style}
    return {"paragraph": p}


def _bullet_para(text: str, list_id: str, nesting: int = 0) -> dict:
    elem = _para(text)
    elem["paragraph"]["bullet"] = {"listId": list_id, "nestingLevel": nesting}
    if nesting == 0:
        del elem["paragraph"]["bullet"]["nestingLevel"]
        elem["paragraph"]["bullet"]["listId"] = list_id
    return elem


def _lists(list_id: str, glyph_type: str | None, glyph_format: str = "") -> dict:
    level: dict = {"glyphFormat": glyph_format}
    if glyph_type:
        level["glyphType"] = glyph_type
    return {list_id: {"listProperties": {"nestingLevels": [level]}}}


def _image_para(object_id: str) -> dict:
    return {
        "paragraph": {
            "elements": [
                {"inlineObjectElement": {"inlineObjectId": object_id}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }


def _inline_objects(object_id: str, *, uri: str | None, drawing: bool = False) -> dict:
    embedded: dict = {}
    if drawing:
        embedded["embeddedDrawingProperties"] = {}
    if uri is not None:
        embedded["imageProperties"] = {"contentUri": uri}
        embedded["size"] = {
            "width": {"magnitude": 200, "unit": "PT"},
            "height": {"magnitude": 100, "unit": "PT"},
        }
    return {object_id: {"inlineObjectProperties": {"embeddedObject": embedded}}}


def _table(rows: list[list[list[dict]]], columns: int | None = None) -> dict:
    """rows = [[cell_content, ...], ...] where cell_content is a list of
    structural elements (the cell's ``content`` list)."""
    table_rows = [
        {"tableCells": [{"content": cell} for cell in row]} for row in rows
    ]
    n_cols = columns if columns is not None else max(len(r) for r in rows)
    return {"table": {"tableRows": table_rows, "columns": n_cols}}


def _plan(elements, *, lists=None, inline_objects=None, report=None, **kwargs):
    return plan_tab_transplant(
        elements,
        lists=lists or {},
        inline_objects=inline_objects or {},
        dest_tab_id=TAB,
        report=report,
        **kwargs,
    )


def _segment_requests(plan) -> list[dict]:
    out: list[dict] = []
    for phase in plan.phases:
        if isinstance(phase, SegmentPhase):
            out.extend(phase.requests)
    return out


def _inserted_text(plan) -> str:
    return "".join(
        r["insertText"]["text"]
        for r in _segment_requests(plan)
        if "insertText" in r
    )


def _requests_of_kind(plan, kind: str) -> list[dict]:
    return [r[kind] for r in _segment_requests(plan) if kind in r]


# ---------------------------------------------------------------------
# Planner - paragraphs, runs, styles
# ---------------------------------------------------------------------


def test_plain_paragraph_roundtrips_text_and_ends_with_newline():
    plan = _plan([_para("Hello world\n")])
    assert _inserted_text(plan) == "Hello world\n"
    assert plan.block_count == 1


def test_paragraph_missing_source_newline_gets_one():
    plan = _plan([_para("no newline run")])
    assert _inserted_text(plan) == "no newline run\n"


def test_empty_paragraph_still_lands_as_paragraph_mark():
    plan = _plan([{"paragraph": {"elements": []}}])
    assert _inserted_text(plan) == "\n"
    assert plan.block_count == 1


def test_run_text_style_passthrough_with_fields_mask():
    plan = _plan(
        [_para("styled\n", text_style={"bold": True, "italic": True})]
    )
    (style_req,) = _requests_of_kind(plan, "updateTextStyle")
    assert style_req["textStyle"] == {"bold": True, "italic": True}
    assert style_req["fields"] == "bold,italic"
    assert style_req["range"] == {"startIndex": 0, "endIndex": 7, "tabId": TAB}


def test_readonly_and_unknown_text_style_fields_are_stripped():
    plan = _plan(
        [
            _para(
                "x\n",
                text_style={
                    "bold": True,
                    "suggestedInsertionIds": ["s1"],
                    "someFutureField": {"a": 1},
                },
            )
        ]
    )
    (style_req,) = _requests_of_kind(plan, "updateTextStyle")
    assert style_req["fields"] == "bold"
    assert "someFutureField" not in style_req["textStyle"]


def test_heading_paragraph_style_carried():
    plan = _plan([_para("Title\n", style="HEADING_2")])
    (para_req,) = _requests_of_kind(plan, "updateParagraphStyle")
    assert para_req["paragraphStyle"] == {"namedStyleType": "HEADING_2"}
    assert para_req["range"]["tabId"] == TAB


def test_default_normal_text_only_style_emits_no_request():
    plan = _plan([_para("plain\n", style="NORMAL_TEXT")])
    assert _requests_of_kind(plan, "updateParagraphStyle") == []


def test_paragraph_style_headingId_stripped():
    elem = _para("h\n", style="HEADING_1")
    elem["paragraph"]["paragraphStyle"]["headingId"] = "h.abc123"
    plan = _plan([elem])
    (para_req,) = _requests_of_kind(plan, "updateParagraphStyle")
    assert "headingId" not in para_req["paragraphStyle"]
    assert "headingId" not in para_req["fields"]


def test_url_link_kept_internal_link_dropped_with_note():
    report = FidelityReport()
    plan = _plan(
        [
            _para("out\n", text_style={"link": {"url": "https://x.example"}}),
            _para("in\n", text_style={"link": {"headingId": "h.target"}}),
        ],
        report=report,
    )
    style_reqs = _requests_of_kind(plan, "updateTextStyle")
    assert len(style_reqs) == 1
    assert style_reqs[0]["textStyle"]["link"] == {"url": "https://x.example"}
    assert report.counts["internal_link"] == 1


# ---------------------------------------------------------------------
# Planner - inline objects, breaks, chips, the dropped tail
# ---------------------------------------------------------------------


def test_image_reinserted_via_content_uri_with_size():
    plan = _plan(
        [_image_para("kix.img1")],
        inline_objects=_inline_objects("kix.img1", uri="https://lh3.example/img"),
    )
    (img_req,) = _requests_of_kind(plan, "insertInlineImage")
    assert img_req["uri"] == "https://lh3.example/img"
    assert img_req["objectSize"]["width"]["magnitude"] == 200
    assert img_req["location"] == {"index": 0, "tabId": TAB}
    # The image occupies exactly 1 index unit: the paragraph newline
    # (from the source's own "\n" run) lands at offset 1.
    text_reqs = _requests_of_kind(plan, "insertText")
    assert text_reqs[0]["location"]["index"] == 1


def test_drawing_detected_and_skipped_never_silent():
    report = FidelityReport()
    plan = _plan(
        [_image_para("kix.draw")],
        inline_objects=_inline_objects("kix.draw", uri=None, drawing=True),
        report=report,
    )
    assert _requests_of_kind(plan, "insertInlineImage") == []
    assert report.counts["drawing"] == 1
    # The paragraph itself still lands (as its newline).
    assert _inserted_text(plan) == "\n"


def test_object_without_content_uri_warned_as_unsupported():
    report = FidelityReport()
    _plan(
        [_image_para("kix.mystery")],
        inline_objects={"kix.mystery": {"inlineObjectProperties": {"embeddedObject": {}}}},
        report=report,
    )
    assert report.counts["unsupported_object"] == 1


def test_page_break_reemitted_as_one_unit():
    elem = {
        "paragraph": {
            "elements": [
                {"pageBreak": {}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }
    plan = _plan([elem])
    (pb_req,) = _requests_of_kind(plan, "insertPageBreak")
    assert pb_req["location"] == {"index": 0, "tabId": TAB}
    (phase,) = [p for p in plan.phases if isinstance(p, SegmentPhase)]
    assert phase.length == 2  # page break unit + newline


def test_horizontal_rule_becomes_bottom_border_paragraph():
    report = FidelityReport()
    elem = {
        "paragraph": {
            "elements": [
                {"horizontalRule": {}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }
    plan = _plan([elem], report=report)
    (para_req,) = _requests_of_kind(plan, "updateParagraphStyle")
    assert "borderBottom" in para_req["paragraphStyle"]
    assert report.counts["horizontal_rule"] == 1


def test_person_chip_reinserted_by_email():
    elem = {
        "paragraph": {
            "elements": [
                {"person": {"personProperties": {"email": "a@ex.com", "name": "A"}}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }
    plan = _plan([elem])
    (person_req,) = _requests_of_kind(plan, "insertPerson")
    # name is output-only on write; only email travels.
    assert person_req["personProperties"] == {"email": "a@ex.com"}


def test_person_chip_without_email_falls_back_to_name_text():
    elem = {
        "paragraph": {
            "elements": [
                {"person": {"personProperties": {"name": "Just A Name"}}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }
    plan = _plan([elem])
    assert _requests_of_kind(plan, "insertPerson") == []
    assert _inserted_text(plan) == "Just A Name\n"


def test_rich_link_chip_reinserted_by_uri():
    elem = {
        "paragraph": {
            "elements": [
                {"richLink": {"richLinkProperties": {"uri": "https://docs.google.com/x"}}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }
    plan = _plan([elem])
    (rl_req,) = _requests_of_kind(plan, "insertRichLink")
    assert rl_req["richLinkProperties"] == {"uri": "https://docs.google.com/x"}


def test_dropped_tail_detected_equation_footnote_autotext_columnbreak_date():
    report = FidelityReport()
    elem = {
        "paragraph": {
            "elements": [
                {"equation": {}},
                {"footnoteReference": {"footnoteId": "f1"}},
                {"autoText": {"type": "PAGE_NUMBER"}},
                {"columnBreak": {}},
                {"dateElement": {}},
                {"textRun": {"content": "\n"}},
            ]
        }
    }
    _plan([elem], report=report)
    for kind in ("equation", "footnote", "auto_text", "column_break", "date_chip"):
        assert report.counts[kind] == 1, kind


def test_unknown_inline_element_counted_not_silent():
    report = FidelityReport()
    elem = {"paragraph": {"elements": [{"futureThing": {}}, {"textRun": {"content": "\n"}}]}}
    _plan([elem], report=report)
    assert report.counts["unknown_inline"] == 1


def test_positioned_objects_counted():
    report = FidelityReport()
    elem = _para("has floats\n")
    elem["paragraph"]["positionedObjectIds"] = ["kix.p1", "kix.p2"]
    _plan([elem], report=report)
    assert report.counts["positioned_object"] == 2


def test_toc_becomes_placeholder_line_with_note():
    report = FidelityReport()
    plan = _plan([{"tableOfContents": {"content": []}}], report=report)
    assert "Table of contents omitted" in _inserted_text(plan)
    assert report.counts["toc"] == 1
    assert plan.block_count == 1


# ---------------------------------------------------------------------
# Planner - lists
# ---------------------------------------------------------------------


def test_bullet_list_nesting_travels_as_leading_tabs():
    lists = _lists("kix.l1", None)
    plan = _plan(
        [
            _bullet_para("top\n", "kix.l1", nesting=0),
            _bullet_para("nested\n", "kix.l1", nesting=2),
        ],
        lists=lists,
    )
    assert _inserted_text(plan) == "top\n\t\tnested\n"
    bullets = _requests_of_kind(plan, "createParagraphBullets")
    assert bullets, "expected createParagraphBullets requests"
    assert all(b["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE" for b in bullets)


def test_ordered_glyphs_map_to_numbered_presets():
    assert ct._bullet_preset_for("L", _lists("L", "DECIMAL")).startswith("NUMBERED")
    assert (
        ct._bullet_preset_for("L", _lists("L", "DECIMAL", glyph_format="%0)"))
        == "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS"
    )
    assert ct._bullet_preset_for("L", _lists("L", None)) == "BULLET_DISC_CIRCLE_SQUARE"
    assert ct._bullet_preset_for(None, {}) == "BULLET_DISC_CIRCLE_SQUARE"


def test_contiguous_same_preset_items_merge_into_one_bullets_request():
    lists = _lists("kix.l1", None)
    plan = _plan(
        [_bullet_para("a\n", "kix.l1"), _bullet_para("b\n", "kix.l1")],
        lists=lists,
    )
    bullets = _requests_of_kind(plan, "createParagraphBullets")
    assert len(bullets) == 1
    assert bullets[0]["range"]["startIndex"] == 0


def test_bullets_apply_bottom_up_after_all_inserts():
    lists = _lists("kix.l1", None)
    plan = _plan(
        [
            _bullet_para("a\n", "kix.l1", nesting=0),
            _bullet_para("b\n", "kix.l1", nesting=1),
            _para("interrupt\n"),
            _bullet_para("c\n", "kix.l1", nesting=0),
        ],
        lists=lists,
    )
    reqs = _segment_requests(plan)
    kinds = [next(iter(r)) for r in reqs]
    first_bullet = kinds.index("createParagraphBullets")
    assert "insertText" not in kinds[first_bullet:]
    bullet_starts = [
        r["createParagraphBullets"]["range"]["startIndex"]
        for r in reqs
        if "createParagraphBullets" in r
    ]
    assert bullet_starts == sorted(bullet_starts, reverse=True)


def test_interrupted_numbered_list_gets_restart_note():
    report = FidelityReport()
    lists = _lists("kix.num", "DECIMAL")
    _plan(
        [
            _bullet_para("one\n", "kix.num"),
            _para("break\n"),
            _bullet_para("two\n", "kix.num"),
        ],
        lists=lists,
        report=report,
    )
    assert report.counts["list_numbering_restart"] == 1


def test_uninterrupted_numbered_list_has_no_restart_note():
    report = FidelityReport()
    lists = _lists("kix.num", "DECIMAL")
    _plan(
        [_bullet_para("one\n", "kix.num"), _bullet_para("two\n", "kix.num")],
        lists=lists,
        report=report,
    )
    assert "list_numbering_restart" not in report.counts


# ---------------------------------------------------------------------
# Planner - tables
# ---------------------------------------------------------------------


def test_table_becomes_table_phase_with_cell_plans():
    table = _table([[[_para("A\n")], [_para("B\n")]], [[_para("C\n")], []]])
    plan = _plan([_para("before\n"), table, _para("after\n")])
    kinds = [type(p).__name__ for p in plan.phases]
    assert kinds == ["SegmentPhase", "TablePhase", "SegmentPhase"]
    phase = plan.phases[1]
    assert isinstance(phase, TablePhase)
    assert (phase.rows, phase.columns) == (2, 2)
    assert set(phase.cell_plans) == {(0, 0), (0, 1), (1, 0)}
    assert plan.block_count == 3


def test_empty_source_cell_is_not_filled():
    # A fresh created cell already holds one empty paragraph; planning
    # an empty source cell would add a stray blank line.
    table = _table([[[_para("\n")], [_para("real\n")]]])
    plan = _plan([table])
    phase = plan.phases[0]
    assert isinstance(phase, TablePhase)
    assert set(phase.cell_plans) == {(0, 1)}


def test_cell_final_paragraph_suppresses_trailing_newline():
    table = _table([[[_para("first\n"), _para("last\n")]]])
    plan = _plan([table])
    phase = plan.phases[0]
    assert isinstance(phase, TablePhase)
    cell_plan = phase.cell_plans[(0, 0)]
    assert _inserted_text(cell_plan) == "first\nlast"


def test_cell_styles_whitelisted_and_spans_become_merges():
    table = _table([[[_para("head\n")], []], [[], []]])
    head_cell = table["table"]["tableRows"][0]["tableCells"][0]
    head_cell["tableCellStyle"] = {
        "backgroundColor": {"color": {"rgbColor": {"red": 1}}},
        "rowSpan": 2,
        "columnSpan": 1,
    }
    report = FidelityReport()
    plan = _plan([table], report=report)
    phase = plan.phases[0]
    assert isinstance(phase, TablePhase)
    ((r, c, style),) = phase.cell_styles
    assert (r, c) == (0, 0)
    assert "rowSpan" not in style and "backgroundColor" in style
    assert phase.merges == [(0, 0, 2, 1)]
    assert report.counts["merged_cells"] == 1


def test_pinned_header_rows_and_column_widths_carried():
    table = _table([[[_para("h\n")]], [[_para("b\n")]]])
    table["table"]["tableRows"][0]["tableRowStyle"] = {
        "tableHeader": True,
        "minRowHeight": {"magnitude": 20, "unit": "PT"},
    }
    table["table"]["tableStyle"] = {
        "tableColumnProperties": [
            {"widthType": "FIXED_WIDTH", "width": {"magnitude": 120, "unit": "PT"}}
        ]
    }
    plan = _plan([table])
    phase = plan.phases[0]
    assert isinstance(phase, TablePhase)
    assert phase.pinned_header_rows == 1
    assert phase.row_styles == [(0, {"minRowHeight": {"magnitude": 20, "unit": "PT"}})]
    assert phase.column_properties[0][0] == 0
    assert phase.column_properties[0][1]["widthType"] == "FIXED_WIDTH"


def test_nested_table_plans_recursively():
    inner = _table([[[_para("deep\n")]]])
    outer = _table([[[_para("intro\n"), inner]]])
    plan = _plan([outer])
    outer_phase = plan.phases[0]
    assert isinstance(outer_phase, TablePhase)
    cell_plan = outer_phase.cell_plans[(0, 0)]
    assert any(isinstance(p, TablePhase) for p in cell_plan.phases)


# ---------------------------------------------------------------------
# E1 - pageBreakBefore must never ride a table-overlapping range
#
# updateParagraphStyle rejects pageBreakBefore on any range containing
# table paragraphs ("Cannot update page-break-before when the range
# contains paragraphs in a table"), and documents.get reports the field
# (usually false) on cell paragraphs - so the cell's own style replay
# used to 400 the whole batch, killing every H1 section carrying a
# table (bug report 2026-07-12, job b47927c4).
# ---------------------------------------------------------------------


def _has_page_break_before(value) -> bool:
    """True if ``pageBreakBefore`` appears as a key anywhere in a request
    tree (a style dict); the ``fields`` mask is a string value, so this
    catches the style-dict occurrence that drives the 400."""
    if isinstance(value, dict):
        if "pageBreakBefore" in value:
            return True
        return any(_has_page_break_before(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_page_break_before(v) for v in value)
    return False


def test_table_cell_paragraph_style_drops_page_break_before():
    # The discriminating unit: a table cell whose paragraph carries
    # pageBreakBefore must emit its paragraph-style request WITHOUT the
    # field (or its fields mask). Fails on pre-fix main, which passes it
    # straight through into a table-range updateParagraphStyle.
    cell = _para("Cell body\n", style="NORMAL_TEXT")
    cell["paragraph"]["paragraphStyle"]["pageBreakBefore"] = True
    cell["paragraph"]["paragraphStyle"]["alignment"] = "CENTER"
    plan = _plan([_table([[[cell]]])])
    (phase,) = plan.phases
    assert isinstance(phase, TablePhase)
    cell_plan = phase.cell_plans[(0, 0)]
    para_reqs = _requests_of_kind(cell_plan, "updateParagraphStyle")
    assert para_reqs, "expected a paragraph-style request in the cell"
    for req in para_reqs:
        assert "pageBreakBefore" not in req["paragraphStyle"]
        assert "pageBreakBefore" not in req["fields"].split(",")
    # The rest of the cell paragraph's styling still rides.
    assert para_reqs[0]["paragraphStyle"]["alignment"] == "CENTER"


def test_off_table_paragraph_keeps_page_break_before_only_when_true():
    # Off a table pageBreakBefore is legal and meaningful (a section's
    # first paragraph starting a new page), so a true value is preserved;
    # but documents.get reports pageBreakBefore=false on ordinary
    # paragraphs and re-emitting that default is pure bloat, so false is
    # dropped. Pre-fix main keeps the false value in the mask.
    truthy = _para("Break\n", style="HEADING_1")
    truthy["paragraph"]["paragraphStyle"]["pageBreakBefore"] = True
    falsy = _para("Plain\n", style="HEADING_1")
    falsy["paragraph"]["paragraphStyle"]["pageBreakBefore"] = False
    plan = _plan([truthy, falsy])
    kept, dropped = _requests_of_kind(plan, "updateParagraphStyle")
    assert kept["paragraphStyle"].get("pageBreakBefore") is True
    assert "pageBreakBefore" in kept["fields"].split(",")
    assert "pageBreakBefore" not in dropped["paragraphStyle"]
    assert "pageBreakBefore" not in dropped["fields"].split(",")


# ---------------------------------------------------------------------
# Planner - per-tab style sheets (named styles / document style)
# ---------------------------------------------------------------------


def test_named_styles_reemitted_onto_dest_tab():
    named_styles = {
        "styles": [
            {
                "namedStyleType": "HEADING_1",
                "textStyle": {"bold": True, "fontSize": {"magnitude": 24, "unit": "PT"}},
                "paragraphStyle": {"spaceAbove": {"magnitude": 20, "unit": "PT"}},
            },
            {"namedStyleType": "NAMED_STYLE_TYPE_UNSPECIFIED", "textStyle": {"bold": True}},
        ]
    }
    plan = _plan([_para("x\n")], named_styles=named_styles)
    named_reqs = _requests_of_kind(plan, "updateNamedStyle")
    assert len(named_reqs) == 1
    req = named_reqs[0]
    assert req["tabId"] == TAB
    assert req["namedStyle"]["namedStyleType"] == "HEADING_1"
    # The mask MUST lead with namedStyleType: the live API rejects an
    # updateNamedStyle whose fields mask omits it ("Named style type is
    # required" - hit on first live contact, 2026-07-08).
    assert req["fields"].split(",")[0] == "namedStyleType"
    assert "textStyle.bold" in req["fields"]
    assert "paragraphStyle.spaceAbove" in req["fields"]


def test_document_style_margins_carried_page_fields_only():
    doc_style = {
        "marginTop": {"magnitude": 36, "unit": "PT"},
        "pageSize": {"width": {"magnitude": 612, "unit": "PT"}},
        "background": {"color": {}},
    }
    plan = _plan([_para("x\n")], document_style=doc_style)
    (req,) = _requests_of_kind(plan, "updateDocumentStyle")
    assert req["tabId"] == TAB
    assert set(req["documentStyle"]) == {"marginTop", "pageSize"}


def test_custom_heading_named_style_and_monospace_run_both_carry():
    # E2 machinery pin: when the source read surfaces a custom Heading1
    # named style (navy, bold, 32pt) AND a run carries direct monospace
    # formatting, the transplant re-emits BOTH - the sheet definition via
    # updateNamedStyle and the run look via updateTextStyle. This is the
    # #223 path, never live-proved with REAL custom styling until the E2
    # field report; it passes when the sheet reaches the planner (the E2
    # break is upstream: the sheet not reaching the planner at all).
    named_styles = {
        "styles": [
            {
                "namedStyleType": "HEADING_1",
                "textStyle": {
                    "foregroundColor": {
                        "color": {"rgbColor": {"red": 0.118, "green": 0.227, "blue": 0.372}}
                    },
                    "bold": True,
                    "fontSize": {"magnitude": 32, "unit": "PT"},
                },
                "paragraphStyle": {},
            }
        ]
    }
    mono = _para(
        "code()\n", text_style={"weightedFontFamily": {"fontFamily": "Courier New"}}
    )
    plan = _plan(
        [_para("Chapter\n", style="HEADING_1"), mono], named_styles=named_styles
    )
    (named_req,) = _requests_of_kind(plan, "updateNamedStyle")
    assert named_req["namedStyle"]["namedStyleType"] == "HEADING_1"
    assert named_req["namedStyle"]["textStyle"]["bold"] is True
    assert named_req["namedStyle"]["textStyle"]["fontSize"]["magnitude"] == 32
    assert "foregroundColor" in named_req["namedStyle"]["textStyle"]
    assert "textStyle.foregroundColor" in named_req["fields"]
    # The run-level monospace override rides too (a character-style look
    # Drive import bakes into direct run formatting).
    text_reqs = _requests_of_kind(plan, "updateTextStyle")
    assert any(
        tr["textStyle"].get("weightedFontFamily", {}).get("fontFamily") == "Courier New"
        for tr in text_reqs
    )


def test_has_named_style_content_flags_a_missing_or_empty_sheet():
    # The predicate the convert caller uses to decide whether to warn:
    # a real sheet -> True; None / empty / unspecified-only -> False.
    assert ct.has_named_style_content(
        {"styles": [{"namedStyleType": "HEADING_1", "textStyle": {"bold": True}}]}
    )
    assert not ct.has_named_style_content(None)
    assert not ct.has_named_style_content({})
    assert not ct.has_named_style_content({"styles": []})
    assert not ct.has_named_style_content(
        {"styles": [{"namedStyleType": "NAMED_STYLE_TYPE_UNSPECIFIED", "textStyle": {"bold": True}}]}
    )


# ---------------------------------------------------------------------
# Fidelity preflight
# ---------------------------------------------------------------------


def test_scan_source_fidelity_renders_counted_warnings():
    elements = [
        {"paragraph": {"elements": [{"equation": {}}, {"equation": {}}, {"textRun": {"content": "\n"}}]}},
    ]
    report = scan_source_fidelity(elements, lists={}, inline_objects={})
    assert report.counts["equation"] == 2
    assert any(w.startswith("2 equation(s)") for w in report.warnings)
    assert report.notes == []


def test_registry_keys_cover_every_counter_the_planner_uses():
    # Guard against a counter drifting out of the exported registries:
    # a kind that is counted but not registered would render nowhere.
    known = set(DROPPED_KINDS) | set(DEGRADED_KINDS)
    counted = {
        "equation", "drawing", "positioned_object", "footnote", "auto_text",
        "column_break", "date_chip", "unsupported_object", "unknown_inline",
        "unknown_block", "horizontal_rule", "list_numbering_restart",
        "internal_link", "image_decoration", "linked_chart", "toc",
        "merged_cells", "multi_section",
    }
    assert counted <= known


# ---------------------------------------------------------------------
# Rebase
# ---------------------------------------------------------------------


def test_rebase_shifts_all_index_keys_and_nothing_else():
    requests = [
        {"insertText": {"location": {"index": 0, "tabId": TAB}, "text": "x"}},
        {
            "updateTextStyle": {
                "range": {"startIndex": 0, "endIndex": 1, "tabId": TAB},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        },
        {"updateNamedStyle": {"namedStyle": {"namedStyleType": "TITLE"}, "fields": "x", "tabId": TAB}},
    ]
    rebased = _rebase_requests(requests, 10)
    assert rebased[0]["insertText"]["location"]["index"] == 10
    assert rebased[1]["updateTextStyle"]["range"]["startIndex"] == 10
    assert rebased[1]["updateTextStyle"]["range"]["endIndex"] == 11
    assert rebased[2] == requests[2]
    # Pure: the originals are untouched.
    assert requests[0]["insertText"]["location"]["index"] == 0


# ---------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------

_text_strategy = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n"),
    min_size=0,
    max_size=40,
)


@given(texts=st.lists(_text_strategy, min_size=1, max_size=10))
@settings(max_examples=60)
def test_property_paragraph_text_reconstructs_exactly(texts):
    """The transplant's core contract: for paragraph-only content, the
    concatenated insertText payload IS the source text (each paragraph
    newline-terminated). Catches dropped runs, doubled newlines, and
    ordering bugs."""
    elements = [_para(t + "\n") for t in texts]
    plan = _plan(elements)
    assert _inserted_text(plan) == "".join(t + "\n" for t in texts)
    assert plan.block_count == len(texts)


@given(texts=st.lists(_text_strategy, min_size=1, max_size=8))
@settings(max_examples=60)
def test_property_offsets_are_utf16_and_monotonic(texts):
    """Insert locations must be non-decreasing and the phase length must
    equal the UTF-16 unit count of everything inserted (the R6 / PR #184
    unit basis - above-BMP characters count 2)."""
    plan = _plan([_para(t + "\n") for t in texts])
    (phase,) = [p for p in plan.phases if isinstance(p, SegmentPhase)]
    locations = [
        r["insertText"]["location"]["index"]
        for r in phase.requests
        if "insertText" in r
    ]
    assert locations == sorted(locations)
    expected_units = sum(
        len((t + "\n").encode("utf-16-le")) // 2 for t in texts
    )
    assert phase.length == expected_units


def _walk_locations(value, found):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("location", "range", "tableStartLocation", "endOfSegmentLocation"):
                found.append(v)
            _walk_locations(v, found)
    elif isinstance(value, list):
        for v in value:
            _walk_locations(v, found)


@given(texts=st.lists(_text_strategy, min_size=1, max_size=6))
@settings(max_examples=60)
def test_property_every_location_and_range_carries_the_dest_tab_id(texts):
    """tabId discipline: an omitted tabId silently targets the FIRST tab
    (the source being carved), so every Location/Range the planner emits
    must name the destination tab explicitly."""
    elements = [_para(t + "\n") for t in texts]
    elements.append(_bullet_para("item\n", "kix.z"))
    plan = _plan(elements, lists=_lists("kix.z", None))
    found: list[dict] = []
    _walk_locations(_segment_requests(plan), found)
    assert found, "expected at least one location/range"
    assert all(loc.get("tabId") == TAB for loc in found)


def test_above_bmp_character_advances_two_units():
    plan = _plan([_para("\U0001d400\n")])  # MATHEMATICAL BOLD CAPITAL A
    (phase,) = [p for p in plan.phases if isinstance(p, SegmentPhase)]
    assert phase.length == 3  # surrogate pair (2) + newline (1)


@given(base=st.integers(min_value=1, max_value=10_000), texts=st.lists(_text_strategy, min_size=1, max_size=5))
@settings(max_examples=40)
def test_property_rebase_roundtrip(base, texts):
    plan = _plan([_para(t + "\n") for t in texts])
    requests = _segment_requests(plan)
    assert _rebase_requests(_rebase_requests(requests, base), -base) == requests


# ---------------------------------------------------------------------
# Executor - scripted fake Docs service
# ---------------------------------------------------------------------


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeDocs:
    """Minimal documents() stand-in: batchUpdate records each request
    list at execute() time (optionally failing on a predicate); get
    serves a scripted queue of document states (last one repeats)."""

    def __init__(self, get_responses: list[dict], fail_when=None):
        self._gets = list(get_responses)
        self.batches: list[list[dict]] = []
        self.fail_when = fail_when

    def documents(self):
        return self

    def get(self, documentId, includeTabsContent=True):
        def _run():
            doc = self._gets[0] if len(self._gets) == 1 else self._gets.pop(0)
            return doc
        return _Call(_run)

    def batchUpdate(self, documentId, body):
        def _run():
            requests = body["requests"]
            if self.fail_when is not None and self.fail_when(requests):
                raise RuntimeError("synthetic batch failure")
            self.batches.append(requests)
            return {}
        return _Call(_run)


def _doc_with_tabs(*tabs: tuple[str, list[dict]]) -> dict:
    return {
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id},
                "documentTab": {"body": {"content": content}},
            }
            for tab_id, content in tabs
        ]
    }


def _empty_tab_content() -> list[dict]:
    # A fresh shell: one empty paragraph, body ends at index 2.
    return [{"startIndex": 1, "endIndex": 2, "paragraph": {"elements": []}}]


def test_executor_rebases_segment_at_tab_append_point():
    plan = _plan([_para("Hi\n")])
    fake = _FakeDocs([_doc_with_tabs((TAB, _empty_tab_content()))])
    blocks = execute_tab_transplant(fake, "DOC", TAB, plan)
    assert blocks == 1
    (batch,) = fake.batches
    assert batch[0]["insertText"]["location"] == {"index": 1, "tabId": TAB}


def test_executor_accepts_prefetched_document_and_skips_initial_get():
    plan = _plan([_para("Hi\n")])
    doc = _doc_with_tabs((TAB, _empty_tab_content()))
    fake = _FakeDocs([{"tabs": []}])  # would fail if the executor re-fetched
    execute_tab_transplant(fake, "DOC", TAB, plan, document=doc)
    assert len(fake.batches) == 1


def test_batch_update_chunks_at_the_request_cap():
    fake = _FakeDocs([{}])
    requests = [{"insertText": {"location": {"index": i, "tabId": TAB}, "text": "x"}} for i in range(950)]
    ct._batch_update(fake, "DOC", requests)
    assert [len(b) for b in fake.batches] == [400, 400, 150]
    # Order is preserved across chunk boundaries.
    flat = [r for b in fake.batches for r in b]
    assert flat == requests


def _fresh_grid_doc(tab_id: str, base: int, rows: int, cols: int) -> dict:
    """Document state after insertTable at ``base``: newline + table.
    Cell start indices ascend in document order."""
    table_start = base + 1
    idx = table_start + 1
    table_rows = []
    for _r in range(rows):
        cells = []
        for _c in range(cols):
            cells.append(
                {"content": [{"startIndex": idx, "endIndex": idx + 1, "paragraph": {}}]}
            )
            idx += 2
        table_rows.append({"tableCells": cells})
    table_elem = {
        "startIndex": table_start,
        "endIndex": idx,
        "table": {"tableRows": table_rows, "columns": cols},
    }
    content = [
        {"startIndex": 1, "endIndex": base + 1, "paragraph": {}},
        table_elem,
        {"startIndex": idx, "endIndex": idx + 1, "paragraph": {}},
    ]
    return _doc_with_tabs((tab_id, content))


def test_table_phase_creates_syncs_fills_reverse_then_styles():
    table = _table([[[_para("A\n")], [_para("B\n")]]])
    table["table"]["tableRows"][0]["tableCells"][0]["tableCellStyle"] = {
        "backgroundColor": {"color": {"rgbColor": {"red": 1}}}
    }
    plan = _plan([table])
    shell = _doc_with_tabs((TAB, _empty_tab_content()))
    grid = _fresh_grid_doc(TAB, base=1, rows=1, cols=2)
    fake = _FakeDocs([grid])  # served for both sync-point gets
    execute_tab_transplant(fake, "DOC", TAB, plan, document=shell)

    assert "insertTable" in fake.batches[0][0]
    assert fake.batches[0][0]["insertTable"]["location"] == {"index": 1, "tabId": TAB}
    # Fills land bottom-up: cell (0,1) (higher start) before cell (0,0).
    fill_starts = [
        b[0]["insertText"]["location"]["index"]
        for b in fake.batches[1:3]
    ]
    assert fill_starts == sorted(fill_starts, reverse=True)
    # Table-level styling references the table's start Location.
    style_batch = fake.batches[3]
    assert "updateTableCellStyle" in style_batch[0]
    cell_loc = style_batch[0]["updateTableCellStyle"]["tableRange"]["tableCellLocation"]
    assert cell_loc["tableStartLocation"] == {"index": 2, "tabId": TAB}


def test_table_cell_page_break_never_reaches_the_live_batch():
    """E1 end to end through the executor: a cell paragraph carrying
    pageBreakBefore (the shape documents.get returns) must never appear
    in a sent batchUpdate - Google 400s pageBreakBefore on any table
    range, which is what killed every table-bearing H1 section convert.
    Pre-fix, the cell's own style replay sends it and the batch dies."""
    cell = _para("cell\n", style="NORMAL_TEXT")
    cell["paragraph"]["paragraphStyle"]["pageBreakBefore"] = True
    cell["paragraph"]["paragraphStyle"]["alignment"] = "CENTER"
    plan = _plan([_table([[[cell]]])])
    shell = _doc_with_tabs((TAB, _empty_tab_content()))
    grid = _fresh_grid_doc(TAB, base=1, rows=1, cols=1)
    fake = _FakeDocs([grid])
    execute_tab_transplant(fake, "DOC", TAB, plan, document=shell)
    # No request in any sent batch carries pageBreakBefore.
    assert not _has_page_break_before(fake.batches)
    # The cell paragraph's non-hostile styling still lands.
    para_styles = [
        r["updateParagraphStyle"]["paragraphStyle"]
        for batch in fake.batches
        for r in batch
        if "updateParagraphStyle" in r
    ]
    assert any(ps.get("alignment") == "CENTER" for ps in para_styles)


def test_verify_passes_when_blocks_present_and_fails_when_short():
    plan = _plan([_para("a\n"), _para("b\n")])
    good = _doc_with_tabs(
        (TAB, [{"paragraph": {}}, {"paragraph": {}}, {"paragraph": {}}])
    )
    verify_tab_transplant(good, TAB, plan)  # no raise
    short = _doc_with_tabs((TAB, [{"paragraph": {}}]))
    try:
        verify_tab_transplant(short, TAB, plan)
    except RuntimeError as e:
        assert "verification failed" in str(e)
    else:
        raise AssertionError("verify_tab_transplant should have raised")


def test_carve_deletes_ranges_bottom_up_capped_before_body_end():
    children = [
        {"startIndex": 1, "endIndex": 10},
        {"startIndex": 10, "endIndex": 30},
        {"startIndex": 30, "endIndex": 45},
    ]
    fake = _FakeDocs([{}])
    carve_source_ranges(fake, "DOC", "t.0", children, [(0, 0), (2, 2)])
    (batch,) = fake.batches
    ranges = [r["deleteContentRange"]["range"] for r in batch]
    # Bottom-up, tab-stamped, and the final span stops one unit short of
    # the body end (the last paragraph mark cannot be deleted).
    assert ranges == [
        {"startIndex": 30, "endIndex": 44, "tabId": "t.0"},
        {"startIndex": 1, "endIndex": 10, "tabId": "t.0"},
    ]


def test_carve_noop_on_empty_children():
    fake = _FakeDocs([{}])
    carve_source_ranges(fake, "DOC", "t.0", [], [])
    assert fake.batches == []
