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

**Value-input semantics.** ``write_range`` passes
``valueInputOption="USER_ENTERED"`` — Sheets interprets values as if
the user typed them in the UI: ``"=SUM(A1:A10)"`` becomes a formula,
``"1/2"`` becomes a date, etc. ``RAW`` would store everything as
literal strings, which is rarely what an MCP caller wants from a
"write these values" tool. Callers needing literal-string writes
should call the API directly until a future PR exposes the option.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service
from appscriptly.services.sheets.batch import (
    add_sheet_request,
    batch_update,
    cell_format,
    color,
    delete_sheet_request,
    grid_range,
    repeat_cell_request,
    update_sheet_title_request,
)

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Default range read when the caller doesn't specify one. ``A1:Z1000``
# covers a reasonable starting workspace — 26 columns x 1000 rows.
# Sheets caps a single ``values.get`` at the spreadsheet's used range
# anyway, so an oversized default doesn't waste bandwidth.
DEFAULT_RANGE = "A1:Z1000"


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

    Returns:
        ``{updated_range, updated_cells}`` — ``updated_range`` is the
        A1 range Sheets actually wrote into (echoed back so callers
        can confirm), ``updated_cells`` is the count Sheets reports
        it changed (may differ from ``len(values) * max-row-len`` if
        the requested range was smaller than the values block).

    Raises:
        ValueError: ``values`` empty or not a list of lists. Cheap
            client-side rejection — Sheets returns a 400 with a less
            helpful message.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Uses ``valueInputOption="USER_ENTERED"`` — formulas / dates /
        numbers parse the same way they would if a user typed them
        in the UI. Use the Sheets API directly if literal-string
        writes are required (no MCP surface for ``RAW`` yet).
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

    sheets = get_service("sheets", "v4", credentials=creds)
    # PR-Δ3.5: gsheets_write_range is annotated idempotent=True (writing
    # the same values to the same range twice is a no-op assuming the
    # caller passes the same values). Wrap to retry on 429/5xx.
    resp = execute_with_retry(
        lambda: sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_str,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute(),
        idempotent=True,
        op_name="sheets.values.update",
    )
    return {
        "updated_range": resp.get("updatedRange", range_str),
        "updated_cells": resp.get("updatedCells", 0),
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
) -> dict:
    """Append rows after a table's last row via ``spreadsheets.values.append``.

    The race-free alternative to ``read_range`` → compute next-empty-row
    → ``write_range``. ``values.append`` lets SHEETS find the table's
    last row and write below it server-side, in one atomic call — so two
    concurrent appends land on consecutive rows instead of clobbering
    each other (the classic read-then-write race the manual pattern has).

    Uses ``valueInputOption="USER_ENTERED"`` (formulas/dates/numbers
    parse as if typed — consistent with ``write_range``) and
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

    Returns:
        ``{updated_range, updated_cells, updated_rows}`` — ``updated_range``
        is the A1 range Sheets actually wrote the new rows into (echoed
        so the caller sees where they landed), ``updated_cells`` /
        ``updated_rows`` are Sheets' counts for the appended block.

    Raises:
        ValueError: ``values`` empty or not a list of lists (same cheap
            client-side rejection as ``write_range`` — Sheets' own 400
            is less helpful).
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

    sheets = get_service("sheets", "v4", credentials=creds)
    # No execute_with_retry: append is non-idempotent (a replay duplicates
    # rows). Let HttpError propagate to the tool-layer envelope.
    resp = sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        valueInputOption="USER_ENTERED",
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
