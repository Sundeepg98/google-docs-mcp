"""Google Sheets REST wrapper.

The range-shaped surface:

  * ``read_range``  — ``spreadsheets.values.get``
  * ``write_range`` — ``spreadsheets.values.update``
  * ``create_spreadsheet`` — ``spreadsheets.create`` (creates an
    empty sheet so the read/write tools have something to target
    in a single-call workflow; pure API call, no schema acrobatics)

The ``batchUpdate`` surface:

  * ``format_range`` — ``spreadsheets.batchUpdate`` with a
    ``repeatCell`` request, composed via the reusable request-builder
    in ``services/sheets/batch.py``.

The ``batchUpdate`` tagged-union (40+ request types: formatting,
charts, pivots, named ranges, conditional formats, etc.) was
originally deferred ("Sheets — pattern stretch. batchUpdate has no
precedent in the foundation"). That rationale is now stale: the
``services/sheets/batch.py`` request-builder generalises the pattern
that docs + slides already proved, so new ``batchUpdate``-backed
features layer on top of those pure builders instead of hand-rolling
raw request dicts. ``format_range`` is the first operation wired
through that seam; further request types (conditional formatting,
charts, dimensions, sheet-lifecycle) reuse the same builders.

**Scope note.** Calls require
``https://www.googleapis.com/auth/spreadsheets`` in the OAuth
consent. This scope was added to ``auth.SCOPES`` and
``oauth_google.GOOGLE_API_SCOPES`` in v2.3.1; existing user grants
get the new scope automatically on next token refresh via Google's
``include_granted_scopes=true`` flow (same incremental-consent
pattern that handled the ``drive.readonly`` and Apps Script scope
additions in earlier PRs).

**Value-input semantics.** ``write_range`` / ``append_rows`` default to
``valueInputOption="USER_ENTERED"`` — Sheets interprets values as if the
user typed them in the UI: ``"=SUM(A1:A10)"`` becomes a formula,
``"1/2"`` becomes a date, etc. That is the right default for "write
these values", but it silently turns a literal leading ``=`` (or a
date-looking string) into a formula/date. Callers that need values
stored EXACTLY as given pass ``value_input_option="RAW"`` — Sheets then
stores every cell as the literal it received (a leading ``=`` stays the
five characters ``"=1+1"``, not the number ``2``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service
from appscriptly.services.sheets.batch import (
    add_chart_request,
    add_conditional_format_rule_request,
    add_protected_range_request,
    add_sheet_request,
    batch_update,
    cell_format,
    color,
    delete_dimension_request,
    delete_sheet_request,
    duplicate_sheet_request,
    freeze_request,
    grid_range,
    insert_dimension_request,
    merge_cells_request,
    repeat_cell_request,
    set_data_validation_request,
    update_sheet_title_request,
)

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Default range read when the caller doesn't specify one. ``A1:Z1000``
# covers a reasonable starting workspace — 26 columns x 1000 rows.
# Sheets caps a single ``values.get`` at the spreadsheet's used range
# anyway, so an oversized default doesn't waste bandwidth.
DEFAULT_RANGE = "A1:Z1000"


# Sheets ``valueInputOption`` values the write tools accept. ``USER_ENTERED``
# (the default) parses values as if typed in the UI (formulas/dates/numbers);
# ``RAW`` stores every value as the literal string it received (a leading
# ``=`` stays literal text rather than becoming a formula). ``INPUT_VALUE_
# OPTION_UNSPECIFIED`` is excluded — it's a sentinel Sheets rejects on write.
_VALUE_INPUT_OPTIONS = frozenset({"USER_ENTERED", "RAW"})


def _check_value_input_option(value_input_option: str) -> None:
    """Reject an unknown ``valueInputOption`` client-side.

    Pinned client-side so a typo (``"raw"``, ``"USER"``) surfaces a clear
    message naming the two valid options rather than bouncing off a generic
    Google 400.
    """
    if value_input_option not in _VALUE_INPUT_OPTIONS:
        raise ValueError(
            f"value_input_option must be one of "
            f"{sorted(_VALUE_INPUT_OPTIONS)} (USER_ENTERED parses formulas/"
            f"dates as typed; RAW stores values literally); got "
            f"{value_input_option!r}."
        )


def _check_read_ranges(ranges: list[str]) -> None:
    """Reject an empty or malformed ``ranges`` list for a batch read.

    Pinned client-side so a bad batch (no ranges, or a blank / non-string
    entry) fails cheaply with a message NAMING the offending index rather
    than issuing a doomed ``values.batchGet`` that bounces off a generic
    Google 400. Runs before any Sheets call, so a rejected list costs zero
    API round-trips.
    """
    if not isinstance(ranges, list):
        raise ValueError(
            f"ranges must be a list of A1-notation strings; got "
            f"{type(ranges).__name__}."
        )
    if not ranges:
        raise ValueError(
            'ranges cannot be empty, pass at least one A1-notation range '
            '(e.g. ["A1:B2", "Sheet2!C1:D5"]).'
        )
    for i, r in enumerate(ranges):
        if not isinstance(r, str) or not r.strip():
            raise ValueError(
                f"ranges[{i}] must be a non-empty A1-notation string; got "
                f"{r!r}."
            )


def _check_write_data(data: list[dict]) -> None:
    """Reject an empty or malformed ``data`` list for a batch write.

    Each entry must be a ``{range, values}`` dict with a non-empty
    A1-notation ``range`` and a non-empty 2D row-major ``values`` list.
    Pinned client-side so a malformed batch fails cheaply with a message
    NAMING the offending index (and its range, when known) rather than
    bouncing off a generic Google 400. Runs before any Sheets call, so a
    rejected batch costs zero API round-trips.
    """
    if not isinstance(data, list):
        raise ValueError(
            f"data must be a list of {{range, values}} dicts; got "
            f"{type(data).__name__}."
        )
    if not data:
        raise ValueError(
            "data cannot be empty, pass at least one {range, values} entry."
        )
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(
                f"data[{i}] must be a {{range, values}} dict; got "
                f"{type(item).__name__}."
            )
        rng = item.get("range")
        if not isinstance(rng, str) or not rng.strip():
            raise ValueError(
                f"data[{i}]['range'] must be a non-empty A1-notation "
                f"string; got {rng!r}."
            )
        values = item.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError(
                f"data[{i}]['values'] (for range {rng!r}) must be a "
                f"non-empty 2D row-major list; got {values!r}."
            )
        if not all(isinstance(row, list) for row in values):
            raise ValueError(
                f"data[{i}]['values'] (for range {rng!r}) must be a list "
                f"of lists (2D row-major); got element types "
                f"{sorted({type(row).__name__ for row in values})}."
            )


def read_range(
    creds: Credentials,
    spreadsheet_id: str,
    range_str: str = DEFAULT_RANGE,
) -> dict:
    """Read cell values from a range via ``spreadsheets.values.get``.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID (the ID part of the URL,
            not the gid).
        range_str: An A1-notation range. Either ``"A1:Z1000"`` (whole
            default tab) or ``"Sheet2!B2:D10"`` (named tab + range).
            Defaults to ``DEFAULT_RANGE``.

    Returns:
        ``{range, values: [[...], ...]}``. ``values`` is a 2D row-major
        list (Sheets returns rows top-to-bottom, cells left-to-right).
        Empty cells at the END of a row are TRIMMED by the Sheets API
        (rows don't pad to the requested width); empty rows at the END
        of the range are similarly omitted. Consumers iterating
        rectangularly should pad client-side.

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx — let it
            propagate; the tool-layer envelope renders it.
    """
    sheets = get_service("sheets", "v4", credentials=creds)
    # PR-Δ3.5: gsheets_read_range is readonly=True, idempotent=True;
    # wrap to retry on 429/5xx.
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_str,
        ).execute(),
        idempotent=True,
        op_name="sheets.values.get",
    )
    return {
        "range": resp.get("range", range_str),
        "values": resp.get("values", []),
    }


def write_range(
    creds: Credentials,
    spreadsheet_id: str,
    range_str: str,
    values: list[list[Any]],
    *,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Write 2D values to a range via ``spreadsheets.values.update``.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        range_str: An A1-notation range — anchor cell or full block.
            If shorter than ``values``, Sheets writes only the slice
            that fits; longer ranges leave the unwritten cells alone
            (NOT cleared). Pass exactly the dimensions you want.
        values: 2D row-major list of cell values. Each inner list is
            one row, left-to-right. Strings / numbers / bools all
            permitted. ``None`` writes a blank cell.
        value_input_option: How Sheets interprets the written values.
            ``"USER_ENTERED"`` (default) parses them as if typed in the
            UI — ``"=SUM(A1:A2)"`` becomes a formula, ``"1/2"`` a date,
            ``"42"`` a number. ``"RAW"`` stores every value as the
            literal it received — a leading ``=`` stays literal text
            (no formula), a date-looking string stays a string. Use
            ``RAW`` when the data must round-trip exactly as given.

    Returns:
        ``{updated_range, updated_cells}`` — ``updated_range`` is the
        A1 range Sheets actually wrote into (echoed back so callers
        can confirm), ``updated_cells`` is the count Sheets reports
        it changed (may differ from ``len(values) * max-row-len`` if
        the requested range was smaller than the values block).

    Raises:
        ValueError: ``values`` empty or not a list of lists, or an
            unknown ``value_input_option``. Cheap client-side
            rejection — Sheets returns a 400 with a less helpful
            message.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not values:
        raise ValueError(
            "values cannot be empty — pass at least one row "
            "(use [[]] for a single blank row)."
        )
    if not all(isinstance(row, list) for row in values):
        raise ValueError(
            "values must be a list of lists (2D row-major). "
            f"Got element types: {sorted({type(r).__name__ for r in values})}"
        )
    _check_value_input_option(value_input_option)

    sheets = get_service("sheets", "v4", credentials=creds)
    # PR-Δ3.5: gsheets_write_range is annotated idempotent=True (writing
    # the same values to the same range twice is a no-op assuming the
    # caller passes the same values). Wrap to retry on 429/5xx.
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_str,
            valueInputOption=value_input_option,
            body={"values": values},
        ).execute(),
        idempotent=True,
        op_name="sheets.values.update",
    )
    return {
        "updated_range": resp.get("updatedRange", range_str),
        "updated_cells": resp.get("updatedCells", 0),
    }


def batch_read(
    creds: Credentials,
    spreadsheet_id: str,
    ranges: list[str],
) -> dict:
    """Read several disjoint ranges in ONE round-trip via ``values.batchGet``.

    The batched counterpart to ``read_range``. Reading N disjoint ranges
    with ``read_range`` costs N round-trips; ``batch_read`` fetches them
    all in a single ``spreadsheets.values.batchGet`` call and returns one
    ``value_ranges`` entry per requested range, in the SAME order.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        ranges: A non-empty list of A1-notation ranges, each either a
            whole-tab / block form (``"A1:B2"``) or a tab-prefixed form
            (``"Sheet2!C1:D5"``). Every entry must be a non-empty string;
            an empty list or a blank / non-string entry is rejected
            client-side (see Raises).

    Returns:
        ``{spreadsheet_id, value_ranges}`` where ``value_ranges`` is a
        list of ``{range, values}`` dicts, one per requested range in
        order. ``values`` is a 2D row-major list (Sheets trims trailing
        empty cells / rows, same as ``read_range``); a fully-blank range
        yields ``values: []``.

    Raises:
        ValueError: ``ranges`` empty, not a list, or containing a blank /
            non-string entry (named by index). Rejected before any API
            call, so a bad list costs zero round-trips.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.

    Note:
        Dispatched with ``idempotent=True`` (a read has no side effects),
        so it is safe to retry on a transient 429 / 5xx.
    """
    _check_read_ranges(ranges)

    sheets = get_service("sheets", "v4", credentials=creds)
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
        ).execute(),
        idempotent=True,
        op_name="sheets.values.batchGet",
    )
    return {
        "spreadsheet_id": resp.get("spreadsheetId", spreadsheet_id),
        "value_ranges": [
            {
                "range": vr.get("range", ""),
                "values": vr.get("values", []),
            }
            for vr in resp.get("valueRanges", [])
        ],
    }


def batch_write(
    creds: Credentials,
    spreadsheet_id: str,
    data: list[dict],
    *,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Write several disjoint ranges in ONE call via ``values.batchUpdate``.

    The batched counterpart to ``write_range``. Writing N disjoint ranges
    with ``write_range`` costs N round-trips; ``batch_write`` sends them
    all in a single ``spreadsheets.values.batchUpdate`` call (the VALUES
    batch endpoint, distinct from the ``spreadsheets.batchUpdate``
    request-builder used for structural / formatting mutations).

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        data: A non-empty list of ``{"range": <A1 str>, "values": <2D
            list>}`` dicts, one per range to write. Each ``range`` is an
            anchor cell or full block (Sheets writes only the slice that
            fits, same rule as ``write_range``); each ``values`` is a
            non-empty 2D row-major list. Malformed entries are rejected
            client-side (see Raises).
        value_input_option: How Sheets interprets EVERY range's values.
            ``"USER_ENTERED"`` (default) parses them as if typed in the UI
            (``"=SUM(A1:A2)"`` becomes a formula, ``"1/2"`` a date);
            ``"RAW"`` stores every value as the literal it received, so a
            leading ``=`` stays plain text. Applies to the whole batch.

    Returns:
        ``{spreadsheet_id, total_updated_cells, total_updated_ranges,
        responses}`` where ``responses`` is a list of ``{updated_range,
        updated_cells}`` dicts (one per written range, echoing what Sheets
        reports it wrote) and ``total_updated_ranges`` is that list's
        length.

    Raises:
        ValueError: ``data`` empty / not a list, an entry that is not a
            ``{range, values}`` dict, a blank / non-string ``range``, a
            ``values`` that is empty or not a list of lists, or an unknown
            ``value_input_option``. Each names the offending index.
            Rejected before any API call, so a bad batch costs zero
            round-trips.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.

    Note:
        Dispatched with ``idempotent=True`` (re-writing the same values to
        the same ranges yields the same cells, same reasoning as
        ``write_range``), so it is safe to retry on a transient 429 / 5xx.
    """
    _check_write_data(data)
    _check_value_input_option(value_input_option)

    sheets = get_service("sheets", "v4", credentials=creds)
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": value_input_option,
                "data": [
                    {"range": item["range"], "values": item["values"]}
                    for item in data
                ],
            },
        ).execute(),
        idempotent=True,
        op_name="sheets.values.batchUpdate",
    )
    responses = [
        {
            "updated_range": r.get("updatedRange", ""),
            "updated_cells": r.get("updatedCells", 0),
        }
        for r in resp.get("responses", [])
    ]
    return {
        "spreadsheet_id": resp.get("spreadsheetId", spreadsheet_id),
        "total_updated_cells": resp.get("totalUpdatedCells", 0),
        "total_updated_ranges": len(responses),
        "responses": responses,
    }


def create_spreadsheet(
    creds: Credentials,
    title: str,
) -> dict:
    """Create an empty Google Sheets spreadsheet via ``spreadsheets.create``.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        title: The title for the new spreadsheet. Becomes the Drive
            filename AND the spreadsheet's first tab name (Sheets
            mirrors the title to the default tab).

    Returns:
        ``{spreadsheet_id, url, title}`` — the same flat envelope
        the docs tools use for ``gdocs_make_tabbed_doc``. Callers
        can immediately pipe ``spreadsheet_id`` into ``read_range`` /
        ``write_range`` for a 3-call create-write-read workflow.

    Raises:
        ValueError: empty / whitespace ``title``. Cheap rejection.
        HttpError: from the underlying SDK — propagated.

    Note:
        The created spreadsheet is owned by the OAuth user and lands
        in Drive root by default (same as ``gdocs_make_tabbed_doc``).
        Move it elsewhere via ``gdocs_move_to_folder``.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")

    sheets = get_service("sheets", "v4", credentials=creds)
    body = {"properties": {"title": title.strip()}}
    resp = sheets.spreadsheets().create(
        body=body,
        fields="spreadsheetId,properties.title,spreadsheetUrl",
    ).execute()
    sid = resp["spreadsheetId"]
    return {
        "spreadsheet_id": sid,
        "url": resp.get(
            "spreadsheetUrl",
            f"https://docs.google.com/spreadsheets/d/{sid}/edit",
        ),
        "title": resp.get("properties", {}).get("title", title.strip()),
    }


def format_range(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    font_size: int | None = None,
    foreground_color: tuple[float, float, float] | None = None,
    background_color: tuple[float, float, float] | None = None,
    horizontal_alignment: str | None = None,
    number_format: str | None = None,
) -> dict:
    """Format a rectangular block of cells via ``spreadsheets.batchUpdate``.

    The first operation wired through the reusable batchUpdate
    request-builder (``services/sheets/batch.py``): composes a single
    ``repeatCell`` request from a ``GridRange`` + a ``CellFormat`` and
    dispatches it. New batchUpdate-backed Sheets features follow the
    same compose-then-``batch_update`` shape.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.
        start_row / end_row / start_col / end_col: 0-based, half-open
            GridRange bounds (``end`` exclusive, like a Python slice).
            Any omitted bound is unbounded on that side — omitting all
            four targets the whole sheet. See ``batch.grid_range``.
        bold / italic / font_size: Text-format options.
        foreground_color / background_color: ``(r, g, b)`` tuples with
            each channel in ``[0.0, 1.0]`` (Sheets colors are floats,
            not 0-255 ints).
        horizontal_alignment: ``LEFT`` / ``CENTER`` / ``RIGHT``.
        number_format: A Sheets number-format pattern (e.g.
            ``"#,##0.00"``, ``"0.00%"``, ``"$#,##0"``, ``"yyyy-mm-dd"``).

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — the flat
        ``batch_update`` envelope. ``total_requests`` is 1 (one
        ``repeatCell``); ``replies`` is the raw Sheets reply list.

    Raises:
        ValueError: no format options supplied (an empty format is a
            no-op Sheets rejects), an invalid alignment / color, or an
            inverted GridRange — all caught client-side by the
            ``batch.py`` builders before the round-trip.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched with ``idempotent=True`` — a ``repeatCell`` format
        produces the same cell state no matter how many times it runs,
        so it is safe to retry on a transient 429/5xx (unlike a generic
        ``batch_update``, which defaults to non-idempotent).
    """
    grid = grid_range(
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
    )
    fmt = cell_format(
        bold=bold,
        italic=italic,
        font_size=font_size,
        foreground_color=(
            color(*foreground_color) if foreground_color is not None else None
        ),
        background_color=(
            color(*background_color) if background_color is not None else None
        ),
        horizontal_alignment=horizontal_alignment,
        number_format=number_format,
    )
    request = repeat_cell_request(grid, fmt)
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        idempotent=True,
        op_name="sheets.spreadsheets.batchUpdate.repeatCell",
    )


def append_rows(
    creds: Credentials,
    spreadsheet_id: str,
    values: list[list[Any]],
    *,
    range_str: str = DEFAULT_RANGE,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Append rows after a table's last row via ``spreadsheets.values.append``.

    The race-free alternative to ``read_range`` → compute next-empty-row
    → ``write_range``. ``values.append`` lets SHEETS find the table's
    last row and write below it server-side, in one atomic call — so two
    concurrent appends land on consecutive rows instead of clobbering
    each other (the classic read-then-write race the manual pattern has).

    Defaults to ``valueInputOption="USER_ENTERED"`` (formulas/dates/numbers
    parse as if typed — consistent with ``write_range``) and always uses
    ``insertDataOption="INSERT_ROWS"`` (push existing rows down rather
    than overwrite anything below the table — the safe default for an
    "append").

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        values: 2D row-major list of rows to append (each inner list is
            one row, left-to-right). Strings / numbers / bools / None
            permitted; ``None`` writes a blank cell.
        range_str: An A1 range Sheets uses to LOCATE the table to append
            to (it searches this range for the table, then writes after
            its last row) — NOT the write destination. Defaults to
            ``DEFAULT_RANGE`` (the first tab). Pass ``"Sheet2!A:Z"`` to
            target a specific tab.
        value_input_option: How Sheets interprets the appended values.
            ``"USER_ENTERED"`` (default) parses them as if typed (formulas
            / dates / numbers); ``"RAW"`` stores them literally (a leading
            ``=`` stays literal text). Same semantics as ``write_range``.

    Returns:
        ``{updated_range, updated_cells, updated_rows}`` — ``updated_range``
        is the A1 range Sheets actually wrote the new rows into (echoed
        so the caller sees where they landed), ``updated_cells`` /
        ``updated_rows`` are Sheets' counts for the appended block.

    Raises:
        ValueError: ``values`` empty or not a list of lists, or an
            unknown ``value_input_option`` (same cheap client-side
            rejection as ``write_range`` — Sheets' own 400 is less
            helpful).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched WITHOUT retry (``execute_with_retry`` is intentionally
        not used): ``append`` is NOT idempotent — a transient-error
        replay could append the same rows twice. The retry "safety floor"
        forbids blanket-retrying a non-idempotent mutation, so this is a
        plain ``.execute()`` (matching that contract).
    """
    if not values:
        raise ValueError(
            "values cannot be empty — pass at least one row to append "
            "(use [[]] for a single blank row)."
        )
    if not all(isinstance(row, list) for row in values):
        raise ValueError(
            "values must be a list of lists (2D row-major). "
            f"Got element types: {sorted({type(r).__name__ for r in values})}"
        )
    _check_value_input_option(value_input_option)

    sheets = get_service("sheets", "v4", credentials=creds)
    # No execute_with_retry: append is non-idempotent (a replay duplicates
    # rows). Let HttpError propagate to the tool-layer envelope.
    resp = sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        valueInputOption=value_input_option,
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    # values.append nests the write result under "updates".
    updates = resp.get("updates", {})
    return {
        "updated_range": updates.get("updatedRange", range_str),
        "updated_cells": updates.get("updatedCells", 0),
        "updated_rows": updates.get("updatedRows", 0),
    }


def add_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    title: str,
    *,
    index: int | None = None,
) -> dict:
    """Add a new tab to a spreadsheet via ``batchUpdate`` (``addSheet``).

    Closes the "create makes only one tab" gap: ``create_spreadsheet``
    yields a single default tab, and this adds further tabs. Sheets
    assigns the new tab's ``sheetId`` (gid) server-side; this function
    parses it out of the batchUpdate reply so the caller gets the gid
    needed for ``format_range`` / ``rename_sheet`` / ``delete_sheet``.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        title: The new tab's name (unique within the spreadsheet —
            Sheets 400s on a duplicate). Blank rejected by the builder.
        index: 0-based position among existing tabs (0 = leftmost).
            ``None`` appends after the last tab.

    Returns:
        ``{spreadsheet_id, sheet_id, title, index}`` — ``sheet_id`` is
        the gid Sheets assigned the new tab; ``title`` / ``index`` echo
        the created tab's properties from the reply.

    Raises:
        ValueError: blank ``title`` / negative ``index`` (from the
            builder).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated
            (e.g. a duplicate tab name).
    """
    request = add_sheet_request(title, index=index)
    # idempotent=False (the batch_update default): re-running addSheet
    # would create ANOTHER tab (Sheets auto-uniquifies / 400s), so a
    # transient-error replay is unsafe.
    result = batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.addSheet",
    )
    props = (
        result.get("replies", [{}])[0]
        .get("addSheet", {})
        .get("properties", {})
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": props.get("sheetId"),
        "title": props.get("title", title.strip()),
        "index": props.get("index"),
    }


def delete_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
) -> dict:
    """Delete a tab by its gid via ``batchUpdate`` (``deleteSheet``).

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.

    Returns:
        ``{spreadsheet_id, deleted_sheet_id}`` — ``deleted_sheet_id``
        echoes the gid that was removed.

    Raises:
        ValueError: negative ``sheet_id`` (from the builder).
        HttpError: from the underlying SDK — propagated. Notably Sheets
            rejects deleting the LAST remaining tab (a spreadsheet must
            keep at least one) with a 400.

    Note:
        ``deleteSheet`` is DESTRUCTIVE (the tab and its data are gone).
        Dispatched non-idempotent — but the tool annotates
        ``idempotent=True`` semantically (deleting an already-deleted
        gid 400s rather than double-deleting). The dispatch stays
        non-retried to honor the destructive-op safety floor.
    """
    request = delete_sheet_request(sheet_id)
    result = batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.deleteSheet",
    )
    return {
        "spreadsheet_id": result.get("spreadsheet_id", spreadsheet_id),
        "deleted_sheet_id": sheet_id,
    }


def rename_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    title: str,
) -> dict:
    """Rename a tab via ``batchUpdate`` (``updateSheetProperties``).

    Uses a ``fields="title"`` mask so only the tab name changes — index,
    gridProperties, tabColor, etc. are untouched.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.
        title: The new tab name (unique within the spreadsheet — Sheets
            400s on a duplicate). Blank rejected by the builder.

    Returns:
        ``{spreadsheet_id, sheet_id, title}`` — ``title`` echoes the
        (stripped) new name.

    Raises:
        ValueError: blank ``title`` / negative ``sheet_id`` (from the
            builder).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated
            (e.g. a duplicate tab name, or an unknown gid).

    Note:
        A rename is idempotent (renaming to the same title twice yields
        the same state), so this dispatches with ``idempotent=True`` —
        safe to retry on a transient 429/5xx.
    """
    request = update_sheet_title_request(sheet_id, title)
    batch_update(
        creds,
        spreadsheet_id,
        [request],
        idempotent=True,
        op_name="sheets.spreadsheets.batchUpdate.updateSheetProperties",
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": sheet_id,
        "title": title.strip(),
    }


def apply_conditional_format(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    condition_type: str,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
    values: list[str] | None = None,
    background_color: tuple[float, float, float] | None = None,
    bold: bool | None = None,
    index: int = 0,
) -> dict:
    """Add a conditional-format rule to a cell range via ``batchUpdate``.

    Composes a single ``addConditionalFormatRule`` request from a
    ``GridRange`` + a boolean condition + the format to apply when the
    condition matches, then dispatches it. Reuses the reusable batch
    request-builder (``services/sheets/batch.py``) — the same seam
    ``format_range`` uses, just a different request type.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.
        condition_type: A Sheets ``ConditionType`` enum value, e.g.
            ``"NUMBER_GREATER"``, ``"NUMBER_LESS"``, ``"NUMBER_BETWEEN"``,
            ``"TEXT_CONTAINS"``, ``"TEXT_EQ"``, ``"DATE_BEFORE"``,
            ``"BLANK"``, ``"NOT_BLANK"``, ``"CUSTOM_FORMULA"``. Passed
            through verbatim (the enum set is large + Google-versioned, so
            an invalid value surfaces Google's own enum error).
        start_row / end_row / start_col / end_col: 0-based, half-open
            GridRange bounds (``end`` exclusive, like a Python slice). Omit
            a bound to leave that side unbounded; omit all four to target
            the whole sheet. See ``batch.grid_range``.
        values: the condition's comparison value(s) as strings — e.g.
            ``["100"]`` for ``NUMBER_GREATER``, ``["10", "20"]`` for
            ``NUMBER_BETWEEN``, ``["=A1>AVERAGE(A:A)"]`` for
            ``CUSTOM_FORMULA``. Omit for valueless conditions (``BLANK`` /
            ``NOT_BLANK``).
        background_color: ``(r, g, b)`` fill applied to matching cells,
            each channel in ``[0.0, 1.0]`` (Sheets colors are floats, not
            0-255). At least one of ``background_color`` / ``bold`` is
            required.
        bold: when ``True``, bold the text of matching cells.
        index: insertion priority among existing rules (0 = highest, run
            first).

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: neither ``background_color`` nor ``bold`` supplied (a
            rule with no format does nothing), or an inverted GridRange —
            both caught client-side by the ``batch.py`` builders before
            the round-trip.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched with ``idempotent=False`` (the ``batch_update``
        default): ``addConditionalFormatRule`` APPENDS a rule, so a
        transient-error replay could add the SAME rule twice (two stacked
        identical rules). Not blanket-retried — matches the
        ``execute_with_retry`` safety floor for append-style mutations.
    """
    grid = grid_range(
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
    )
    request = add_conditional_format_rule_request(
        grid,
        condition_type=condition_type,
        values=values,
        background_color=(
            color(*background_color) if background_color is not None else None
        ),
        bold=bold,
        index=index,
    )
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.addConditionalFormatRule",
    )


def clear_range(
    creds: Credentials,
    spreadsheet_id: str,
    range_str: str,
) -> dict:
    """Clear cell VALUES in a range via ``spreadsheets.values.clear``.

    Removes the values from every cell in the range while leaving the
    cells' FORMATTING (bold, colors, number formats, data validation,
    conditional rules) intact — the values-only counterpart to
    ``write_range``. To remove a whole tab (cells AND formatting AND the
    tab itself), use ``delete_sheet`` instead.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        range_str: An A1-notation range to clear, e.g. ``"A1:Z1000"``
            (default tab) or ``"Sheet2!B2:D10"`` (named tab + range).
            Required — there is no "clear the whole sheet" default, so
            the caller must state exactly what to wipe.

    Returns:
        ``{spreadsheet_id, cleared_range}`` — ``cleared_range`` is the
        A1 range Sheets reports it cleared (echoed back so the caller
        can confirm what was wiped).

    Raises:
        ValueError: blank ``range_str`` (clearing "nothing" is a caller
            bug — reject it rather than issue a no-op call).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched with ``idempotent=True`` — clearing an
        already-cleared range produces the same (empty) state, so it is
        safe to retry on a transient 429/5xx.
    """
    if not range_str or not range_str.strip():
        raise ValueError(
            "range_str cannot be empty — pass the A1 range to clear "
            "(e.g. \"A1:Z1000\" or \"Sheet2!B2:D10\")."
        )

    sheets = get_service("sheets", "v4", credentials=creds)
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range_str,
            body={},
        ).execute(),
        idempotent=True,
        op_name="sheets.values.clear",
    )
    return {
        "spreadsheet_id": resp.get("spreadsheetId", spreadsheet_id),
        "cleared_range": resp.get("clearedRange", range_str),
    }


def duplicate_sheet(
    creds: Credentials,
    spreadsheet_id: str,
    source_sheet_id: int,
    *,
    new_sheet_name: str | None = None,
    insert_index: int | None = None,
) -> dict:
    """Duplicate a tab via ``batchUpdate`` (``duplicateSheet``).

    Makes a full copy of a sheet (values, formats, conditional rules,
    charts) as a new tab. Sheets assigns the copy a fresh ``sheetId``
    (gid) server-side; this parses it out of the reply so the caller gets
    the gid needed for ``format_range`` / ``rename_sheet`` / etc.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        source_sheet_id: The gid of the tab to copy — NOT the tab name.
            The first/default tab is ``0``.
        new_sheet_name: Name for the copy. ``None`` lets Sheets auto-name
            it (``"Copy of <source>"``). A duplicate name is rejected by
            Sheets; blank rejected by the builder.
        insert_index: 0-based position for the copy among existing tabs
            (0 = leftmost). ``None`` lets Sheets place it after the source.

    Returns:
        ``{spreadsheet_id, sheet_id, title, index}`` — ``sheet_id`` is
        the gid Sheets assigned the copy; ``title`` / ``index`` echo the
        new tab's properties from the reply.

    Raises:
        ValueError: negative ``source_sheet_id`` / ``insert_index``, or a
            blank ``new_sheet_name`` (from the builder).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated
            (e.g. a duplicate tab name).

    Note:
        idempotent=False (the ``batch_update`` default): re-running
        ``duplicateSheet`` would create ANOTHER copy, so a transient-error
        replay is unsafe.
    """
    request = duplicate_sheet_request(
        source_sheet_id,
        new_sheet_name=new_sheet_name,
        insert_index=insert_index,
    )
    result = batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.duplicateSheet",
    )
    props = (
        result.get("replies", [{}])[0]
        .get("duplicateSheet", {})
        .get("properties", {})
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": props.get("sheetId"),
        "title": props.get("title"),
        "index": props.get("index"),
    }


def freeze(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    frozen_row_count: int | None = None,
    frozen_column_count: int | None = None,
) -> dict:
    """Freeze rows/columns of a tab via ``batchUpdate`` (``updateSheetProperties``).

    Pins the top ``frozen_row_count`` rows and/or the left
    ``frozen_column_count`` columns so they stay visible while the rest
    of the sheet scrolls (the canonical way to keep a header row in view).
    Uses a ``fields`` mask scoped to exactly the dimension(s) supplied, so
    the other frozen count and every other sheet property are untouched.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.
        frozen_row_count: Rows to freeze from the top (``0`` unfreezes
            rows). ``None`` leaves the row freeze untouched.
        frozen_column_count: Columns to freeze from the left (``0``
            unfreezes columns). ``None`` leaves the column freeze
            untouched.

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: negative ``sheet_id`` / count, or neither count
            supplied (from the builder).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched with ``idempotent=True`` — setting the same frozen
        counts twice yields the same state, so it is safe to retry on a
        transient 429/5xx.
    """
    request = freeze_request(
        sheet_id,
        frozen_row_count=frozen_row_count,
        frozen_column_count=frozen_column_count,
    )
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        idempotent=True,
        op_name="sheets.spreadsheets.batchUpdate.updateSheetProperties.freeze",
    )


def protect_range(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
    description: str | None = None,
    warning_only: bool = False,
    editor_emails: list[str] | None = None,
) -> dict:
    """Protect a cell range via ``batchUpdate`` (``addProtectedRange``).

    Restricts who can edit a rectangular block. ``warning_only=True``
    shows an "are you sure?" warning but doesn't block edits; the default
    (``warning_only=False``) BLOCKS edits for everyone except the listed
    editors (and the owner).

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, not the tab
            name. The first/default tab is ``0``.
        start_row / end_row / start_col / end_col: 0-based, half-open
            GridRange bounds (``end`` exclusive, like a Python slice).
            Omit a bound to leave that side unbounded; omit all four to
            protect the whole sheet. See ``batch.grid_range``.
        description: Optional label for the protected range (shown in the
            Sheets protection UI).
        warning_only: When ``True``, edits warn but aren't blocked; when
            ``False`` (default), edits are restricted to ``editor_emails``
            (+ owner). ``editor_emails`` is incompatible with
            ``warning_only=True``.
        editor_emails: Emails allowed to edit the protected range (ignored
            when ``warning_only=True``). ``None`` / empty means only the
            owner can edit.

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: ``warning_only=True`` combined with ``editor_emails``,
            or an inverted GridRange — both caught client-side by the
            builders before the round-trip.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched with ``idempotent=False`` (the ``batch_update``
        default): ``addProtectedRange`` APPENDS a protected range, so a
        transient-error replay could add a second overlapping protection.
        Not blanket-retried — matches the append-style safety floor.
    """
    grid = grid_range(
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
    )
    request = add_protected_range_request(
        grid,
        description=description,
        warning_only=warning_only,
        editor_emails=editor_emails,
    )
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.addProtectedRange",
    )


def insert_dimension(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    dimension: str,
    start_index: int,
    end_index: int,
    inherit_from_before: bool = False,
) -> dict:
    """Insert empty rows or columns via ``batchUpdate`` (``insertDimension``).

    Inserts ``end_index - start_index`` empty rows (``dimension="ROWS"``)
    or columns (``dimension="COLUMNS"``) before ``start_index``, shifting
    existing cells down / right. Uses the reusable batch request-builder
    (``services/sheets/batch.py``).

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id, the ``gid``, not the tab
            name. The first/default tab is ``0``.
        dimension: ``"ROWS"`` or ``"COLUMNS"``.
        start_index: 0-based index to insert before (``0`` = very top /
            left).
        end_index: 0-based EXCLUSIVE end; count inserted is
            ``end_index - start_index``.
        inherit_from_before: When ``True``, new rows/cols inherit the
            formatting of the row/col before them; ``False`` (default)
            inherits from after. ``True`` with ``start_index=0`` is
            rejected by Sheets (nothing before to inherit), that 400
            propagates.

    Returns:
        ``{spreadsheet_id, total_requests, replies}``, the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: bad dimension / index (from the builder).
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.

    Note:
        Dispatched with ``idempotent=False`` (the ``batch_update``
        default): re-running inserts ANOTHER band, so a transient-error
        replay could insert twice. Not blanket-retried.
    """
    request = insert_dimension_request(
        sheet_id,
        dimension=dimension,
        start_index=start_index,
        end_index=end_index,
        inherit_from_before=inherit_from_before,
    )
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.insertDimension",
    )


def delete_dimension(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    dimension: str,
    start_index: int,
    end_index: int,
) -> dict:
    """Delete rows or columns via ``batchUpdate`` (``deleteDimension``).

    Removes the half-open band ``[start_index, end_index)`` of rows
    (``dimension="ROWS"``) or columns (``dimension="COLUMNS"``), shifting
    later cells up / left. DESTRUCTIVE, the cells in the band are gone.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id, the ``gid``. The
            first/default tab is ``0``.
        dimension: ``"ROWS"`` or ``"COLUMNS"``.
        start_index: 0-based inclusive first row/col to delete.
        end_index: 0-based EXCLUSIVE end; count deleted is
            ``end_index - start_index``.

    Returns:
        ``{spreadsheet_id, total_requests, replies}``, the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: bad dimension / index (from the builder).
        HttpError: from the underlying SDK on 4xx / 5xx, propagated
            (e.g. deleting all rows of a sheet, which Sheets rejects).

    Note:
        Dispatched with ``idempotent=False``: a deleteDimension is not
        safe to blanket-retry (a replay would delete a DIFFERENT band
        after the first delete shifted the indices). Matches the
        destructive-op safety floor.
    """
    request = delete_dimension_request(
        sheet_id,
        dimension=dimension,
        start_index=start_index,
        end_index=end_index,
    )
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.deleteDimension",
    )


def merge_cells(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
    merge_type: str = "MERGE_ALL",
) -> dict:
    """Merge a cell range via ``batchUpdate`` (``mergeCells``).

    Combines the cells in a ``GridRange`` into one (``MERGE_ALL``), or
    merges down each column (``MERGE_COLUMNS``) / across each row
    (``MERGE_ROWS``). Uses the reusable batch request-builder.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id, the ``gid``. The
            first/default tab is ``0``.
        start_row / end_row / start_col / end_col: 0-based, half-open
            GridRange bounds (``end`` exclusive). A merge needs a real
            multi-cell rectangle, supply bounds covering 2+ cells.
        merge_type: ``"MERGE_ALL"`` (default), ``"MERGE_COLUMNS"``, or
            ``"MERGE_ROWS"``.

    Returns:
        ``{spreadsheet_id, total_requests, replies}``, the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: bad ``merge_type`` or an inverted GridRange, caught
            by the builders client-side.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated
            (e.g. a single-cell range Sheets rejects as nothing to merge).

    Note:
        Dispatched with ``idempotent=True``, merging the same range the
        same way twice yields the same state (the 2nd call is a no-op),
        so it is safe to retry on a transient 429/5xx.
    """
    grid = grid_range(
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
    )
    request = merge_cells_request(grid, merge_type=merge_type)
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        idempotent=True,
        op_name="sheets.spreadsheets.batchUpdate.mergeCells",
    )


def set_data_validation(
    creds: Credentials,
    spreadsheet_id: str,
    sheet_id: int,
    *,
    condition_type: str,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
    values: list[str] | None = None,
    strict: bool = True,
    show_custom_ui: bool = True,
    input_message: str | None = None,
) -> dict:
    """Set a data-validation rule on a range via ``batchUpdate``.

    Attaches a ``setDataValidation`` rule to every cell in a
    ``GridRange``, a dropdown (``condition_type="ONE_OF_LIST"``), a
    numeric bound (``NUMBER_BETWEEN``), a checkbox (``BOOLEAN``), etc.
    Reuses the reusable batch request-builder.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        sheet_id: The numeric sheet (tab) id, the ``gid``. The
            first/default tab is ``0``.
        condition_type: A Sheets ``ConditionType`` enum value, e.g.
            ``"ONE_OF_LIST"`` (dropdown), ``"NUMBER_BETWEEN"``,
            ``"BOOLEAN"`` (checkbox), ``"TEXT_IS_EMAIL"``. Passed through
            verbatim (an invalid value surfaces Google's own enum error).
        start_row / end_row / start_col / end_col: 0-based, half-open
            GridRange bounds (``end`` exclusive). Omit a bound to leave
            that side unbounded; omit all four to target the whole sheet.
        values: the condition's value(s) as strings (e.g. the dropdown
            items for ``ONE_OF_LIST``, ``["1", "10"]`` for
            ``NUMBER_BETWEEN``). Omit for valueless conditions.
        strict: ``True`` (default) REJECTS invalid entries; ``False``
            allows them with a warning flag.
        show_custom_ui: ``True`` (default) renders the UI affordance
            (e.g. the dropdown arrow).
        input_message: optional help text shown when a cell is selected.

    Returns:
        ``{spreadsheet_id, total_requests, replies}``, the flat
        ``batch_update`` envelope (``total_requests`` is 1).

    Raises:
        ValueError: blank ``condition_type`` or an inverted GridRange -
            caught by the builders client-side.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.

    Note:
        Dispatched with ``idempotent=True``, setting the same rule on
        the same range twice yields the same state, so it is safe to
        retry on a transient 429/5xx.
    """
    grid = grid_range(
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
    )
    request = set_data_validation_request(
        grid,
        condition_type=condition_type,
        values=values,
        strict=strict,
        show_custom_ui=show_custom_ui,
        input_message=input_message,
    )
    return batch_update(
        creds,
        spreadsheet_id,
        [request],
        idempotent=True,
        op_name="sheets.spreadsheets.batchUpdate.setDataValidation",
    )


def add_chart(
    creds: Credentials,
    spreadsheet_id: str,
    *,
    chart_type: str,
    domain_sheet_id: int,
    domain_start_row: int,
    domain_end_row: int,
    domain_start_col: int,
    domain_end_col: int,
    series_ranges: list[dict],
    anchor_sheet_id: int,
    anchor_row: int,
    anchor_col: int,
    title: str | None = None,
    header_count: int = 1,
) -> dict:
    """Add a basic embedded chart via ``batchUpdate`` (``addChart``).

    Builds a basic cartesian chart (bar / line / area / column / scatter
    / combo / stepped-area) from one domain range (the X axis) plus one
    or more series ranges (the plotted Y values), anchored as an overlay
    at a cell. Sheets assigns the new chart a ``chartId`` server-side and
    echoes it in the reply; this surfaces it for follow-up calls.

    Args:
        creds: OAuth credentials carrying the ``spreadsheets`` scope.
        spreadsheet_id: The Sheets file ID.
        chart_type: A Sheets ``BasicChartType``, ``BAR`` / ``LINE`` /
            ``AREA`` / ``COLUMN`` / ``SCATTER`` / ``COMBO`` /
            ``STEPPED_AREA``.
        domain_sheet_id: gid of the sheet holding the domain data.
        domain_start_row / domain_end_row / domain_start_col /
            domain_end_col: 0-based, half-open GridRange bounds of the
            domain (X axis / categories).
        series_ranges: list of ``{sheet_id, start_row, end_row,
            start_col, end_col}`` dicts, each a data series (Y values).
            At least one required. Each is turned into a GridRange via
            ``grid_range`` (so each gets the same half-open / index
            validation).
        anchor_sheet_id: gid of the sheet the chart overlay is placed on.
        anchor_row / anchor_col: 0-based cell coordinates of the chart's
            top-left anchor.
        title: optional chart title.
        header_count: leading header rows/cols Sheets labels series from
            (default 1).

    Returns:
        ``{spreadsheet_id, chart_id, total_requests, replies}`` -
        ``chart_id`` is the gid Sheets assigned the new chart (parsed
        from the reply), plus the flat ``batch_update`` envelope.

    Raises:
        ValueError: bad ``chart_type``, empty ``series_ranges``, an
            inverted GridRange, or a negative anchor coordinate, all
            caught by the builders client-side.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.

    Note:
        Dispatched with ``idempotent=False``: ``addChart`` APPENDS a new
        chart (a fresh chartId each call), so a transient-error replay
        could create a duplicate chart. Not blanket-retried.
    """
    domain_grid = grid_range(
        domain_sheet_id,
        start_row=domain_start_row,
        end_row=domain_end_row,
        start_col=domain_start_col,
        end_col=domain_end_col,
    )
    series_grids = [
        grid_range(
            s["sheet_id"],
            start_row=s.get("start_row"),
            end_row=s.get("end_row"),
            start_col=s.get("start_col"),
            end_col=s.get("end_col"),
        )
        for s in series_ranges
    ]
    request = add_chart_request(
        chart_type=chart_type,
        title=title,
        domain_grid=domain_grid,
        series_grids=series_grids,
        anchor_sheet_id=anchor_sheet_id,
        anchor_row=anchor_row,
        anchor_col=anchor_col,
        header_count=header_count,
    )
    result = batch_update(
        creds,
        spreadsheet_id,
        [request],
        op_name="sheets.spreadsheets.batchUpdate.addChart",
    )
    chart_id = (
        result.get("replies", [{}])[0]
        .get("addChart", {})
        .get("chart", {})
        .get("chartId")
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "chart_id": chart_id,
        "total_requests": result.get("total_requests", 1),
        "replies": result.get("replies", []),
    }
