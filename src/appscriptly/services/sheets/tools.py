"""Google Sheets MCP tool registrations (v2.3.1 — 2nd new service).

Mirrors the layout established by ``services/drive/tools.py`` and
``services/docs/tools.py``: ``@workspace_tool``-decorated functions
that register with the live ``mcp`` instance when this module is
imported. ``server.py`` performs the import at the bottom AFTER
constructing ``mcp``, the same side-effect pattern as Phase A/B/C
and Gap #7.

**Tools registered here** (13 sheets-service tools):

1.  ``gsheets_read_range``               — read cell values from a range
2.  ``gsheets_write_range``              — write 2D values to a range
3.  ``gsheets_create_spreadsheet``       — create an empty new spreadsheet
4.  ``gsheets_format_range``             — format a cell block (batchUpdate)
5.  ``gsheets_append_rows``              — append rows after the last row of data
6.  ``gsheets_add_sheet``                — add a tab/sheet to a spreadsheet
7.  ``gsheets_delete_sheet``             — delete a tab/sheet by id
8.  ``gsheets_rename_sheet``             — rename a tab/sheet
9.  ``gsheets_apply_conditional_format`` — apply a conditional-format rule
10. ``gsheets_clear_range``              — clear cell values in a range (values.clear)
11. ``gsheets_duplicate_sheet``          — copy a tab/sheet (batchUpdate)
12. ``gsheets_freeze``                   — freeze header rows/columns (batchUpdate)
13. ``gsheets_protect_range``            — protect a cell range (batchUpdate)

(Authoritative declaration: ``services/sheets/_expected_tools.py``.)

The first trio enables a complete 3-call workflow:
``create_spreadsheet`` → ``write_range`` → ``read_range``.
``gsheets_format_range`` is the first ``batchUpdate``-backed tool,
wired through the reusable request-builder in
``services/sheets/batch.py``.

**The batchUpdate seam.** ``batchUpdate``'s tagged-union surface
(formatting, conditional formatting, charts, pivots, named ranges,
sheet-lifecycle, dimensions — ~40 request types) was originally
deferred for lack of precedent. ``services/sheets/batch.py`` closes
that gap: pure, typed request-builders (mirroring the docs + slides
batchUpdate pattern) that compose request dicts, plus one
``execute_with_retry``-wrapped dispatcher. ``gsheets_format_range``
proves the seam end-to-end; further request types layer on the same
builders instead of hand-rolling raw dicts.

**Import discipline.** Same as ``services/drive/tools.py``:

- ``_get_credentials`` + ``_format_http_error`` imported directly
  from ``_tool_helpers`` (the M3 Phase C extraction).
- The api module is the standard ``from ... import api`` pattern.
- ``@workspace_tool(service="sheets", ...)`` annotation carries the
  service= literal that drives the partition test + future telemetry.
"""
from __future__ import annotations

from appscriptly.decorators import workspace_tool
from appscriptly.services.sheets.api import (
    DEFAULT_RANGE,
    add_sheet as _add_sheet,
    append_rows as _append_rows,
    apply_conditional_format as _apply_conditional_format,
    clear_range as _clear_range,
    create_spreadsheet as _create_spreadsheet,
    delete_sheet as _delete_sheet,
    duplicate_sheet as _duplicate_sheet,
    format_range as _format_range,
    freeze as _freeze,
    protect_range as _protect_range,
    read_range as _read_range,
    rename_sheet as _rename_sheet,
    write_range as _write_range,
)
from appscriptly.tool_schemas import (
    GSHEETS_ADD_SHEET_OUTPUT_SCHEMA,
    GSHEETS_APPEND_ROWS_OUTPUT_SCHEMA,
    GSHEETS_APPLY_CONDITIONAL_FORMAT_OUTPUT_SCHEMA,
    GSHEETS_CLEAR_RANGE_OUTPUT_SCHEMA,
    GSHEETS_CREATE_SPREADSHEET_OUTPUT_SCHEMA,
    GSHEETS_DELETE_SHEET_OUTPUT_SCHEMA,
    GSHEETS_DUPLICATE_SHEET_OUTPUT_SCHEMA,
    GSHEETS_FORMAT_RANGE_OUTPUT_SCHEMA,
    GSHEETS_FREEZE_OUTPUT_SCHEMA,
    GSHEETS_PROTECT_RANGE_OUTPUT_SCHEMA,
    GSHEETS_READ_RANGE_OUTPUT_SCHEMA,
    GSHEETS_RENAME_SHEET_OUTPUT_SCHEMA,
    GSHEETS_WRITE_RANGE_OUTPUT_SCHEMA,
)

# Imported for parity with services/drive/tools.py; not used by the
# minimal trio (none of these need _format_http_error since they let
# HttpError propagate to the standard decorator envelope). Kept as a
# top-level import so adding a 4th tool that DOES need it doesn't
# trigger a separate import statement.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)


# ---------------------------------------------------------------------
# 1. gsheets_read_range — values.get (pure read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Read a range from a Google Sheet",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_READ_RANGE_OUTPUT_SCHEMA,
)
def gsheets_read_range(
    creds,
    spreadsheet_id: str,
    range: str = DEFAULT_RANGE,
) -> dict:
    """Read cell values from a range in a Google Sheet.

    USE WHEN: the agent needs to inspect spreadsheet contents — for
    summarization, validation, conditional follow-up actions, or
    just to surface the values back to the user.

    Uses Sheets' ``spreadsheets.values.get`` REST endpoint. Returns
    a 2D row-major values list — rows top-to-bottom, cells
    left-to-right. Sheets TRIMS trailing empty cells from each row
    and trailing empty rows from the range; consumers iterating
    rectangularly should pad client-side.

    Args:
        spreadsheet_id: The spreadsheet ID (the ID part of the
            sharing URL, NOT a gid for an individual sheet).
        range: A1-notation range, e.g. ``"A1:Z1000"`` (default tab),
            ``"Sheet2!B2:D10"`` (named tab + range), or
            ``"NamedRange"``. Defaults to ``"A1:Z1000"``.

    Returns:
        ``{range, values: [[cell, cell, ...], ...]}``. ``range`` is
        the canonical A1 form Sheets returned (may differ from input
        — Sheets normalizes ``"Sheet1!A:Z"`` to its full bound).
        ``values`` is empty list for blank ranges (not missing key).

    Choreography: ``spreadsheet_id`` from the user (URL), from a
    prior ``gsheets_create_spreadsheet`` call, or from
    ``gdocs_find_doc_by_title`` (sheets show up there too, just with
    ``mimeType=application/vnd.google-apps.spreadsheet``).
    """
    return _read_range(creds, spreadsheet_id, range)


# ---------------------------------------------------------------------
# 2. gsheets_write_range — values.update (overwrite)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Write 2D values to a range in a Google Sheet",
    # Overwriting cells in place is not "destructive" in our sense
    # (the doc / spreadsheet still exists; cells can be re-written
    # to recover); matches the convention used by gdocs_replace_all_text.
    readonly=False,
    destructive=False,
    # Same input → same Sheets state. Re-running a successful
    # write_range with identical args produces identical cells.
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_WRITE_RANGE_OUTPUT_SCHEMA,
)
def gsheets_write_range(
    creds,
    spreadsheet_id: str,
    range: str,
    values: list[list],
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Write 2D values to a range in a Google Sheet — overwrites in place.

    USE WHEN: the agent has computed a tabular result (a forecast,
    a roster, a summary table) that should land in a specific
    spreadsheet range. Common chained call after
    ``gsheets_create_spreadsheet``.

    Uses Sheets' ``spreadsheets.values.update`` REST endpoint. By
    default (``value_input_option="USER_ENTERED"``) values parse as if
    the user typed them in the UI: ``"=SUM(A1:A10)"`` becomes a
    formula, ``"1/2/2026"`` becomes a date, ``"42"`` becomes a number.
    Pass ``value_input_option="RAW"`` to store values EXACTLY as given
    — a leading ``=`` stays literal text, not a formula.

    Args:
        spreadsheet_id: The spreadsheet ID.
        range: A1-notation range — anchor cell or full block. If the
            range is smaller than ``values``, Sheets writes only the
            slice that fits. If larger, the extra cells are LEFT
            ALONE (not cleared) — pass exactly the dimensions you
            want.
        values: 2D row-major list. Each inner list is one row
            (left-to-right cells). Strings / numbers / bools / None
            all permitted. ``None`` writes a blank cell.
        value_input_option: ``"USER_ENTERED"`` (default) parses values
            as if typed (formulas / dates / numbers); ``"RAW"`` stores
            them literally. Use ``RAW`` when a value beginning with
            ``=`` (or that looks like a date/number) must be kept as
            plain text rather than interpreted.

    Returns:
        ``{updated_range, updated_cells}``. ``updated_range`` is the
        A1 range Sheets actually wrote into (echoed back so the
        caller can confirm). ``updated_cells`` is Sheets' count —
        may differ from ``sum(len(row) for row in values)`` when
        the request range was smaller than the values block.

    Choreography: typically follows ``gsheets_create_spreadsheet``
    (which returns the ID) or pairs with ``gsheets_read_range`` for
    a read-modify-write loop.
    """
    return _write_range(
        creds, spreadsheet_id, range, values,
        value_input_option=value_input_option,
    )


# ---------------------------------------------------------------------
# 3. gsheets_create_spreadsheet — spreadsheets.create
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Create an empty new Google Sheets spreadsheet",
    # Creating a fresh resource isn't a mutation of existing state.
    # Matches gdocs_make_tabbed_doc's annotations.
    readonly=False,
    destructive=False,
    # Re-running creates ANOTHER spreadsheet — NOT idempotent. Same
    # convention as gdocs_make_tabbed_doc.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSHEETS_CREATE_SPREADSHEET_OUTPUT_SCHEMA,
)
def gsheets_create_spreadsheet(creds, title: str) -> dict:
    """Create an empty Google Sheets spreadsheet (lands in Drive root).

    USE WHEN: the agent needs a fresh spreadsheet to write tabular
    output into — typically the FIRST call in a
    create → write_range → read_range workflow.

    Uses Sheets' ``spreadsheets.create`` REST endpoint. The created
    spreadsheet is owned by the OAuth user and lands in Drive root.
    Move it elsewhere via ``gdocs_move_to_folder`` (works because
    Sheets files are Drive files under the hood, so the existing
    Drive-service tools apply).

    Args:
        title: Title for the new spreadsheet. Becomes the Drive
            filename AND the spreadsheet's default (first) tab name.

    Returns:
        ``{spreadsheet_id, url, title}`` — same flat envelope as
        ``gdocs_make_tabbed_doc`` so callers can immediately pipe
        ``spreadsheet_id`` into ``gsheets_read_range`` /
        ``gsheets_write_range``.

    Choreography: the natural starter for any Sheets workflow.
    Pair with ``gdocs_move_to_folder`` to file it into the right
    folder, ``gdocs_share_file`` to grant collaborators access, and
    ``gsheets_write_range`` to populate.
    """
    return _create_spreadsheet(creds, title)


# ---------------------------------------------------------------------
# 4. gsheets_format_range — spreadsheets.batchUpdate (repeatCell)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Format a block of cells in a Google Sheet",
    # Applying formatting in place is not "destructive" in our sense
    # (cell values are untouched; formatting can be re-applied to
    # recover); matches gsheets_write_range's annotations.
    readonly=False,
    destructive=False,
    # A repeatCell format produces the same cell state no matter how
    # many times it runs — safe to retry. (The api layer dispatches it
    # with idempotent=True for exactly this reason.)
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_FORMAT_RANGE_OUTPUT_SCHEMA,
)
def gsheets_format_range(
    creds,
    spreadsheet_id: str,
    sheet_id: int,
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
    """Format a rectangular block of cells (bold, colors, alignment, etc.).

    USE WHEN: the agent needs to style a spreadsheet — bold a header
    row, currency-format a column, shade a total, center a label.
    Common follow-up after ``gsheets_write_range`` has placed the
    values.

    Uses Sheets' ``spreadsheets.batchUpdate`` with a single
    ``repeatCell`` request, composed via the reusable request-builder
    in ``services/sheets/batch.py`` (the same batchUpdate plumbing
    docs + slides already use). Only the format options you pass are
    applied — unrelated existing formatting in the range is preserved
    (the field mask is derived from exactly what you set).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, NOT the tab
            name and NOT the spreadsheet id. The first/default tab is
            ``0``. (Find a tab's gid in its URL ``#gid=...``.)
        start_row / end_row / start_col / end_col: 0-based, half-open
            cell bounds (``end`` is EXCLUSIVE, like a Python slice — so
            ``start_row=0, end_row=1`` is just the first row). Omit a
            bound to leave that side unbounded; omit all four to format
            the entire sheet.
        bold / italic / font_size: Text-format options.
        foreground_color / background_color: ``(r, g, b)`` tuples, each
            channel in ``[0.0, 1.0]`` (Sheets colors are floats, not
            0-255 ints — e.g. red is ``(1.0, 0.0, 0.0)``).
        horizontal_alignment: ``"LEFT"`` / ``"CENTER"`` / ``"RIGHT"``.
        number_format: A Sheets number-format pattern, e.g.
            ``"#,##0.00"`` (thousands + 2dp), ``"0.00%"`` (percent),
            ``"$#,##0"`` (currency), ``"yyyy-mm-dd"`` (date).

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — ``total_requests``
        is 1 (one ``repeatCell``); ``replies`` is Sheets' raw reply
        list (empty entries for request types that produce no reply).

    Choreography: typically follows ``gsheets_write_range`` (style the
    values you just wrote). Pass at least one format option — an
    all-``None`` call raises a ValueError rather than issuing a no-op
    batchUpdate.
    """
    return _format_range(
        creds,
        spreadsheet_id,
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
        bold=bold,
        italic=italic,
        font_size=font_size,
        foreground_color=foreground_color,
        background_color=background_color,
        horizontal_alignment=horizontal_alignment,
        number_format=number_format,
    )


# ---------------------------------------------------------------------
# 5. gsheets_append_rows — values.append (race-free append)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Append rows to the bottom of a Google Sheet table",
    # Adds rows below existing data — not a mutation of existing cells,
    # and the rows can be removed afterward. Matches gsheets_write_range.
    readonly=False,
    destructive=False,
    # NOT idempotent: re-running appends the SAME rows AGAIN (a second
    # copy below the first). Unlike write_range (fixed range → same
    # cells), append always grows the table. Same convention as
    # gsheets_create_spreadsheet / gslides_add_slide.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSHEETS_APPEND_ROWS_OUTPUT_SCHEMA,
)
def gsheets_append_rows(
    creds,
    spreadsheet_id: str,
    values: list[list],
    range: str = DEFAULT_RANGE,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Append rows to the END of a sheet's data — the race-free way.

    USE WHEN: the agent needs to ADD rows to an existing table (a log
    entry, a new record, more results) WITHOUT overwriting what's
    there. This is the correct tool for "add a row" — do NOT read the
    sheet, compute the next empty row, and ``gsheets_write_range`` to
    it: that read-then-write pattern races (two concurrent appends pick
    the same row and clobber each other).

    Uses Sheets' ``spreadsheets.values.append`` — SHEETS finds the
    table's last row and writes below it SERVER-SIDE in one atomic
    call, so concurrent appends land on consecutive rows. By default
    (``value_input_option="USER_ENTERED"``) values parse as if typed
    (formulas / dates / numbers, same as ``gsheets_write_range``); pass
    ``"RAW"`` to store them literally. Always uses
    ``insertDataOption="INSERT_ROWS"`` (existing rows below the table
    are pushed down, never overwritten).

    Args:
        spreadsheet_id: The spreadsheet ID.
        values: 2D row-major list of rows to append. Each inner list is
            one row (left-to-right cells). Strings / numbers / bools /
            None permitted; ``None`` writes a blank cell.
        range: An A1 range Sheets uses to LOCATE the table (it searches
            here for the data block, then appends after its last row) —
            NOT the write destination. Defaults to ``"A1:Z1000"`` (first
            tab). Pass e.g. ``"Sheet2!A:Z"`` to append to a specific tab.
        value_input_option: ``"USER_ENTERED"`` (default) parses values
            as if typed; ``"RAW"`` stores them literally (a leading ``=``
            stays text). Same semantics as ``gsheets_write_range``.

    Returns:
        ``{updated_range, updated_cells, updated_rows}`` — ``updated_range``
        is the A1 range Sheets actually wrote the new rows into (echoed
        so you can confirm where they landed); ``updated_cells`` /
        ``updated_rows`` are Sheets' counts for the appended block.

    Choreography: pairs with ``gsheets_create_spreadsheet`` +
    ``gsheets_write_range`` (write a header row, then append data rows),
    or stands alone to add to an existing sheet. Use
    ``gsheets_read_range`` afterward to read the table back.
    """
    return _append_rows(
        creds, spreadsheet_id, values, range_str=range,
        value_input_option=value_input_option,
    )


# ---------------------------------------------------------------------
# 6. gsheets_add_sheet — batchUpdate (addSheet)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Add a new tab (sheet) to a Google Sheets spreadsheet",
    # Adds a fresh tab — not a mutation of existing tabs. Matches
    # gsheets_create_spreadsheet's annotations.
    readonly=False,
    destructive=False,
    # NOT idempotent: re-running adds ANOTHER tab (Sheets 400s on a
    # duplicate title, or auto-uniquifies). Same convention as create.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSHEETS_ADD_SHEET_OUTPUT_SCHEMA,
)
def gsheets_add_sheet(
    creds,
    spreadsheet_id: str,
    title: str,
    index: int | None = None,
) -> dict:
    """Add a new tab (sheet) to a spreadsheet.

    USE WHEN: a spreadsheet needs MORE than one tab —
    ``gsheets_create_spreadsheet`` only makes the single default tab,
    so this is how you get a second/third tab (a "Summary" tab, a
    per-month tab, a "Raw Data" tab, etc.).

    Uses Sheets' ``spreadsheets.batchUpdate`` with an ``addSheet``
    request (via the reusable builder in ``services/sheets/batch.py``).
    Sheets assigns the new tab's numeric ``sheet_id`` (gid) — returned
    here so you can immediately target it with ``gsheets_write_range``
    (``"<title>!A1"``), ``gsheets_format_range`` (needs the gid),
    ``gsheets_rename_sheet`` or ``gsheets_delete_sheet``.

    Args:
        spreadsheet_id: The spreadsheet ID.
        title: Name for the new tab. Must be UNIQUE within the
            spreadsheet — a duplicate tab name is rejected by Sheets.
        index: 0-based position among existing tabs (``0`` = leftmost).
            Omit to append the new tab after the last one.

    Returns:
        ``{spreadsheet_id, sheet_id, title, index}`` — ``sheet_id`` is
        the gid Sheets assigned the new tab (pass it to the gid-based
        tools); ``title`` / ``index`` echo the created tab's properties.

    Choreography: follows ``gsheets_create_spreadsheet`` when you need
    multiple tabs; precedes ``gsheets_write_range`` /
    ``gsheets_format_range`` (which target the returned ``sheet_id`` /
    the tab name).
    """
    return _add_sheet(creds, spreadsheet_id, title, index=index)


# ---------------------------------------------------------------------
# 7. gsheets_delete_sheet — batchUpdate (deleteSheet)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Delete a tab (sheet) from a Google Sheets spreadsheet",
    readonly=False,
    # Removing a tab deletes its data — genuinely destructive (unlike a
    # cell overwrite, the tab + contents are gone). Matches the
    # gdocs_delete_tab convention.
    destructive=True,
    # Deleting the same gid twice 400s rather than double-deleting, so
    # the OUTCOME is idempotent in intent; annotated True to match
    # gdocs_delete_tab. (The api layer still dispatches non-retried to
    # honor the destructive-op safety floor.)
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_DELETE_SHEET_OUTPUT_SCHEMA,
)
def gsheets_delete_sheet(
    creds,
    spreadsheet_id: str,
    sheet_id: int,
) -> dict:
    """Delete a tab (sheet) from a spreadsheet — removes its data too.

    USE WHEN: a tab is no longer needed (a scratch tab, an obsolete
    month). DESTRUCTIVE: the tab and ALL its cell data are removed.

    Uses Sheets' ``spreadsheets.batchUpdate`` with a ``deleteSheet``
    request. A spreadsheet must keep at least one tab — Sheets REJECTS
    deleting the last remaining sheet (surfaced as an error).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_id: The numeric tab id — the ``gid``, NOT the tab name and
            NOT the spreadsheet id. The first/default tab is ``0``; find
            a tab's gid in its URL (``#gid=...``) or from
            ``gsheets_add_sheet``'s returned ``sheet_id``.

    Returns:
        ``{spreadsheet_id, deleted_sheet_id}`` — ``deleted_sheet_id``
        echoes the gid that was removed.

    Choreography: get the gid from ``gsheets_add_sheet`` (when removing
    a tab you just made) or the tab URL. To merely RENAME a tab instead
    of deleting it, use ``gsheets_rename_sheet``.
    """
    return _delete_sheet(creds, spreadsheet_id, sheet_id)


# ---------------------------------------------------------------------
# 8. gsheets_rename_sheet — batchUpdate (updateSheetProperties)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Rename a tab (sheet) in a Google Sheets spreadsheet",
    # Renaming is an in-place property change — not destructive (the
    # tab + data are untouched; only the name changes). Matches
    # gdocs_rename_tab.
    readonly=False,
    destructive=False,
    # Renaming to the same title twice yields the same state — safe to
    # retry. Same convention as gdocs_rename_tab.
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_RENAME_SHEET_OUTPUT_SCHEMA,
)
def gsheets_rename_sheet(
    creds,
    spreadsheet_id: str,
    sheet_id: int,
    title: str,
) -> dict:
    """Rename a tab (sheet) — changes only the name, not its data.

    USE WHEN: a tab needs a clearer name (the default ``Sheet1``, or
    renaming a tab created with a placeholder name).

    Uses Sheets' ``spreadsheets.batchUpdate`` with an
    ``updateSheetProperties`` request masked to exactly the ``title``
    field — so the tab's position, contents, and other properties are
    left untouched.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_id: The numeric tab id — the ``gid``, NOT the tab name.
            The first/default tab is ``0`` (find a tab's gid in its URL
            ``#gid=...`` or from ``gsheets_add_sheet``).
        title: The new tab name. Must be UNIQUE within the spreadsheet
            (a duplicate is rejected by Sheets).

    Returns:
        ``{spreadsheet_id, sheet_id, title}`` — ``title`` echoes the
        new (stripped) name.

    Choreography: get the gid from ``gsheets_add_sheet`` or the tab URL.
    Pairs with ``gsheets_create_spreadsheet`` to rename its default
    ``Sheet1`` tab into something meaningful.
    """
    return _rename_sheet(creds, spreadsheet_id, sheet_id, title)


# ---------------------------------------------------------------------
# 9. gsheets_apply_conditional_format — batchUpdate (addConditionalFormatRule)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Add a conditional-format rule to a Google Sheet range",
    # Adding a rule layers formatting on top of existing cells without
    # altering their values — not "destructive" (same as gsheets_format_range).
    readonly=False,
    destructive=False,
    # NOT idempotent: addConditionalFormatRule APPENDS a rule, so re-running
    # stacks a SECOND identical rule. Unlike gsheets_format_range's
    # repeatCell (which overwrites to the same state). Same convention as
    # gsheets_append_rows. The api layer dispatches idempotent=False.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSHEETS_APPLY_CONDITIONAL_FORMAT_OUTPUT_SCHEMA,
)
def gsheets_apply_conditional_format(
    creds,
    spreadsheet_id: str,
    sheet_id: int,
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
    """Highlight cells that meet a condition (conditional formatting).

    USE WHEN: the agent should make a spreadsheet self-highlight by RULE
    rather than by fixed formatting — e.g. "shade overdue rows red",
    "bold totals over 1000", "flag blank cells", "color cells containing
    'FAIL'". Unlike ``gsheets_format_range`` (which formats cells
    unconditionally, right now), this installs a LIVE rule: Sheets
    re-applies it automatically as the data changes.

    Uses Sheets' ``spreadsheets.batchUpdate`` with a single
    ``addConditionalFormatRule`` request (a BooleanRule), composed via the
    reusable request-builder in ``services/sheets/batch.py`` (the same
    batchUpdate plumbing ``gsheets_format_range`` uses).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_id: The numeric sheet (tab) id — the ``gid``, NOT the tab
            name and NOT the spreadsheet id. The first/default tab is
            ``0`` (find a tab's gid in its URL ``#gid=...``).
        condition_type: a Sheets ``ConditionType`` — common values:
            ``"NUMBER_GREATER"``, ``"NUMBER_LESS"``, ``"NUMBER_BETWEEN"``,
            ``"NUMBER_EQ"``, ``"TEXT_CONTAINS"``, ``"TEXT_EQ"``,
            ``"TEXT_STARTS_WITH"``, ``"DATE_BEFORE"``, ``"DATE_AFTER"``,
            ``"BLANK"``, ``"NOT_BLANK"``, ``"CUSTOM_FORMULA"``. Passed to
            Sheets verbatim (an invalid value surfaces Google's own enum
            error).
        start_row / end_row / start_col / end_col: 0-based, half-open cell
            bounds (``end`` EXCLUSIVE, like a Python slice — so
            ``start_row=0, end_row=1`` is just the first row). Omit a bound
            to leave that side unbounded; omit all four to target the
            whole sheet.
        values: the condition's comparison value(s) as strings — e.g.
            ``["100"]`` for ``NUMBER_GREATER``, ``["10", "20"]`` for
            ``NUMBER_BETWEEN``, ``["FAIL"]`` for ``TEXT_CONTAINS``,
            ``["=A1>AVERAGE(A:A)"]`` for ``CUSTOM_FORMULA``. OMIT for
            valueless conditions (``BLANK`` / ``NOT_BLANK``).
        background_color: ``(r, g, b)`` fill for matching cells, each
            channel in ``[0.0, 1.0]`` (Sheets colors are floats, not 0-255
            ints — red is ``(1.0, 0.0, 0.0)``).
        bold: when ``True``, bold matching cells. Pass at least one of
            ``background_color`` / ``bold`` (a rule with no format does
            nothing — an all-``None`` format raises ValueError).
        index: priority among existing rules (``0`` = highest, evaluated
            first).

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — ``total_requests``
        is 1; ``replies`` is Sheets' raw reply list.

    Choreography: typically follows ``gsheets_write_range`` /
    ``gsheets_append_rows`` (highlight the data you just wrote). Get the
    ``sheet_id`` (gid) from ``gsheets_add_sheet`` or the tab URL. Re-running
    ADDS another rule (rules stack) rather than replacing — apply each
    distinct highlight once.
    """
    return _apply_conditional_format(
        creds,
        spreadsheet_id,
        sheet_id,
        condition_type=condition_type,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
        values=values,
        background_color=background_color,
        bold=bold,
        index=index,
    )


# ---------------------------------------------------------------------
# 10. gsheets_clear_range — values.clear (values-only wipe)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Clear cell values in a Google Sheet range",
    readonly=False,
    # Clearing VALUES leaves formatting + the sheet itself intact and the
    # cells can be re-written — not "destructive" in our sense (matches
    # gsheets_write_range, which also overwrites cell contents in place).
    # gsheets_delete_sheet (removes a whole tab + its data) IS destructive;
    # this is not.
    destructive=False,
    # Clearing an already-cleared range yields the same empty state — safe
    # to retry. (The api layer dispatches it idempotent=True.)
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_CLEAR_RANGE_OUTPUT_SCHEMA,
)
def gsheets_clear_range(
    creds,
    spreadsheet_id: str,
    range: str,
) -> dict:
    """Clear cell VALUES in a range — keeps formatting, keeps the tab.

    USE WHEN: the agent needs to empty cells (wipe stale data before a
    fresh write, clear a scratch area) WITHOUT removing their formatting
    or deleting the tab. This is the values-only counterpart to
    ``gsheets_write_range`` — bold/colors/number-formats/validation in
    the cleared range survive. To remove an ENTIRE tab (data AND
    formatting AND the tab), use ``gsheets_delete_sheet`` instead.

    Uses Sheets' ``spreadsheets.values.clear`` REST endpoint.

    Args:
        spreadsheet_id: The spreadsheet ID.
        range: A1-notation range to clear, e.g. ``"A1:Z1000"`` (default
            tab) or ``"Sheet2!B2:D10"`` (named tab + range). Required —
            state exactly what to wipe (there is no clear-everything
            default).

    Returns:
        ``{spreadsheet_id, cleared_range}`` — ``cleared_range`` is the A1
        range Sheets reports it cleared (echoed back so you can confirm
        what was wiped).

    Choreography: often precedes ``gsheets_write_range`` (clear stale
    rows, then write fresh ones). To clear AND remove the tab, use
    ``gsheets_delete_sheet``.
    """
    return _clear_range(creds, spreadsheet_id, range)


# ---------------------------------------------------------------------
# 11. gsheets_duplicate_sheet — batchUpdate (duplicateSheet)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Duplicate a tab (sheet) in a Google Sheets spreadsheet",
    # Adds a fresh tab (a copy) — not a mutation of existing tabs. Matches
    # gsheets_add_sheet's annotations.
    readonly=False,
    destructive=False,
    # NOT idempotent: re-running creates ANOTHER copy. Same convention as
    # gsheets_add_sheet / gsheets_create_spreadsheet.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSHEETS_DUPLICATE_SHEET_OUTPUT_SCHEMA,
)
def gsheets_duplicate_sheet(
    creds,
    spreadsheet_id: str,
    source_sheet_id: int,
    new_sheet_name: str | None = None,
    insert_index: int | None = None,
) -> dict:
    """Duplicate a tab (sheet) — a full copy (values + formatting).

    USE WHEN: the agent needs a copy of an existing tab — a per-month
    sheet seeded from a template tab, a "scratch" copy to edit without
    touching the original, a backup before a risky transformation. The
    copy includes everything: values, formats, conditional rules, charts.

    Uses Sheets' ``spreadsheets.batchUpdate`` with a ``duplicateSheet``
    request (via the reusable builder in ``services/sheets/batch.py``).
    Sheets assigns the copy a fresh numeric ``sheet_id`` (gid) — returned
    here so you can immediately target it with the gid-based tools.

    Args:
        spreadsheet_id: The spreadsheet ID.
        source_sheet_id: The numeric tab id (``gid``) of the tab to COPY —
            NOT the tab name and NOT the spreadsheet id. The first/default
            tab is ``0`` (find a tab's gid in its URL ``#gid=...`` or from
            ``gsheets_add_sheet``).
        new_sheet_name: Name for the copy. Omit to let Sheets auto-name it
            (``"Copy of <source>"``). Must be UNIQUE — a duplicate name is
            rejected by Sheets.
        insert_index: 0-based position for the copy among existing tabs
            (``0`` = leftmost). Omit to let Sheets place it right after the
            source tab.

    Returns:
        ``{spreadsheet_id, sheet_id, title, index}`` — ``sheet_id`` is the
        gid Sheets assigned the copy (pass it to the gid-based tools);
        ``title`` / ``index`` echo the copy's properties.

    Choreography: get the source gid from ``gsheets_add_sheet`` or the tab
    URL. Follow with ``gsheets_rename_sheet`` /
    ``gsheets_write_range`` (target the returned ``sheet_id``) to customise
    the copy.
    """
    return _duplicate_sheet(
        creds,
        spreadsheet_id,
        source_sheet_id,
        new_sheet_name=new_sheet_name,
        insert_index=insert_index,
    )


# ---------------------------------------------------------------------
# 12. gsheets_freeze — batchUpdate (updateSheetProperties / gridProperties)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Freeze header rows/columns of a Google Sheet tab",
    # An in-place view property change — not destructive (values + other
    # properties untouched; only the frozen counts change). Matches
    # gsheets_rename_sheet.
    readonly=False,
    destructive=False,
    # Setting the same frozen counts twice yields the same state — safe to
    # retry. (The api layer dispatches it idempotent=True.)
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSHEETS_FREEZE_OUTPUT_SCHEMA,
)
def gsheets_freeze(
    creds,
    spreadsheet_id: str,
    sheet_id: int,
    frozen_row_count: int | None = None,
    frozen_column_count: int | None = None,
) -> dict:
    """Freeze header rows and/or columns so they stay visible on scroll.

    USE WHEN: a sheet has a header row (or label column) that should stay
    pinned while the data scrolls — the standard "freeze row 1" you'd do
    by hand in the UI. Freeze the top N rows (``frozen_row_count``) and/or
    the left N columns (``frozen_column_count``).

    Uses Sheets' ``spreadsheets.batchUpdate`` with an
    ``updateSheetProperties`` request masked to exactly the
    ``gridProperties.frozen*Count`` field(s) you set — so the other frozen
    count and every other sheet property are left untouched.

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_id: The numeric tab id — the ``gid``, NOT the tab name. The
            first/default tab is ``0`` (find a tab's gid in its URL
            ``#gid=...`` or from ``gsheets_add_sheet``).
        frozen_row_count: Number of rows to freeze from the top (e.g. ``1``
            to pin a header row). Pass ``0`` to UNFREEZE rows. Omit to
            leave the row freeze unchanged.
        frozen_column_count: Number of columns to freeze from the left.
            Pass ``0`` to UNFREEZE columns. Omit to leave the column freeze
            unchanged.

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — ``total_requests``
        is 1 (one updateSheetProperties); ``replies`` is Sheets' raw reply
        list.

    Choreography: typically follows ``gsheets_write_range`` (write a header
    row, then ``gsheets_freeze(frozen_row_count=1)`` to pin it). Pass at
    least one of the two counts — an all-omitted call raises a ValueError
    rather than issuing a no-op batchUpdate.
    """
    return _freeze(
        creds,
        spreadsheet_id,
        sheet_id,
        frozen_row_count=frozen_row_count,
        frozen_column_count=frozen_column_count,
    )


# ---------------------------------------------------------------------
# 13. gsheets_protect_range — batchUpdate (addProtectedRange)
# ---------------------------------------------------------------------


@workspace_tool(
    service="sheets",
    title="Protect a cell range in a Google Sheet",
    # Adds a protection rule on top of existing cells without altering
    # their values — not "destructive" (same posture as
    # gsheets_apply_conditional_format, which also layers a rule).
    readonly=False,
    destructive=False,
    # NOT idempotent: addProtectedRange APPENDS a protected range, so
    # re-running adds a SECOND overlapping protection. Same convention as
    # gsheets_apply_conditional_format / gsheets_append_rows. The api layer
    # dispatches idempotent=False.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSHEETS_PROTECT_RANGE_OUTPUT_SCHEMA,
)
def gsheets_protect_range(
    creds,
    spreadsheet_id: str,
    sheet_id: int,
    start_row: int | None = None,
    end_row: int | None = None,
    start_col: int | None = None,
    end_col: int | None = None,
    description: str | None = None,
    warning_only: bool = False,
    editor_emails: list[str] | None = None,
) -> dict:
    """Protect a cell range — restrict (or warn on) edits to a block.

    USE WHEN: the agent should guard cells from edits — lock a formula
    column, freeze a finalized report area, or (soft mode) warn editors
    before they change a sensitive range. Two modes:

      * ``warning_only=True`` — edits show an "are you sure?" warning but
        are NOT blocked (a gentle guard against accidental changes).
      * ``warning_only=False`` (default) — edits are BLOCKED for everyone
        except the listed ``editor_emails`` (and the owner). With no
        editors, only the owner can edit.

    Uses Sheets' ``spreadsheets.batchUpdate`` with an ``addProtectedRange``
    request (via the reusable builder in ``services/sheets/batch.py``).

    Args:
        spreadsheet_id: The spreadsheet ID.
        sheet_id: The numeric tab id — the ``gid``, NOT the tab name. The
            first/default tab is ``0`` (find a tab's gid in its URL
            ``#gid=...``).
        start_row / end_row / start_col / end_col: 0-based, half-open cell
            bounds (``end`` EXCLUSIVE, like a Python slice — so
            ``start_row=0, end_row=1`` is just the first row). Omit a bound
            to leave that side unbounded; omit all four to protect the
            WHOLE sheet.
        description: Optional label for the protected range (shown in the
            Sheets protection UI).
        warning_only: When ``True``, edits warn but aren't blocked; when
            ``False`` (default), edits are restricted to ``editor_emails``
            (+ owner). Incompatible with ``editor_emails``.
        editor_emails: Email addresses allowed to edit the protected range
            (ignored when ``warning_only=True``). Omit / empty means only
            the owner can edit.

    Returns:
        ``{spreadsheet_id, total_requests, replies}`` — ``total_requests``
        is 1 (one addProtectedRange); ``replies`` is Sheets' raw reply
        list.

    Choreography: get the ``sheet_id`` (gid) from ``gsheets_add_sheet`` or
    the tab URL. Re-running ADDS another protected range (they stack)
    rather than replacing — protect each distinct range once.
    """
    return _protect_range(
        creds,
        spreadsheet_id,
        sheet_id,
        start_row=start_row,
        end_row=end_row,
        start_col=start_col,
        end_col=end_col,
        description=description,
        warning_only=warning_only,
        editor_emails=editor_emails,
    )
