"""Docs-JSON to batchUpdate content transplant (the REST tabs renderer).

Moves content read from one tab (``documents.get`` with
``includeTabsContent=true``) into another tab of the same document by
re-emitting it as ``documents.batchUpdate`` requests. This replaces the
per-user Apps Script web-app step of the ``gdocs_tab_existing_doc``
pipeline: the /exec POST died at Google's per-script consent door for
every cloud user (see ``_audit/2026-07-08-exec-scope-auth-spike.md``
and ``_audit/2026-07-08-tabs-architecture-decision.md``), while every
request emitted here runs under the already-granted ``documents``
scope with no second consent surface.

Core insight: the Docs READ shape is (mostly) the WRITE shape. A
``textStyle`` / ``paragraphStyle`` / ``tableCellStyle`` dict read from
``documents.get`` passes through to the matching ``update*`` request
with a computed ``fields`` mask, so the planner is a dict-passthrough
walker plus request emission, not a parser.

Layering (mirrors the ``markdown_render.py`` / ``api.py`` split):

* **Planner (pure, no network):** ``plan_tab_transplant`` walks a
  slice of a source tab's ``body.content`` and returns a
  ``TabTransplantPlan`` of phases. ``SegmentPhase`` requests carry
  indices RELATIVE to the phase's insertion point (0-based); the
  executor rebases them, so the planner is testable with no fetched
  document. Tables become ``TablePhase`` entries because cell indices
  are server-assigned only after the empty grid exists (the proven
  two-phase pattern from ``insert_markdown_table``).
* **Executor (network):** ``execute_tab_transplant`` runs the phases
  in order, re-fetching at each table sync-point to learn real cell
  start indices and to re-anchor the append position (which also
  kills cumulative index drift).

Index discipline: all offsets are UTF-16 code units
(``len(text.encode("utf-16-le")) // 2``), the R6 / PR #184 contract
shared with ``markdown_render.py``. Inline objects (images, page
breaks, person/rich-link chips) each occupy exactly 1 unit.

Tab discipline: every ``Location`` / ``Range`` emitted here carries an
explicit ``tabId``. An omitted ``tabId`` silently targets the FIRST
tab, which during a transplant is the source tab being carved, i.e.
corruption, not an error.

Fidelity: not lossless. Elements the REST API cannot re-emit are
DETECTED and counted in a ``FidelityReport`` so the caller can warn
per document before converting; nothing is dropped silently. The
detection registry (``DROPPED_KINDS`` / ``DEGRADED_KINDS``) is
exported for reuse by any future channel that closes the tail.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from googleapiclient.errors import HttpError

from appscriptly.google_api_client import execute_with_retry

from .tab_tree import _find_tab_by_id

_log = logging.getLogger("appscriptly.docs.transplant")

# Docs batchUpdate accepts large request lists but degrades on huge
# payloads; chunking keeps each POST well under the practical limit.
# Chunk boundaries are safe anywhere because request order is preserved
# across sequential batchUpdate calls.
_MAX_REQUESTS_PER_BATCH = 400

# ---------------------------------------------------------------------
# 429 backoff for the transplant WRITE path (N2, 2026-07-10 retest)
#
# Concurrent convert jobs can trip the per-user Docs write quota
# (WriteRequestsPerMinutePerUser = 60); Google answers HTTP 429
# RATE_LIMIT_EXCEEDED. A 429 is issued by the rate limiter AT THE DOOR,
# BEFORE the batchUpdate executes, so retrying a write is safe - unlike
# a 5xx, where the mutation may have partially applied and a blind
# replay risks duplicate inserts (which is exactly why _batch_update
# passes idempotent=False to the chokepoint and why this handler
# retries 429 and ONLY 429).
#
# The wait budget is shared across ONE public entry (one tab transplant
# or one carve): bounded exponential backoff + full jitter until the
# budget is spent, then the HttpError propagates into the existing
# keep-the-doc + completion-manifest failure path.
# ---------------------------------------------------------------------

_RATE_LIMIT_WAIT_BUDGET_SECONDS = 75.0
_RATE_LIMIT_BASE_WAIT_SECONDS = 2.0
_RATE_LIMIT_MAX_WAIT_SECONDS = 32.0


class _RateLimitBudget:
    """Total-sleep allowance for one transplant/carve execution."""

    def __init__(self, seconds: float = _RATE_LIMIT_WAIT_BUDGET_SECONDS) -> None:
        self.remaining = seconds
        self.attempt = 0

    def next_wait(self) -> float | None:
        """The next backoff sleep, or None when the budget is spent."""
        self.attempt += 1
        base = min(
            _RATE_LIMIT_BASE_WAIT_SECONDS * (2 ** (self.attempt - 1)),
            _RATE_LIMIT_MAX_WAIT_SECONDS,
        )
        wait = base + random.uniform(0.0, base / 2.0)
        if wait > self.remaining:
            return None
        self.remaining -= wait
        return wait


def _is_rate_limit_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, HttpError)
        and getattr(exc, "status_code", None) == 429
    )


# ---------------------------------------------------------------------
# Per-user cross-job WRITE GOVERNOR (A2 root fix, 2026-07-10 retest 3)
#
# The 60-writes/min Docs quota is PER USER, but concurrent convert jobs
# each ran their own writes flat out: under an 8-job storm they competed
# each other into 429s until one job's retry budget bled dry and it died
# terminally (7/8). The governor paces batchUpdate SENDS across every
# concurrent job of the same user so the aggregate stays under quota -
# the 429s (and their budget burn) largely stop happening at the source.
#
# Mechanics: one pacer per user key, shared across the worker THREADS
# that run converts (asyncio.to_thread). acquire() reserves the next
# send slot under a lock and sleeps outside it, so concurrent writers
# space themselves ~MIN_INTERVAL apart in reservation order. Waiting on
# the governor is ordinary pacing, NOT a 429 retry: it consumes no
# _RateLimitBudget.
#
# Interval: 60/min quota -> ~1.09s floor for a 55/min target; 1.1s
# default leaves headroom for the few ungoverned writes elsewhere
# (icons, tab deletes). Override with DOCS_WRITE_MIN_INTERVAL_SECONDS
# (0 disables - the test suites do this to stay fast).
# ---------------------------------------------------------------------

_WRITE_GOVERNOR_DEFAULT_INTERVAL_SECONDS = 1.1
_WRITE_GOVERNOR_ENV = "DOCS_WRITE_MIN_INTERVAL_SECONDS"


def _write_governor_interval() -> float:
    raw = os.environ.get(_WRITE_GOVERNOR_ENV, "")
    if not raw:
        return _WRITE_GOVERNOR_DEFAULT_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        _log.warning(
            "invalid %s=%r; using default %.2fs",
            _WRITE_GOVERNOR_ENV, raw,
            _WRITE_GOVERNOR_DEFAULT_INTERVAL_SECONDS,
        )
        return _WRITE_GOVERNOR_DEFAULT_INTERVAL_SECONDS


class _WriteGovernor:
    """Minimum-interval pacer for one user's Docs write requests.

    Thread-safe: the reservation (compute my wait, book the next free
    slot) happens under the lock; the sleep happens outside it, so N
    threads queue up in reservation order without serializing their
    sleeps. The interval is re-read from the env per acquire so tests
    and operators can tune without restarting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_free = 0.0  # time.monotonic() when the next send may go

    def acquire(self) -> float:
        """Block until this thread's send slot; return the wait served."""
        interval = _write_governor_interval()
        if interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_free - now)
            self._next_free = max(now, self._next_free) + interval
        if wait > 0:
            time.sleep(wait)
        return wait


_GOVERNORS: dict[str, _WriteGovernor] = {}
_GOVERNORS_LOCK = threading.Lock()


def _governor_for(key: str | None) -> _WriteGovernor:
    """The (created-on-demand) pacer for one user key.

    ``None`` maps to the operator/single-tenant bucket - the quota is
    per Google user either way, and bearer-path converts all run as the
    operator."""
    resolved = key or "operator"
    with _GOVERNORS_LOCK:
        governor = _GOVERNORS.get(resolved)
        if governor is None:
            governor = _WriteGovernor()
            _GOVERNORS[resolved] = governor
        return governor


# ---------------------------------------------------------------------
# Fidelity registry - the honest non-representable tail
# ---------------------------------------------------------------------

# Elements with NO REST write path: detected, counted, omitted.
DROPPED_KINDS: dict[str, str] = {
    "equation": "equation(s) omitted (equation content is not readable via the Docs REST API)",
    "drawing": "embedded drawing(s) omitted (drawings have no REST write path)",
    "positioned_object": "positioned/floating object(s) omitted (no REST re-insert path)",
    "footnote": "footnote(s) omitted (footnote fill is not yet implemented)",
    "auto_text": "auto-text field(s) (page numbers) omitted",
    "column_break": "column break(s) omitted",
    "date_chip": "date smart chip(s) omitted",
    "unsupported_object": "embedded object(s) with no readable image content omitted",
    "unknown_inline": "unrecognized inline element(s) omitted",
    "unknown_block": "unrecognized structural element(s) omitted",
}

# Elements that ARE carried, with a visible downgrade.
DEGRADED_KINDS: dict[str, str] = {
    "horizontal_rule": "native horizontal rule(s) rendered as a bottom-border paragraph",
    "list_numbering_restart": "ordered-list numbering restarts where a list is interrupted by other content",
    "internal_link": "in-document heading/bookmark link(s) kept as plain text (target tab changes during the split)",
    "image_decoration": "image crop/rotation/title/alt-text not carried (image itself re-inserted)",
    "linked_chart": "linked chart(s) re-inserted as static image snapshots",
    "toc": "table of contents replaced with a placeholder (the tab sidebar is the navigation)",
    "merged_cells": "merged table cell(s) re-merged best-effort",
    "multi_section": "multi-section layout (columns, per-section page setup) not carried",
}


@dataclass
class FidelityReport:
    """Per-document tally of everything the transplant cannot carry 1:1.

    ``counts`` maps a kind key (from ``DROPPED_KINDS`` /
    ``DEGRADED_KINDS``) to the number of occurrences detected. The
    ``warnings`` / ``notes`` properties render the human-facing lists
    the convert pipeline returns (warnings = content loss the caller
    should know about; notes = visible-but-carried degradations).
    """

    counts: dict[str, int] = field(default_factory=dict)

    def count(self, kind: str, n: int = 1) -> None:
        if n <= 0:
            return
        self.counts[kind] = self.counts.get(kind, 0) + n

    def _render(self, registry: dict[str, str]) -> list[str]:
        return [
            f"{self.counts[kind]} {registry[kind]}"
            for kind in registry
            if self.counts.get(kind, 0) > 0
        ]

    @property
    def warnings(self) -> list[str]:
        return self._render(DROPPED_KINDS)

    @property
    def notes(self) -> list[str]:
        return self._render(DEGRADED_KINDS)


# ---------------------------------------------------------------------
# Writable-field whitelists (read shape -> write mask)
# ---------------------------------------------------------------------
#
# documents.get returns some read-only fields inside the style objects
# (e.g. ParagraphStyle.headingId, TableCellStyle.rowSpan). Naming one
# in an update request's ``fields`` mask is a 400, so passthrough is
# whitelist-filtered: anything not listed is dropped from both the
# style dict and the mask. A new read-only field Google adds therefore
# degrades to "not carried", never to a failed batch.

_TEXT_STYLE_FIELDS = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "smallCaps",
    "backgroundColor",
    "foregroundColor",
    "fontSize",
    "weightedFontFamily",
    "baselineOffset",
    "link",
)

_PARAGRAPH_STYLE_FIELDS = (
    "namedStyleType",
    "alignment",
    "lineSpacing",
    "direction",
    "spacingMode",
    "spaceAbove",
    "spaceBelow",
    "borderBetween",
    "borderTop",
    "borderBottom",
    "borderLeft",
    "borderRight",
    "indentFirstLine",
    "indentStart",
    "indentEnd",
    "keepLinesTogether",
    "keepWithNext",
    "avoidWidowAndOrphan",
    "shading",
    "pageBreakBefore",
)

_TABLE_CELL_STYLE_FIELDS = (
    "backgroundColor",
    "borderLeft",
    "borderRight",
    "borderTop",
    "borderBottom",
    "paddingLeft",
    "paddingRight",
    "paddingTop",
    "paddingBottom",
    "contentAlignment",
)

_TABLE_ROW_STYLE_FIELDS = ("minRowHeight",)

_TABLE_COLUMN_PROPERTIES_FIELDS = ("widthType", "width")

_DOCUMENT_STYLE_FIELDS = (
    "marginTop",
    "marginBottom",
    "marginLeft",
    "marginRight",
    "pageSize",
)

# The horizontal-rule fallback border: the Docs request set has no
# insertHorizontalRule, so an HR paragraph gets a bottom border that
# reads as a rule. Values approximate the native rule's rendering.
_HR_FALLBACK_BORDER = {
    "color": {"color": {"rgbColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}},
    "width": {"magnitude": 1, "unit": "PT"},
    "padding": {"magnitude": 1, "unit": "PT"},
    "dashStyle": "SOLID",
}


def _filtered_style(style: dict, allowed: tuple[str, ...]) -> dict:
    return {k: v for k, v in style.items() if k in allowed and v is not None}


def _writable_text_style(style: dict, report: FidelityReport) -> dict:
    out = _filtered_style(style, _TEXT_STYLE_FIELDS)
    link = out.get("link")
    if isinstance(link, dict) and "url" not in link:
        # headingId / bookmarkId / tab-internal links point at anchors
        # that move or vanish during the split; the remap pass is a
        # follow-up, so v1 keeps the text and drops the link target.
        out.pop("link")
        report.count("internal_link")
    return out


def _writable_paragraph_style(style: dict, *, in_table: bool = False) -> dict:
    out = _filtered_style(style, _PARAGRAPH_STYLE_FIELDS)
    # pageBreakBefore is rejected by updateParagraphStyle on ANY range
    # that overlaps a table ("Cannot update page-break-before when the
    # range contains paragraphs in a table"), and documents.get reports
    # it - usually false - on cell paragraphs, so a table cell's own
    # style replay would 400 the whole transplant batch (E1: any H1
    # section carrying a table died mid-transplant). Off a table it is
    # legal, but re-emitting the false default on every paragraph is
    # pure request bloat; keep it only where it is actually set true (a
    # section-first page break - the meaningful case the tester asked to
    # preserve).
    if in_table or out.get("pageBreakBefore") is not True:
        out.pop("pageBreakBefore", None)
    return out


def _style_request(kind: str, range_: dict, style: dict, style_key: str) -> dict:
    return {
        kind: {
            "range": range_,
            style_key: style,
            "fields": ",".join(sorted(style)),
        }
    }


# ---------------------------------------------------------------------
# List (bullet) preset mapping
# ---------------------------------------------------------------------


# Numbered glyph types (ListProperties.nestingLevels[].glyphType).
_NUMBERED_GLYPH_TYPES = frozenset(
    {"DECIMAL", "ZERO_DECIMAL", "ALPHA", "UPPER_ALPHA", "ROMAN", "UPPER_ROMAN"}
)


def _bullet_preset_for(list_id: str | None, lists: dict) -> str:
    """Map a source list's level-0 glyph to the nearest bullet preset.

    ``createParagraphBullets`` only accepts PRESETS, so custom glyph
    formats collapse to the closest one (documented degradation). The
    numbering-continuity caveat also lives here: each contiguous
    bulleted range becomes its OWN list on the write side (there is no
    API to attach a paragraph to an existing listId or to set a start
    number), so a numbered list interrupted by other content restarts
    at 1 after the interruption, and numbering never continues across
    tabs.
    """
    props = (lists or {}).get(list_id or "", {}).get("listProperties", {})
    levels = props.get("nestingLevels") or []
    level0 = levels[0] if levels else {}
    if level0.get("glyphType") in _NUMBERED_GLYPH_TYPES:
        fmt = str(level0.get("glyphFormat") or "")
        if fmt.rstrip().endswith(")"):
            return "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS"
        return "NUMBERED_DECIMAL_ALPHA_ROMAN"
    return "BULLET_DISC_CIRCLE_SQUARE"


# ---------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------


@dataclass
class SegmentPhase:
    """One batchUpdate program with indices relative to its insertion
    point (0-based offsets; the executor adds the absolute base).

    ``length`` is the total UTF-16 units the phase inserts, so the
    executor can advance its append position without a re-fetch.
    """

    requests: list[dict] = field(default_factory=list)
    length: int = 0


@dataclass
class TablePhase:
    """A table to create-then-fill at the phase's insertion point.

    Everything here is addressed by (row, column) or by the table's
    start Location, so no absolute indices are baked in; the executor
    learns them from the post-insert re-fetch (the sync-point).
    """

    rows: int
    columns: int
    tab_id: str
    # (row, col) -> recursive plan for that cell's content. Only cells
    # with real content appear (a fresh cell already holds one empty
    # paragraph, so planning an empty source cell would add a stray
    # blank line).
    cell_plans: dict[tuple[int, int], "TabTransplantPlan"] = field(default_factory=dict)
    # (row, col, writable TableCellStyle dict)
    cell_styles: list[tuple[int, int, dict]] = field(default_factory=list)
    # (row, col, row_span, col_span) merge blocks, applied AFTER fills.
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    # (row index, writable TableRowStyle dict)
    row_styles: list[tuple[int, dict]] = field(default_factory=list)
    # (column index, writable TableColumnProperties dict)
    column_properties: list[tuple[int, dict]] = field(default_factory=list)
    pinned_header_rows: int = 0


@dataclass
class TabTransplantPlan:
    """The full request program for one destination tab (or one cell)."""

    phases: list[SegmentPhase | TablePhase] = field(default_factory=list)
    # Structural blocks planned (paragraphs + tables + TOC placeholders):
    # the executor's post-transplant verify floor.
    block_count: int = 0


# ---------------------------------------------------------------------
# Planner - segment builder (mirrors markdown_render's _Ctx)
# ---------------------------------------------------------------------


def _utf16_len(text: str) -> int:
    # R6 contract: Google Docs addresses positions in UTF-16 code
    # units; above-BMP characters are 2 units, so len(text) drifts.
    return len(text.encode("utf-16-le")) // 2


class _SegmentBuilder:
    """Accumulates one SegmentPhase: forward inserts + deferred styles."""

    def __init__(self, tab_id: str) -> None:
        self.tab_id = tab_id
        self.offset = 0
        self.inserts: list[dict] = []
        self.text_styles: list[tuple[int, int, dict]] = []
        self.para_styles: list[tuple[int, int, dict]] = []
        self.list_items: list[dict] = []
        self._last_text = ""

    def loc(self) -> dict:
        return {"index": self.offset, "tabId": self.tab_id}

    def range(self, start: int, end: int) -> dict:
        return {"startIndex": start, "endIndex": end, "tabId": self.tab_id}

    def insert_text(self, text: str) -> None:
        if not text:
            return
        self.inserts.append(
            {"insertText": {"location": self.loc(), "text": text}}
        )
        self.offset += _utf16_len(text)
        self._last_text = text

    def insert_unit(self, request: dict) -> None:
        """Emit a request that occupies exactly 1 index unit (image,
        page break, person / rich-link chip)."""
        self.inserts.append(request)
        self.offset += 1
        self._last_text = ""

    def ends_with_newline(self) -> bool:
        return self._last_text.endswith("\n")

    def is_empty(self) -> bool:
        return not self.inserts

    def build(self) -> SegmentPhase:
        """Finalize into a SegmentPhase: inserts, then styles, then
        bullets bottom-up (createParagraphBullets consumes the leading
        nesting tabs and shifts everything below its range, so bullet
        requests must run last and in descending start order)."""
        requests = list(self.inserts)
        for start, end, style in self.text_styles:
            if end > start and style:
                requests.append(
                    _style_request(
                        "updateTextStyle", self.range(start, end), style, "textStyle"
                    )
                )
        for start, end, style in self.para_styles:
            if end > start and style:
                requests.append(
                    _style_request(
                        "updateParagraphStyle",
                        self.range(start, end),
                        style,
                        "paragraphStyle",
                    )
                )
        items = sorted(
            (i for i in self.list_items if i["end"] > i["start"]),
            key=lambda i: i["start"],
        )
        merged: list[dict] = []
        for it in items:
            if (
                merged
                and merged[-1]["preset"] == it["preset"]
                and it["start"] <= merged[-1]["end"] + 1
            ):
                merged[-1]["end"] = max(merged[-1]["end"], it["end"])
            else:
                merged.append(dict(it))
        for m in sorted(merged, key=lambda x: -x["start"]):
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": self.range(m["start"], m["end"]),
                        "bulletPreset": m["preset"],
                    }
                }
            )
        return SegmentPhase(requests=requests, length=self.offset)


# ---------------------------------------------------------------------
# Planner - element walkers
# ---------------------------------------------------------------------


def _plan_inline_object(
    seg: _SegmentBuilder,
    element: dict,
    inline_objects: dict,
    report: FidelityReport,
) -> None:
    obj = (inline_objects or {}).get(element.get("inlineObjectId") or "", {})
    obj_props = obj.get("inlineObjectProperties") or {}
    embedded = obj_props.get("embeddedObject") or {}
    if "embeddedDrawingProperties" in embedded:
        report.count("drawing")
        return
    image_props = embedded.get("imageProperties") or {}
    uri = image_props.get("contentUri")
    if not uri:
        report.count("unsupported_object")
        return
    request: dict[str, Any] = {"location": seg.loc(), "uri": uri}
    size = embedded.get("size") or obj_props.get("size") or {}
    if "width" in size and "height" in size:
        request["objectSize"] = {"width": size["width"], "height": size["height"]}
    if "linkedContentReference" in embedded:
        report.count("linked_chart")
    if (
        any(k in image_props for k in ("cropProperties", "angle"))
        or embedded.get("title")
        or embedded.get("description")
    ):
        report.count("image_decoration")
    seg.insert_unit({"insertInlineImage": request})


def _plan_paragraph(
    seg: _SegmentBuilder,
    para: dict,
    *,
    lists: dict,
    inline_objects: dict,
    report: FidelityReport,
    suppress_trailing_newline: bool = False,
    in_table: bool = False,
) -> None:
    para_start = seg.offset

    bullet = para.get("bullet")
    item_start = para_start
    preset = ""
    if bullet is not None:
        # Nesting travels as leading tabs, which createParagraphBullets
        # consumes to infer the level (the proven markdown_render trick).
        level = int(bullet.get("nestingLevel") or 0)
        if level > 0:
            seg.insert_text("\t" * level)
        item_start = seg.offset
        preset = _bullet_preset_for(bullet.get("listId"), lists)

    positioned = para.get("positionedObjectIds") or []
    if positioned:
        report.count("positioned_object", n=len(positioned))

    elements = para.get("elements") or []
    has_horizontal_rule = False
    for i, pe in enumerate(elements):
        if "textRun" in pe:
            run = pe["textRun"]
            content = run.get("content") or ""
            if (
                suppress_trailing_newline
                and i == len(elements) - 1
                and content.endswith("\n")
            ):
                content = content[:-1]
            if not content:
                continue
            start = seg.offset
            seg.insert_text(content)
            style = _writable_text_style(run.get("textStyle") or {}, report)
            if style:
                seg.text_styles.append((start, seg.offset, style))
        elif "inlineObjectElement" in pe:
            _plan_inline_object(seg, pe["inlineObjectElement"], inline_objects, report)
        elif "pageBreak" in pe:
            seg.insert_unit({"insertPageBreak": {"location": seg.loc()}})
        elif "horizontalRule" in pe:
            has_horizontal_rule = True
            report.count("horizontal_rule")
        elif "person" in pe:
            props = pe["person"].get("personProperties") or {}
            email = props.get("email")
            if email:
                seg.insert_unit(
                    {
                        "insertPerson": {
                            "location": seg.loc(),
                            "personProperties": {"email": email},
                        }
                    }
                )
            else:
                seg.insert_text(props.get("name") or "")
        elif "richLink" in pe:
            props = pe["richLink"].get("richLinkProperties") or {}
            uri = props.get("uri")
            if uri:
                seg.insert_unit(
                    {
                        "insertRichLink": {
                            "location": seg.loc(),
                            "richLinkProperties": {"uri": uri},
                        }
                    }
                )
        elif "footnoteReference" in pe:
            report.count("footnote")
        elif "equation" in pe:
            report.count("equation")
        elif "columnBreak" in pe:
            report.count("column_break")
        elif "autoText" in pe:
            report.count("auto_text")
        elif "dateElement" in pe:
            report.count("date_chip")
        else:
            report.count("unknown_inline")

    # Every source paragraph must land as a paragraph: if its visible
    # elements were all skipped (or it was empty), the newline still
    # carries the paragraph mark (and any border/shading styling). A
    # suppressed (cell-final) paragraph never adds one: the fresh
    # cell's own empty paragraph provides its mark.
    if not suppress_trailing_newline and (
        seg.offset == para_start or not seg.ends_with_newline()
    ):
        seg.insert_text("\n")

    style = _writable_paragraph_style(para.get("paragraphStyle") or {}, in_table=in_table)
    if has_horizontal_rule and "borderBottom" not in style:
        style["borderBottom"] = dict(_HR_FALLBACK_BORDER)
    # Skip a pure NORMAL_TEXT-only style: it's the default and would
    # just bloat the request program.
    if style == {"namedStyleType": "NORMAL_TEXT"}:
        style = {}
    if style and seg.offset > para_start:
        seg.para_styles.append((para_start, seg.offset, style))

    if bullet is not None and seg.offset > item_start:
        end = seg.offset - 1 if seg.ends_with_newline() else seg.offset
        seg.list_items.append(
            {"start": item_start, "end": end, "preset": preset}
        )


def _plan_toc_placeholder(seg: _SegmentBuilder, report: FidelityReport) -> None:
    # There is no REST request that creates a native TOC element; the
    # tab sidebar replaces its navigation job, so a one-line marker
    # keeps the position visible instead of silently vanishing.
    start = seg.offset
    seg.insert_text("[Table of contents omitted: the tab sidebar replaces it]\n")
    seg.text_styles.append((start, seg.offset - 1, {"italic": True}))
    report.count("toc")


def _cell_has_content(content: list[dict]) -> bool:
    """False when the cell is exactly one empty paragraph (the shape a
    freshly created cell already has, so filling it would only add a
    blank line)."""
    if len(content) != 1:
        return bool(content)
    para = content[0].get("paragraph")
    if para is None:
        return True
    for pe in para.get("elements") or []:
        if "textRun" in pe:
            if (pe["textRun"].get("content") or "").strip("\n"):
                return True
        else:
            return True
    return bool(para.get("bullet"))


def _plan_table(
    table: dict,
    *,
    tab_id: str,
    lists: dict,
    inline_objects: dict,
    report: FidelityReport,
) -> TablePhase:
    table_rows = table.get("tableRows") or []
    rows = len(table_rows)
    columns = int(table.get("columns") or 0)
    if columns <= 0:
        columns = max(
            (len(r.get("tableCells") or []) for r in table_rows), default=0
        )
    phase = TablePhase(rows=rows, columns=columns, tab_id=tab_id)

    # Occupancy-aware grid mapping: a merge head cell carries
    # rowSpan/columnSpan > 1 and the covered positions may not appear
    # as their own entries, so each row is placed with a cursor that
    # skips positions covered by merges from prior rows.
    occupied: set[tuple[int, int]] = set()
    saw_merge = False
    for r, row in enumerate(table_rows):
        row_style = _filtered_style(
            row.get("tableRowStyle") or {}, _TABLE_ROW_STYLE_FIELDS
        )
        if row_style:
            phase.row_styles.append((r, row_style))
        if (row.get("tableRowStyle") or {}).get("tableHeader"):
            if r == phase.pinned_header_rows:
                phase.pinned_header_rows += 1
        c = 0
        for cell in row.get("tableCells") or []:
            while (r, c) in occupied and c < columns:
                c += 1
            if c >= columns:
                break
            cell_style_raw = cell.get("tableCellStyle") or {}
            row_span = int(cell_style_raw.get("rowSpan") or 1)
            col_span = int(cell_style_raw.get("columnSpan") or 1)
            if row_span == 0 or col_span == 0:
                # Some producers mark covered cells with zero spans;
                # they hold no content of their own.
                c += 1
                continue
            row_span = min(row_span, rows - r)
            col_span = min(col_span, columns - c)
            for rr in range(r, r + row_span):
                for cc in range(c, c + col_span):
                    occupied.add((rr, cc))
            if row_span > 1 or col_span > 1:
                phase.merges.append((r, c, row_span, col_span))
                saw_merge = True

            content = cell.get("content") or []
            if _cell_has_content(content):
                phase.cell_plans[(r, c)] = plan_tab_transplant(
                    content,
                    lists=lists,
                    inline_objects=inline_objects,
                    dest_tab_id=tab_id,
                    report=report,
                    _in_table_cell=True,
                )
            cell_style = _filtered_style(cell_style_raw, _TABLE_CELL_STYLE_FIELDS)
            if cell_style:
                phase.cell_styles.append((r, c, cell_style))
            c += col_span

    for idx, col_props in enumerate(
        (table.get("tableStyle") or {}).get("tableColumnProperties") or []
    ):
        writable = _filtered_style(col_props, _TABLE_COLUMN_PROPERTIES_FIELDS)
        # EVENLY_DISTRIBUTED is the create-time default; re-stating it
        # without a width is a no-op not worth a request.
        if writable and writable != {"widthType": "EVENLY_DISTRIBUTED"}:
            phase.column_properties.append((idx, writable))

    if saw_merge:
        report.count("merged_cells")
    return phase


def _named_style_requests(named_styles: dict | None, tab_id: str) -> list[dict]:
    """Re-emit the source tab's named-style sheet onto the destination
    tab, so custom heading looks survive the move (a fresh tab starts
    from Google defaults, and paragraph reads carry only OVERRIDES of
    the sheet, not the sheet itself)."""
    requests: list[dict] = []
    for style in (named_styles or {}).get("styles") or []:
        style_type = style.get("namedStyleType")
        if not style_type or style_type == "NAMED_STYLE_TYPE_UNSPECIFIED":
            continue
        text_style = _filtered_style(style.get("textStyle") or {}, _TEXT_STYLE_FIELDS)
        para_style = _writable_paragraph_style(style.get("paragraphStyle") or {})
        para_style.pop("namedStyleType", None)
        style_fields = [f"textStyle.{k}" for k in sorted(text_style)] + [
            f"paragraphStyle.{k}" for k in sorted(para_style)
        ]
        if not style_fields:
            continue
        # The UpdateNamedStyleRequest fields mask MUST include
        # namedStyleType (it is the row selector, not an updated value);
        # the API rejects the request with "Named style type is
        # required" when the mask carries only textStyle/paragraphStyle
        # paths.
        fields = ["namedStyleType"] + style_fields
        requests.append(
            {
                "updateNamedStyle": {
                    "namedStyle": {
                        "namedStyleType": style_type,
                        "textStyle": text_style,
                        "paragraphStyle": para_style,
                    },
                    "fields": ",".join(fields),
                    "tabId": tab_id,
                }
            }
        )
    return requests


def _document_style_request(document_style: dict | None, tab_id: str) -> list[dict]:
    style = _filtered_style(document_style or {}, _DOCUMENT_STYLE_FIELDS)
    if not style:
        return []
    return [
        {
            "updateDocumentStyle": {
                "documentStyle": style,
                "fields": ",".join(sorted(style)),
                "tabId": tab_id,
            }
        }
    ]


def plan_tab_transplant(
    elements: list[dict],
    *,
    lists: dict,
    inline_objects: dict,
    dest_tab_id: str,
    named_styles: dict | None = None,
    document_style: dict | None = None,
    report: FidelityReport | None = None,
    _in_table_cell: bool = False,
) -> TabTransplantPlan:
    """Plan the batchUpdate program that re-creates ``elements`` (a
    slice of a source tab's ``body.content``) inside ``dest_tab_id``.

    Pure: consumes only dicts from a prior ``documents.get`` and
    returns a ``TabTransplantPlan``; every non-representable element
    is tallied on ``report`` (never silently skipped). ``named_styles``
    / ``document_style`` (tab-level sheets from the source
    ``documentTab``) ride ahead of the content as index-free requests.

    ``_in_table_cell`` marks a recursive cell plan: the cell's last
    paragraph suppresses its trailing newline so the fresh cell's own
    empty paragraph doesn't leave a stray blank line.
    """
    if report is None:
        report = FidelityReport()
    plan = TabTransplantPlan()

    prelude = _named_style_requests(named_styles, dest_tab_id)
    prelude += _document_style_request(document_style, dest_tab_id)
    if prelude:
        plan.phases.append(SegmentPhase(requests=prelude, length=0))

    # Track ordered-list interruptions for the numbering-continuity
    # caveat: a numbered listId that resumes after non-list content
    # restarts at 1 on the write side (no attach-to-list API).
    open_ordered: set[str] = set()
    interrupted_ordered: set[str] = set()

    seg = _SegmentBuilder(dest_tab_id)
    last_para_index = -1
    if _in_table_cell:
        for i in range(len(elements) - 1, -1, -1):
            if "paragraph" in elements[i]:
                last_para_index = i
                break

    def flush() -> None:
        nonlocal seg
        if not seg.is_empty():
            plan.phases.append(seg.build())
        seg = _SegmentBuilder(dest_tab_id)

    for i, elem in enumerate(elements):
        if "paragraph" in elem:
            para = elem["paragraph"]
            bullet = para.get("bullet")
            if bullet is not None:
                list_id = bullet.get("listId") or ""
                if list_id in interrupted_ordered:
                    report.count("list_numbering_restart")
                    interrupted_ordered.discard(list_id)
                if _bullet_preset_for(list_id, lists).startswith("NUMBERED"):
                    open_ordered.add(list_id)
            else:
                interrupted_ordered |= open_ordered
                open_ordered.clear()
            _plan_paragraph(
                seg,
                para,
                lists=lists,
                inline_objects=inline_objects,
                report=report,
                suppress_trailing_newline=(i == last_para_index),
                in_table=_in_table_cell,
            )
            plan.block_count += 1
        elif "table" in elem:
            interrupted_ordered |= open_ordered
            open_ordered.clear()
            flush()
            plan.phases.append(
                _plan_table(
                    elem["table"],
                    tab_id=dest_tab_id,
                    lists=lists,
                    inline_objects=inline_objects,
                    report=report,
                )
            )
            plan.block_count += 1
        elif "tableOfContents" in elem:
            _plan_toc_placeholder(seg, report)
            plan.block_count += 1
        elif "sectionBreak" in elem:
            # Slices come pre-filtered (the _detect_splits DocApp-index
            # contract excludes sectionBreaks); tolerate one anyway.
            report.count("multi_section")
        else:
            report.count("unknown_block")
    flush()
    return plan


def scan_source_fidelity(
    elements: list[dict], *, lists: dict, inline_objects: dict
) -> FidelityReport:
    """Preflight-only detection pass: what would the transplant drop or
    degrade for these elements? Runs the real planner against a
    throwaway tab id and discards the plan, so the warning list can
    never drift from what the walker actually does."""
    report = FidelityReport()
    plan_tab_transplant(
        elements,
        lists=lists,
        inline_objects=inline_objects,
        dest_tab_id="preflight",
        report=report,
    )
    return report


# ---------------------------------------------------------------------
# Executor - rebase + sync-point mechanics
# ---------------------------------------------------------------------

_INDEX_KEYS = frozenset({"index", "startIndex", "endIndex"})


def _rebase_value(value: Any, base: int) -> Any:
    if isinstance(value, dict):
        return {
            k: (v + base if k in _INDEX_KEYS and isinstance(v, int) else _rebase_value(v, base))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rebase_value(v, base) for v in value]
    return value


def _rebase_requests(requests: list[dict], base: int) -> list[dict]:
    """Shift every index/startIndex/endIndex in a relative request
    program by ``base``. Requests without index fields (updateNamedStyle,
    updateDocumentStyle) pass through untouched."""
    return [_rebase_value(r, base) for r in requests]


def _get_document(docs: Any, doc_id: str) -> dict:
    return execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.transplant",
    )


def _batch_update(
    docs: Any,
    doc_id: str,
    requests: list[dict],
    budget: _RateLimitBudget | None = None,
    governor: "_WriteGovernor | None" = None,
) -> None:
    # Content writes are single-shot for everything EXCEPT 429:
    # replaying a partially-applied mutation risks duplicate inserts
    # (same convention as make_doc_with_tabs / insert_markdown_table),
    # but a 429 was rejected before execution, so backing off and
    # re-sending the SAME chunk is safe (see the N2 block up top).
    #
    # Every SEND (first try and each 429 retry alike) first takes the
    # per-user governor slot, so concurrent jobs pace each other under
    # the shared quota instead of colliding into 429s. Governor waits
    # are ordinary pacing and consume no retry budget.
    if budget is None:
        budget = _RateLimitBudget()
    for start in range(0, len(requests), _MAX_REQUESTS_PER_BATCH):
        chunk = requests[start : start + _MAX_REQUESTS_PER_BATCH]
        while True:
            try:
                if governor is not None:
                    paced = governor.acquire()
                    if paced > 0.5:
                        _log.info(
                            "docs write paced %.1fs by the per-user "
                            "write governor for doc %s",
                            paced, doc_id,
                        )
                execute_with_retry(
                    lambda chunk=chunk: docs.documents().batchUpdate(
                        documentId=doc_id, body={"requests": chunk}
                    ).execute(),
                    idempotent=False,
                    op_name="docs.documents.batchUpdate.transplant",
                )
                break
            except HttpError as e:
                if not _is_rate_limit_error(e):
                    raise
                wait = budget.next_wait()
                if wait is None:
                    # Budget spent: fail into the existing keep-the-doc
                    # + completion-manifest path with the real error.
                    _log.warning(
                        "docs write rate limit persisted past the %ss "
                        "backoff budget for doc %s; giving up",
                        _RATE_LIMIT_WAIT_BUDGET_SECONDS, doc_id,
                    )
                    raise
                _log.info(
                    "docs write rate-limited (429) for doc %s; backing "
                    "off %.1fs (attempt %d, %.1fs budget left)",
                    doc_id, wait, budget.attempt, budget.remaining,
                )
                time.sleep(wait)


def _tab_body_content(document: dict, tab_id: str) -> list[dict]:
    tab = _find_tab_by_id(document.get("tabs") or [], tab_id)
    if tab is None:
        raise RuntimeError(f"tab {tab_id} not found in re-fetched document")
    return tab.get("documentTab", {}).get("body", {}).get("content", [])


def _append_base(document: dict, tab_id: str) -> int:
    """The insertion point for appending to a tab: just before the
    body's final implicit newline (same convention as append_to_tab)."""
    content = _tab_body_content(document, tab_id)
    if not content:
        return 1
    return content[-1]["endIndex"] - 1


def _find_table_at_or_after(content: list[dict], index: int) -> dict | None:
    """First table element (in document order, descending into cells for
    the nested case) whose startIndex >= index."""
    for element in content:
        table = element.get("table")
        if table is None:
            continue
        if element.get("startIndex", -1) >= index:
            return element
        for row in table.get("tableRows") or []:
            for cell in row.get("tableCells") or []:
                found = _find_table_at_or_after(cell.get("content") or [], index)
                if found is not None:
                    return found
    return None


def _cell_content_starts(table_element: dict) -> dict[tuple[int, int], int]:
    """(row, col) -> first content index of each cell of a FRESH table
    (full rows x columns grid, no merges yet, one empty paragraph per
    cell)."""
    starts: dict[tuple[int, int], int] = {}
    for r, row in enumerate(table_element["table"].get("tableRows") or []):
        for c, cell in enumerate(row.get("tableCells") or []):
            content = cell.get("content") or []
            if not content:
                raise RuntimeError(f"fresh table cell ({r},{c}) has no content")
            starts[(r, c)] = content[0]["startIndex"]
    return starts


def _table_style_requests(phase: TablePhase, table_start: int) -> list[dict]:
    start_loc = {"index": table_start, "tabId": phase.tab_id}
    requests: list[dict] = []
    for idx, props in phase.column_properties:
        requests.append(
            {
                "updateTableColumnProperties": {
                    "tableStartLocation": start_loc,
                    "columnIndices": [idx],
                    "tableColumnProperties": props,
                    "fields": ",".join(sorted(props)),
                }
            }
        )
    for r, style in phase.row_styles:
        requests.append(
            {
                "updateTableRowStyle": {
                    "tableStartLocation": start_loc,
                    "rowIndices": [r],
                    "tableRowStyle": style,
                    "fields": ",".join(sorted(style)),
                }
            }
        )
    if phase.pinned_header_rows > 0:
        requests.append(
            {
                "pinTableHeaderRows": {
                    "tableStartLocation": start_loc,
                    "pinnedHeaderRowsCount": phase.pinned_header_rows,
                }
            }
        )
    for r, c, style in phase.cell_styles:
        requests.append(
            {
                "updateTableCellStyle": {
                    "tableRange": {
                        "tableCellLocation": {
                            "tableStartLocation": start_loc,
                            "rowIndex": r,
                            "columnIndex": c,
                        },
                        "rowSpan": 1,
                        "columnSpan": 1,
                    },
                    "tableCellStyle": style,
                    "fields": ",".join(sorted(style)),
                }
            }
        )
    # Merges LAST: merging relocates cell boundaries, so every
    # cell-addressed fill/style above must land on the unmerged grid.
    for r, c, row_span, col_span in phase.merges:
        requests.append(
            {
                "mergeTableCells": {
                    "tableRange": {
                        "tableCellLocation": {
                            "tableStartLocation": start_loc,
                            "rowIndex": r,
                            "columnIndex": c,
                        },
                        "rowSpan": row_span,
                        "columnSpan": col_span,
                    }
                }
            }
        )
    return requests


def _execute_table_phase(
    docs: Any,
    doc_id: str,
    tab_id: str,
    phase: TablePhase,
    base: int,
    budget: _RateLimitBudget | None = None,
    governor: "_WriteGovernor | None" = None,
) -> int:
    """Create, fill, and style one table at ``base``; return the index
    just after the finished table (the next append position)."""
    if phase.rows < 1 or phase.columns < 1:
        return base
    _batch_update(
        docs,
        doc_id,
        [
            {
                "insertTable": {
                    "location": {"index": base, "tabId": tab_id},
                    "rows": phase.rows,
                    "columns": phase.columns,
                }
            }
        ],
        budget=budget,
        governor=governor,
    )
    # Sync-point: cell indices are server-assigned, so re-fetch and
    # locate the just-created table (first table at/after base).
    document = _get_document(docs, doc_id)
    table_element = _find_table_at_or_after(_tab_body_content(document, tab_id), base)
    if table_element is None:
        raise RuntimeError(
            f"transplant could not locate the table created at index {base} "
            f"in tab {tab_id}"
        )
    starts = _cell_content_starts(table_element)

    # Fill bottom-up: content inserted into a cell shifts only indices
    # AFTER that cell, and every higher-index cell is already done.
    for key in sorted(phase.cell_plans, key=lambda rc: starts.get(rc, 0), reverse=True):
        if key not in starts:
            raise RuntimeError(
                f"planned cell {key} missing from created {phase.rows}x"
                f"{phase.columns} table in tab {tab_id}"
            )
        _execute_phases(
            docs, doc_id, tab_id, phase.cell_plans[key].phases, starts[key],
            budget=budget, governor=governor,
        )

    style_requests = _table_style_requests(phase, table_element["startIndex"])
    if style_requests:
        _batch_update(
            docs, doc_id, style_requests, budget=budget, governor=governor
        )

    # Fills and merges moved the table's end; re-anchor from the live
    # document rather than trusting arithmetic.
    document = _get_document(docs, doc_id)
    table_element = _find_table_at_or_after(_tab_body_content(document, tab_id), base)
    if table_element is None:
        raise RuntimeError(
            f"transplant lost track of the table at index {base} in tab {tab_id}"
        )
    return table_element["endIndex"]


def _execute_phases(
    docs: Any,
    doc_id: str,
    tab_id: str,
    phases: list[SegmentPhase | TablePhase],
    base: int,
    budget: _RateLimitBudget | None = None,
    governor: "_WriteGovernor | None" = None,
) -> int:
    for phase in phases:
        if isinstance(phase, SegmentPhase):
            if phase.requests:
                _batch_update(
                    docs, doc_id, _rebase_requests(phase.requests, base),
                    budget=budget, governor=governor,
                )
            base += phase.length
        else:
            base = _execute_table_phase(
                docs, doc_id, tab_id, phase, base,
                budget=budget, governor=governor,
            )
    return base


def execute_tab_transplant(
    docs: Any,
    doc_id: str,
    dest_tab_id: str,
    plan: TabTransplantPlan,
    *,
    document: dict | None = None,
    governor_key: str | None = None,
) -> int:
    """Run a planned transplant against the live document.

    ``document`` may carry an already-fetched ``documents.get`` result
    (with tabs content) to spare the initial round-trip; table phases
    always re-fetch at their sync-points regardless. Returns the number
    of structural blocks written (the plan's ``block_count``).

    Writes hitting the per-user Docs rate limit (HTTP 429) back off and
    retry against a shared per-call budget (N2); exhaustion propagates
    the 429 into the caller's keep-the-doc failure handling.

    ``governor_key`` identifies whose write quota this transplant
    consumes (the Google user id; None = the operator). Every write is
    paced by that user's shared cross-job governor so concurrent
    converts stop competing each other into 429s (A2 root fix).
    """
    if document is None:
        document = _get_document(docs, doc_id)
    base = _append_base(document, dest_tab_id)
    budget = _RateLimitBudget()
    _execute_phases(
        docs, doc_id, dest_tab_id, plan.phases, base,
        budget=budget, governor=_governor_for(governor_key),
    )
    return plan.block_count


def verify_tab_transplant(
    document: dict, tab_id: str, plan: TabTransplantPlan
) -> None:
    """Assert the destination tab holds at least the planned block
    count (a batch that silently landed nowhere, or an omitted tabId
    that wrote into the wrong tab, fails HERE, before the source is
    carved). ``document`` is a fresh ``documents.get`` result."""
    content = _tab_body_content(document, tab_id)
    blocks = [e for e in content if "sectionBreak" not in e]
    if len(blocks) < plan.block_count:
        raise RuntimeError(
            f"transplant verification failed for tab {tab_id}: expected at "
            f"least {plan.block_count} structural blocks, found {len(blocks)}"
        )


def carve_source_ranges(
    docs: Any,
    doc_id: str,
    tab_id: str,
    docapp_children: list[dict],
    ranges: list[tuple[int, int]],
    governor_key: str | None = None,
) -> None:
    """Delete the transplanted element ranges from the source tab.

    Runs strictly AFTER the new tabs are built and verified (the
    transactional contract: source content is untouched until the
    copies are proven). ``ranges`` are inclusive (lo, hi) indices into
    ``docapp_children`` (the sectionBreak-filtered body list whose
    elements still carry their true startIndex/endIndex). Deletions are
    emitted bottom-up so earlier spans stay valid, and the final span
    is capped one unit short of the body end because the last paragraph
    mark of a body cannot be deleted.
    """
    if not docapp_children:
        return
    body_end = docapp_children[-1]["endIndex"]
    spans: list[tuple[int, int]] = []
    for lo, hi in ranges:
        start = docapp_children[lo]["startIndex"]
        end = min(docapp_children[hi]["endIndex"], body_end - 1)
        if end > start:
            spans.append((start, end))
    requests = [
        {
            "deleteContentRange": {
                "range": {"startIndex": start, "endIndex": end, "tabId": tab_id}
            }
        }
        for start, end in sorted(spans, reverse=True)
    ]
    if requests:
        # Own 429 budget: the carve is one bounded write burst and its
        # failure path (placeholder warnings / keep semantics) is the
        # caller's, so it should not drain a transplant's allowance.
        # Same per-user governor as the transplant writes, though - the
        # quota does not care which phase a write belongs to.
        _batch_update(
            docs, doc_id, requests,
            budget=_RateLimitBudget(),
            governor=_governor_for(governor_key),
        )
