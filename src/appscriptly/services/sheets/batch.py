"""Reusable Sheets ``spreadsheets.batchUpdate`` request-builder.

This module is the **typed seam** the Sheets service was missing: a
small set of pure functions that compose valid ``batchUpdate`` request
objects (the entries that go inside ``body={"requests": [...]}``), plus
one dispatcher that actually issues the call. Every future Sheets
write-feature (cell formatting, conditional formatting, charts, pivots,
sheet-lifecycle, dimension ops, ...) layers on top of these builders
instead of hand-rolling raw request dicts at each call site.

**Why this exists (the stale "no precedent" rationale).** The original
``services/sheets/`` docstring deferred ``batchUpdate`` because it
"has no precedent in the foundation". That rationale is stale:
``batchUpdate`` already ships in BOTH the docs service
(``services/docs/api.py`` → ``documents().batchUpdate``) and the slides
service (``services/slides/api.py::replace_all_text`` →
``presentations().batchUpdate``). This module mirrors that proven
pattern — pure request builders + an ``execute_with_retry``-wrapped
dispatch — rather than inventing a new abstraction.

**The shape, mirrored from docs/slides:**

    builders (pure, no I/O)            dispatcher (one API call)
    -----------------------            -------------------------
    grid_range(...)            -.
    repeat_cell_request(...)     |->  batch_update(creds, sid, requests)
    add_conditional_..._request -'      -> spreadsheets().batchUpdate

The builders return plain ``dict`` request objects and never touch the
network, so they are unit-testable in isolation (no creds, no stubs —
just dict-assembly assertions). The dispatcher is the ONLY function
here that calls Google, and it goes through the ``get_service`` /
``execute_with_retry`` chokepoints like every other Sheets call.

**Idempotency / retry safety.** ``batch_update`` defaults to
``idempotent=False`` — a *generic* batch of arbitrary request types
cannot be blanket-retried (a partially-applied mutating batch replayed
could duplicate inserts, shift dimensions twice, etc.; this matches the
``execute_with_retry`` "safety floor" contract). Callers that KNOW
their specific batch is idempotent (e.g. a pure ``repeatCell`` format,
which produces the same cell state no matter how many times it runs)
opt in by passing ``idempotent=True``.

**Coordinate model.** Sheets identifies a rectangular block with a
``GridRange`` — ``{sheetId, startRowIndex, endRowIndex, startColumnIndex,
endColumnIndex}`` — using **0-based, half-open** indices (``end`` is
exclusive, like a Python slice). This is intentionally NOT A1 notation:
the ``batchUpdate`` request types operate on GridRanges, while the
``values.*`` endpoints (``read_range`` / ``write_range``) use A1. The
``grid_range`` helper centralises the GridRange assembly + index
validation so every builder shares one correct coordinate constructor.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Horizontal-alignment values Sheets accepts for a CellFormat. Pinned
# as a module constant so the builder can validate client-side (a typo
# like ``"centre"`` otherwise bounces off a generic Google 400).
_HORIZONTAL_ALIGNMENTS = frozenset({"LEFT", "CENTER", "RIGHT"})


# ---------------------------------------------------------------------
# Coordinate + style primitives (pure, no I/O)
# ---------------------------------------------------------------------


def grid_range(
    sheet_id: int,
    *,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
) -> dict[str, Any]:
    """Build a Sheets ``GridRange`` (0-based, half-open indices).

    A GridRange is the rectangular-block coordinate object every
    ``batchUpdate`` request type that targets cells uses (``repeatCell``,
    ``addConditionalFormatRule``, ``mergeCells``, ``updateBorders``, ...).

    Args:
        sheet_id: The numeric sheet (tab) id — the ``gid``, NOT the
            spreadsheet id and NOT the tab name. The first/default tab
            is ``0``. Obtainable from ``spreadsheets.get`` →
            ``sheets[].properties.sheetId``.
        start_row: 0-based inclusive first row. ``None`` (default) means
            "from the top" — Sheets treats an omitted ``startRowIndex``
            as unbounded above.
        end_row: 0-based EXCLUSIVE last row (half-open, like a Python
            slice: ``start_row=0, end_row=3`` covers rows 1-3 in the
            UI). ``None`` means "to the bottom".
        start_col: 0-based inclusive first column (``0`` = column A).
            ``None`` means "from the left".
        end_col: 0-based EXCLUSIVE last column. ``None`` means "to the
            right edge".

    Returns:
        A ``GridRange`` dict carrying ``sheetId`` plus only the bounds
        that were supplied (omitted bounds are left out so Sheets
        applies its unbounded default — a fully-omitted range targets
        the WHOLE sheet, which is valid and useful for e.g. formatting
        every cell).

    Raises:
        ValueError: a negative index, or a non-positive span where
            ``end <= start`` for either axis (an empty/inverted range
            is always a caller bug — Sheets would either no-op silently
            or 400 with a worse message).
    """
    for name, value in (
        ("start_row", start_row),
        ("end_row", end_row),
        ("start_col", start_col),
        ("end_col", end_col),
    ):
        if value is not None and value < 0:
            raise ValueError(
                f"{name} must be >= 0 (GridRange indices are 0-based); "
                f"got {value}."
            )
    if start_row is not None and end_row is not None and end_row <= start_row:
        raise ValueError(
            f"end_row ({end_row}) must be > start_row ({start_row}) — "
            f"GridRange is half-open, so end is exclusive."
        )
    if start_col is not None and end_col is not None and end_col <= start_col:
        raise ValueError(
            f"end_col ({end_col}) must be > start_col ({start_col}) — "
            f"GridRange is half-open, so end is exclusive."
        )

    gr: dict[str, Any] = {"sheetId": sheet_id}
    if start_row is not None:
        gr["startRowIndex"] = start_row
    if end_row is not None:
        gr["endRowIndex"] = end_row
    if start_col is not None:
        gr["startColumnIndex"] = start_col
    if end_col is not None:
        gr["endColumnIndex"] = end_col
    return gr


def color(
    red: float = 0.0,
    green: float = 0.0,
    blue: float = 0.0,
) -> dict[str, float]:
    """Build a Sheets ``Color`` from 0.0-1.0 RGB components.

    Sheets colors are floats in ``[0, 1]`` (NOT 0-255 ints). This helper
    centralises the conversion guard so a ``255``-style mistake is
    caught client-side rather than producing an out-of-gamut color.

    Args:
        red / green / blue: Channel intensities, each in ``[0.0, 1.0]``.

    Returns:
        A ``{"red", "green", "blue"}`` dict suitable for any Sheets
        field that takes a ``Color`` (foreground/background, border
        color, conditional-format color, ...).

    Raises:
        ValueError: any channel outside ``[0.0, 1.0]`` (the most common
            cause is passing 0-255 ints).
    """
    for name, value in (("red", red), ("green", green), ("blue", blue)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"{name} must be in [0.0, 1.0] (Sheets colors are floats, "
                f"not 0-255 ints); got {value}."
            )
    return {"red": red, "green": green, "blue": blue}


def cell_format(
    *,
    bold: bool | None = None,
    italic: bool | None = None,
    font_size: int | None = None,
    foreground_color: dict[str, float] | None = None,
    background_color: dict[str, float] | None = None,
    horizontal_alignment: str | None = None,
    number_format: str | None = None,
) -> dict[str, Any]:
    """Build a Sheets ``CellFormat`` from a flat set of style options.

    Composes the nested ``CellFormat`` structure (``textFormat`` for
    bold/italic/size/color, plus top-level ``backgroundColor`` /
    ``horizontalAlignment`` / ``numberFormat``) from flat kwargs so
    callers don't have to memorise Sheets' nesting. Only the options
    that are explicitly supplied appear in the result — this keeps the
    field mask (see ``repeat_cell_request``) minimal and avoids
    clobbering unrelated existing formatting.

    Args:
        bold / italic: Text weight / slant toggles.
        font_size: Point size for the text run.
        foreground_color: Text color — pass a ``color(...)`` dict.
        background_color: Cell fill color — pass a ``color(...)`` dict.
        horizontal_alignment: One of ``LEFT`` / ``CENTER`` / ``RIGHT``.
        number_format: A Sheets number-format pattern string (e.g.
            ``"#,##0.00"``, ``"0.00%"``, ``"$#,##0"``, ``"yyyy-mm-dd"``).
            Applied as a ``NUMBER``-type format.

    Returns:
        A ``CellFormat`` dict containing only the supplied options.
        Returns an empty dict if nothing was supplied (the caller is
        responsible for treating an empty format as a no-op).

    Raises:
        ValueError: ``horizontal_alignment`` not in the allowed set, or
            ``font_size`` <= 0.
    """
    if (
        horizontal_alignment is not None
        and horizontal_alignment not in _HORIZONTAL_ALIGNMENTS
    ):
        raise ValueError(
            f"horizontal_alignment must be one of "
            f"{sorted(_HORIZONTAL_ALIGNMENTS)}; got "
            f"{horizontal_alignment!r}."
        )
    if font_size is not None and font_size <= 0:
        raise ValueError(f"font_size must be > 0; got {font_size}.")

    text_format: dict[str, Any] = {}
    if bold is not None:
        text_format["bold"] = bold
    if italic is not None:
        text_format["italic"] = italic
    if font_size is not None:
        text_format["fontSize"] = font_size
    if foreground_color is not None:
        text_format["foregroundColor"] = foreground_color

    fmt: dict[str, Any] = {}
    if text_format:
        fmt["textFormat"] = text_format
    if background_color is not None:
        fmt["backgroundColor"] = background_color
    if horizontal_alignment is not None:
        fmt["horizontalAlignment"] = horizontal_alignment
    if number_format is not None:
        fmt["numberFormat"] = {"type": "NUMBER", "pattern": number_format}
    return fmt


def _format_field_mask(fmt: dict[str, Any]) -> str:
    """Derive a ``repeatCell`` field mask from a ``CellFormat`` dict.

    ``repeatCell`` requires a ``fields`` mask naming exactly which
    ``userEnteredFormat`` sub-fields to overwrite; anything not in the
    mask is left untouched. Building the mask FROM the format dict (vs
    a blunt ``"userEnteredFormat"``) means a partial format only
    touches the fields it actually sets — e.g. setting just ``bold``
    won't wipe an existing background color.

    Returns a comma-joined mask string rooted at ``userEnteredFormat``
    (e.g. ``"userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor"``).
    """
    paths: list[str] = []
    text_format = fmt.get("textFormat")
    if isinstance(text_format, dict):
        for sub in text_format:
            paths.append(f"userEnteredFormat.textFormat.{sub}")
    for top in ("backgroundColor", "horizontalAlignment", "numberFormat"):
        if top in fmt:
            paths.append(f"userEnteredFormat.{top}")
    return ",".join(paths)


# ---------------------------------------------------------------------
# Request builders (pure — return entries for body["requests"])
# ---------------------------------------------------------------------


def repeat_cell_request(
    grid: dict[str, Any],
    fmt: dict[str, Any],
) -> dict[str, Any]:
    """Build a ``repeatCell`` request that applies ``fmt`` across ``grid``.

    ``repeatCell`` stamps a single ``CellData`` (here, just a
    ``userEnteredFormat``) onto every cell in a ``GridRange`` — the
    canonical way to format a block (bold a header row, currency-format
    a column, shade a region). The ``fields`` mask is derived from the
    format so unrelated existing formatting in the range is preserved.

    Args:
        grid: A ``GridRange`` (build with ``grid_range``).
        fmt: A ``CellFormat`` (build with ``cell_format``).

    Returns:
        A single ``{"repeatCell": {...}}`` request dict, ready to drop
        into the ``requests`` list passed to ``batch_update``.

    Raises:
        ValueError: ``fmt`` is empty — a ``repeatCell`` with an empty
            field mask is a no-op that Sheets rejects; reject it
            client-side with a clearer message.
    """
    mask = _format_field_mask(fmt)
    if not mask:
        raise ValueError(
            "fmt is empty — repeat_cell_request needs at least one "
            "format option set (build it with cell_format(bold=..., "
            "background_color=..., etc.))."
        )
    return {
        "repeatCell": {
            "range": grid,
            "cell": {"userEnteredFormat": fmt},
            "fields": mask,
        }
    }


def add_conditional_format_rule_request(
    grid: dict[str, Any],
    *,
    condition_type: str,
    values: list[str] | None = None,
    background_color: dict[str, float] | None = None,
    bold: bool | None = None,
    index: int = 0,
) -> dict[str, Any]:
    """Build an ``addConditionalFormatRule`` request (a BooleanRule).

    Demonstrates that the builder seam generalises beyond the one
    formatting request type: a conditional-format rule is a DIFFERENT
    ``batchUpdate`` request shape (rule + ranges + boolean condition +
    a format to apply when the condition is true), yet it reuses the
    same ``grid_range`` / ``color`` / ``CellFormat`` primitives.

    Args:
        grid: The ``GridRange`` the rule applies to.
        condition_type: A Sheets ``ConditionType`` enum value, e.g.
            ``"NUMBER_GREATER"``, ``"TEXT_CONTAINS"``,
            ``"NUMBER_BETWEEN"``, ``"CUSTOM_FORMULA"``. Passed through
            verbatim (the enum set is large + Google-versioned, so this
            builder doesn't hardcode it — an invalid value fails at the
            API with Google's own enum error).
        values: The condition's comparison value(s) as strings (e.g.
            ``["100"]`` for ``NUMBER_GREATER``, ``["10", "20"]`` for
            ``NUMBER_BETWEEN``). Omit for conditions that take no value
            (e.g. ``BLANK``).
        background_color: Fill to apply when the condition matches —
            pass a ``color(...)`` dict. At least one of
            ``background_color`` / ``bold`` must be set.
        bold: When set, bold the text of matching cells.
        index: Insertion priority among existing rules (0 = highest
            priority, evaluated first).

    Returns:
        A single ``{"addConditionalFormatRule": {...}}`` request dict.

    Raises:
        ValueError: neither ``background_color`` nor ``bold`` supplied
            (a rule with no format to apply does nothing).
    """
    fmt = cell_format(bold=bold, background_color=background_color)
    if not fmt:
        raise ValueError(
            "a conditional-format rule needs a format to apply on match "
            "— set background_color and/or bold."
        )

    condition: dict[str, Any] = {"type": condition_type}
    if values:
        condition["values"] = [{"userEnteredValue": v} for v in values]

    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [grid],
                "booleanRule": {
                    "condition": condition,
                    "format": fmt,
                },
            },
            "index": index,
        }
    }


# ---------------------------------------------------------------------
# Sheet-lifecycle request builders (tab add / delete / rename)
# ---------------------------------------------------------------------
#
# These operate on a whole SHEET (tab), not a cell GridRange, so they
# don't use ``grid_range``. They are the ``batchUpdate`` request types
# behind ``gsheets_add_sheet`` / ``gsheets_delete_sheet`` /
# ``gsheets_rename_sheet`` — closing the "create makes only one tab" gap.


def add_sheet_request(
    title: str,
    *,
    index: int | None = None,
) -> dict[str, Any]:
    """Build an ``addSheet`` request that creates a new tab.

    ``addSheet`` adds a sheet (tab) to an existing spreadsheet. Sheets
    assigns the new tab's ``sheetId`` (gid) server-side and echoes it in
    the batchUpdate reply (``replies[i].addSheet.properties.sheetId``),
    so the api layer can surface the gid for follow-up calls.

    Args:
        title: The new tab's name. Must be unique within the
            spreadsheet — Sheets 400s on a duplicate tab name (caught
            server-side; we reject blank client-side).
        index: 0-based position among existing tabs (0 = leftmost).
            ``None`` (default) appends after the last tab.

    Returns:
        A single ``{"addSheet": {...}}`` request dict.

    Raises:
        ValueError: blank ``title``, or a negative ``index``.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty (the new tab needs a name).")
    if index is not None and index < 0:
        raise ValueError(f"index must be >= 0 (0 = leftmost tab); got {index}.")

    properties: dict[str, Any] = {"title": title.strip()}
    if index is not None:
        properties["index"] = index
    return {"addSheet": {"properties": properties}}


def delete_sheet_request(sheet_id: int) -> dict[str, Any]:
    """Build a ``deleteSheet`` request that removes a tab by its gid.

    Args:
        sheet_id: The numeric sheet (tab) id — the ``gid``, NOT the tab
            name and NOT the spreadsheet id. Obtainable from
            ``spreadsheets.get`` → ``sheets[].properties.sheetId`` or
            the tab's URL ``#gid=...``.

    Returns:
        A single ``{"deleteSheet": {...}}`` request dict.

    Raises:
        ValueError: a negative ``sheet_id`` (gids are non-negative;
            a negative value is always a caller bug — Sheets would 400
            with a worse message). Note: deleting the LAST remaining
            sheet is rejected by Sheets server-side (a spreadsheet must
            keep at least one tab), surfaced as an HttpError.
    """
    if sheet_id < 0:
        raise ValueError(
            f"sheet_id must be >= 0 (a sheet gid is non-negative); "
            f"got {sheet_id}."
        )
    return {"deleteSheet": {"sheetId": sheet_id}}


def update_sheet_title_request(
    sheet_id: int,
    title: str,
) -> dict[str, Any]:
    """Build an ``updateSheetProperties`` request that renames a tab.

    Renames a single tab via ``updateSheetProperties`` with a ``fields``
    mask scoped to exactly ``title`` — so no other sheet property
    (index, gridProperties, tabColor, ...) is touched.

    Args:
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.
        title: The new tab name. Must be unique within the spreadsheet
            (Sheets 400s on a duplicate, server-side); blank rejected
            client-side.

    Returns:
        A single ``{"updateSheetProperties": {...}}`` request dict with
        a ``fields="title"`` mask.

    Raises:
        ValueError: blank ``title`` or a negative ``sheet_id``.
    """
    if sheet_id < 0:
        raise ValueError(
            f"sheet_id must be >= 0 (a sheet gid is non-negative); "
            f"got {sheet_id}."
        )
    if not title or not title.strip():
        raise ValueError("title cannot be empty (the tab needs a new name).")
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "title": title.strip()},
            "fields": "title",
        }
    }


# ---------------------------------------------------------------------
# Dispatcher (the ONLY function here that calls Google)
# ---------------------------------------------------------------------


def batch_update(
    creds: Credentials,
    spreadsheet_id: str,
    requests: list[dict[str, Any]],
    *,
    idempotent: bool = False,
    op_name: str = "sheets.spreadsheets.batchUpdate",
) -> dict[str, Any]:
    """Dispatch a list of request dicts via ``spreadsheets.batchUpdate``.

    The single API-touching function in this module. Mirrors the
    docs/slides precedent exactly: wrap ``spreadsheets().batchUpdate``
    in ``execute_with_retry`` and return a flat envelope. Every
    higher-level Sheets write-tool composes its ``requests`` from the
    pure builders above, then hands them here.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        requests: A non-empty list of ``batchUpdate`` request dicts
            (each typically produced by a ``*_request`` builder above).
        idempotent: Whether this specific batch is safe to retry on a
            transient 429/5xx. Defaults to ``False`` — a generic batch
            of arbitrary request types must NOT be blanket-retried (a
            partially-applied mutating batch replayed could duplicate
            its effect). Callers pass ``True`` only when they KNOW the
            batch is idempotent (e.g. a pure ``repeatCell`` format).
        op_name: Telemetry label for the retry layer.

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — ``replies`` is
        the raw per-request reply list Sheets returns (one entry per
        request, in order; entries are ``{}`` for request types that
        produce no reply). ``total_requests`` echoes how many requests
        were sent so callers can confirm the batch size.

    Raises:
        ValueError: ``requests`` is empty (an empty batch is a caller
            bug — Sheets would 400).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated to
            the tool-layer envelope.
    """
    if not requests:
        raise ValueError(
            "requests cannot be empty — pass at least one batchUpdate "
            "request dict (build them with the *_request helpers in "
            "services/sheets/batch.py)."
        )

    sheets = get_service("sheets", "v4", credentials=creds)
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute(),
        idempotent=idempotent,
        op_name=op_name,
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "total_requests": len(requests),
        "replies": resp.get("replies", []),
    }
