"""Co-located tests for services/sheets/api.py (v2.3.1).

Mirrors ``tests/unit/services/drive/test_sharing.py`` (PR #117):
exercise the module via ``with_google_api_client(InMemoryGoogleAPIClient)``
so the real ``get_service`` chokepoint runs but Sheets' HTTP boundary
is stubbed. No real OAuth, no real Sheets round-trip.

Tests cover three surfaces:

1. **Module-level constants** — pin ``DEFAULT_RANGE`` as the public
   surface; a stray edit (e.g. shrinking to ``"A1:A100"``) would
   surprise callers depending on the documented default.
2. **Pre-API validation** — ``write_range``'s ``ValueError`` branches
   for empty / non-list-of-lists values; ``create_spreadsheet``'s
   blank-title rejection.
3. **Sheets call shape** — the right method chain
   (``sheets.spreadsheets().values().get`` / ``.update`` /
   ``sheets.spreadsheets().create``) receives the right kwargs:
   ``valueInputOption="USER_ENTERED"`` on writes (pinned so a
   future "let me try RAW for a second" experiment fires the
   guard), the ``fields`` mask on ``create``, the body shape on
   each call.
4. **Response envelope shape** — the flat ``{range, values}`` /
   ``{updated_range, updated_cells}`` / ``{spreadsheet_id, url,
   title}`` envelopes the tool layer surfaces.

The empirical-validation framing of v2.3.1: this test file is the
proof that the M2 chokepoint + per-service-folder pattern + M4
``@workspace_tool`` annotation surface scale to a NEW Google service
without infrastructure rework. Drive sharing (PR #117) was the
single-folder-bolt-on proof; sheets is the new-service proof.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.sheets.api import (
    DEFAULT_RANGE,
    add_chart,
    add_sheet,
    append_rows,
    apply_conditional_format,
    clear_range,
    create_spreadsheet,
    delete_dimension,
    delete_sheet,
    duplicate_sheet,
    format_range,
    freeze,
    insert_dimension,
    merge_cells,
    protect_range,
    read_range,
    rename_sheet,
    set_data_validation,
    write_range,
)


# ---------------------------------------------------------------------
# Module-level constants — public surface canary
# ---------------------------------------------------------------------


def test_default_range_is_A1_through_Z1000():
    """A1:Z1000 = 26 columns × 1000 rows. Pinned so a stray edit
    that shrinks the default doesn't silently break callers who
    rely on the documented size."""
    assert DEFAULT_RANGE == "A1:Z1000"


# ---------------------------------------------------------------------
# read_range — Sheets call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_read():
    """A Sheets v4 Resource stub whose
    spreadsheets().values().get().execute() returns a plausible
    Sheets response. Enough to let read_range complete and let us
    inspect its call args + response envelope."""
    sheets = MagicMock(name="sheets-v4-stub-read")
    sheets.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:Z1000",
        "majorDimension": "ROWS",
        "values": [["a", "b"], ["c", "d"]],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_get_kwargs(sheets: MagicMock) -> dict:
    """The kwargs of the most recent values().get(...) call that
    actually carried a ``spreadsheetId``. Mirrors the helper pattern
    from ``test_sharing.py::_last_create_kwargs``."""
    for call in reversed(sheets.spreadsheets().values().get.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no values().get() call captured spreadsheetId")


def test_read_range_passes_spreadsheetId_to_sheets(stub_sheets_for_read):
    """The Sheets call must target the spreadsheet_id the caller passed."""
    read_range(MagicMock(), "SPREAD-ABC")
    kw = _last_get_kwargs(stub_sheets_for_read)
    assert kw["spreadsheetId"] == "SPREAD-ABC"


def test_read_range_default_range_when_caller_omits(stub_sheets_for_read):
    """Omitted range falls back to DEFAULT_RANGE — A1:Z1000."""
    read_range(MagicMock(), "SPREAD1")
    kw = _last_get_kwargs(stub_sheets_for_read)
    assert kw["range"] == DEFAULT_RANGE


def test_read_range_passes_caller_supplied_range_through(stub_sheets_for_read):
    """Explicit range is forwarded verbatim — including sheet-prefixed
    forms like ``Sheet2!B2:D10``."""
    read_range(MagicMock(), "SPREAD1", "Sheet2!B2:D10")
    kw = _last_get_kwargs(stub_sheets_for_read)
    assert kw["range"] == "Sheet2!B2:D10"


def test_read_range_returns_flat_envelope(stub_sheets_for_read):
    """The returned dict is the flat ``{range, values}`` envelope —
    ``range`` echoes back the Sheets-canonical form (which may differ
    from the requested form when Sheets normalizes)."""
    result = read_range(MagicMock(), "SPREAD-ABC", "A1:B2")
    assert result == {
        "range": "Sheet1!A1:Z1000",  # the stubbed Sheets canonical form
        "values": [["a", "b"], ["c", "d"]],
    }


def test_read_range_returns_empty_values_for_blank_range(stub_sheets_for_read):
    """Sheets omits the ``values`` key entirely for a fully-blank
    range; the envelope defaults to ``[]`` rather than missing key.
    Consumers can iterate ``result["values"]`` without a KeyError."""
    stub_sheets_for_read.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:Z1000",
        "majorDimension": "ROWS",
        # No ``values`` key — what Sheets returns for an empty range.
    }
    result = read_range(MagicMock(), "SPREAD1", "A1:Z1000")
    assert result["values"] == []


def test_read_range_returns_range_fallback_when_sheets_omits_it(
    stub_sheets_for_read,
):
    """Defensive: if Sheets ever omits ``range`` from the response
    (shouldn't, but the SDK contract permits it), the envelope falls
    back to the requested range rather than KeyError."""
    stub_sheets_for_read.spreadsheets().values().get().execute.return_value = {
        "values": [["x"]],
    }
    result = read_range(MagicMock(), "SPREAD1", "A1:A1")
    assert result["range"] == "A1:A1"


# ---------------------------------------------------------------------
# write_range — pre-API validation + Sheets call shape + envelope
# ---------------------------------------------------------------------


def test_write_range_rejects_empty_values():
    """Empty ``values`` is a caller bug — Sheets would 400 with a
    less-helpful message. Reject client-side."""
    with pytest.raises(ValueError, match="values cannot be empty"):
        write_range(MagicMock(), "SPREAD1", "A1", [])


def test_write_range_rejects_non_list_of_lists():
    """A flat list (forgetting the outer wrapper) is the most common
    caller mistake. Reject with a message that explains the 2D shape."""
    with pytest.raises(ValueError, match="2D row-major"):
        write_range(MagicMock(), "SPREAD1", "A1", ["a", "b", "c"])


def test_write_range_rejects_mixed_row_types():
    """A list-of-lists with one non-list entry buried inside is also
    a 2D-shape violation — caught by the ``all(isinstance(...))``
    check."""
    with pytest.raises(ValueError, match="2D row-major"):
        write_range(MagicMock(), "SPREAD1", "A1", [["a"], "not-a-row", ["b"]])


@pytest.fixture
def stub_sheets_for_write():
    sheets = MagicMock(name="sheets-v4-stub-write")
    sheets.spreadsheets().values().update().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "updatedRange": "Sheet1!A1:B2",
        "updatedRows": 2,
        "updatedColumns": 2,
        "updatedCells": 4,
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_update_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().values().update.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no values().update() call captured spreadsheetId")


def test_write_range_passes_spreadsheetId_and_range(stub_sheets_for_write):
    write_range(MagicMock(), "SPREAD-XYZ", "A1:B2", [["a", "b"], ["c", "d"]])
    kw = _last_update_kwargs(stub_sheets_for_write)
    assert kw["spreadsheetId"] == "SPREAD-XYZ"
    assert kw["range"] == "A1:B2"


def test_write_range_uses_USER_ENTERED_value_input_option(stub_sheets_for_write):
    """PINNED INVARIANT: ``valueInputOption`` DEFAULTS to ``USER_ENTERED``
    — formulas / dates parse as the user typed them. A regression that
    flipped the default to RAW (silently breaking formula support) fires
    this guard."""
    write_range(MagicMock(), "SPREAD1", "A1", [["=SUM(B1:B10)"]])
    kw = _last_update_kwargs(stub_sheets_for_write)
    assert kw["valueInputOption"] == "USER_ENTERED"


def test_write_range_RAW_value_input_option_keeps_leading_equals_literal(
    stub_sheets_for_write,
):
    """RAW SAFETY (the headline of this enhancement): with
    ``value_input_option="RAW"``, a value with a leading ``=`` is sent to
    Sheets under RAW — so Sheets stores it as the literal string ``"=1+1"``
    rather than evaluating it to the formula result ``2``. We assert BOTH
    that RAW reaches the API AND that the literal value is forwarded
    unchanged (the body is not mangled), which together prove the
    leading-``=`` stays literal."""
    write_range(
        MagicMock(), "SPREAD1", "A1", [["=1+1"]],
        value_input_option="RAW",
    )
    kw = _last_update_kwargs(stub_sheets_for_write)
    assert kw["valueInputOption"] == "RAW"
    # The literal value is forwarded verbatim — under RAW, Sheets keeps it
    # as the 4-char string "=1+1", NOT the number 2.
    assert kw["body"] == {"values": [["=1+1"]]}


def test_write_range_rejects_unknown_value_input_option(stub_sheets_for_write):
    """A typo'd option (e.g. lowercase ``"raw"``) is rejected client-side
    with a message naming the two valid options — rather than bouncing off
    a generic Google 400."""
    with pytest.raises(ValueError, match="value_input_option must be one of"):
        write_range(
            MagicMock(), "SPREAD1", "A1", [["x"]],
            value_input_option="raw",
        )


def test_write_range_wraps_values_in_body(stub_sheets_for_write):
    """The Sheets API expects ``body={"values": [[...]]}`` — the
    values list goes UNDER the ``values`` key, not at body root.
    A misplaced wrapper would cause Sheets to write nothing
    (and not error — silent data loss)."""
    write_range(MagicMock(), "SPREAD1", "A1:B1", [["x", "y"]])
    kw = _last_update_kwargs(stub_sheets_for_write)
    assert kw["body"] == {"values": [["x", "y"]]}


def test_write_range_returns_flat_envelope(stub_sheets_for_write):
    """The response envelope maps Sheets' ``updatedRange`` →
    ``updated_range`` and ``updatedCells`` → ``updated_cells``
    (snake_case in the public surface)."""
    result = write_range(MagicMock(), "SPREAD1", "A1:B2", [["a", "b"], ["c", "d"]])
    assert result == {
        "updated_range": "Sheet1!A1:B2",
        "updated_cells": 4,
    }


def test_write_range_returns_zero_cells_when_sheets_omits_field(
    stub_sheets_for_write,
):
    """Defensive: if Sheets ever omits ``updatedCells`` from the
    response, the envelope defaults to 0 rather than KeyError."""
    stub_sheets_for_write.spreadsheets().values().update().execute.return_value = {
        "updatedRange": "Sheet1!A1:A1",
    }
    result = write_range(MagicMock(), "SPREAD1", "A1", [["x"]])
    assert result == {"updated_range": "Sheet1!A1:A1", "updated_cells": 0}


# ---------------------------------------------------------------------
# create_spreadsheet — pre-API validation + Sheets call shape + envelope
# ---------------------------------------------------------------------


def test_create_spreadsheet_rejects_blank_title():
    """Empty / whitespace title rejected client-side. Sheets would
    accept it (the new spreadsheet would just have an empty Drive
    name), but that's never what an MCP caller wants."""
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_spreadsheet(MagicMock(), "")
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_spreadsheet(MagicMock(), "   ")


@pytest.fixture
def stub_sheets_for_create():
    sheets = MagicMock(name="sheets-v4-stub-create")
    sheets.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "NEW-SHEET-ID-001",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/NEW-SHEET-ID-001/edit",
        "properties": {"title": "My Sheet"},
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_create_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().create.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs
    raise AssertionError("no spreadsheets().create() call captured body")


def test_create_spreadsheet_builds_properties_title_body(stub_sheets_for_create):
    """The create call body must wrap the title inside
    ``{"properties": {"title": ...}}`` — Sheets' documented shape."""
    create_spreadsheet(MagicMock(), "My Sheet")
    kw = _last_create_kwargs(stub_sheets_for_create)
    assert kw["body"] == {"properties": {"title": "My Sheet"}}


def test_create_spreadsheet_strips_whitespace_from_title(stub_sheets_for_create):
    """Leading / trailing whitespace stripped before the Sheets call,
    so the created spreadsheet's Drive name + tab name don't have
    surprise spaces."""
    create_spreadsheet(MagicMock(), "  My Sheet  ")
    kw = _last_create_kwargs(stub_sheets_for_create)
    assert kw["body"]["properties"]["title"] == "My Sheet"


def test_create_spreadsheet_requests_minimal_fields_mask(stub_sheets_for_create):
    """The ``fields`` mask limits the Sheets response to what the
    envelope needs (id + title + URL). Sheets returns a much larger
    object by default."""
    create_spreadsheet(MagicMock(), "My Sheet")
    kw = _last_create_kwargs(stub_sheets_for_create)
    assert kw["fields"] == "spreadsheetId,properties.title,spreadsheetUrl"


def test_create_spreadsheet_returns_flat_envelope(stub_sheets_for_create):
    """Maps Sheets' ``spreadsheetId`` → ``spreadsheet_id`` (snake_case)
    and ``spreadsheetUrl`` → ``url`` (shortened name) so the caller
    doesn't have to learn Sheets' vocabulary."""
    result = create_spreadsheet(MagicMock(), "My Sheet")
    assert result == {
        "spreadsheet_id": "NEW-SHEET-ID-001",
        "url": "https://docs.google.com/spreadsheets/d/NEW-SHEET-ID-001/edit",
        "title": "My Sheet",
    }


def test_create_spreadsheet_synthesizes_url_when_sheets_omits_it(
    stub_sheets_for_create,
):
    """Defensive: if Sheets ever omits ``spreadsheetUrl``, the
    envelope synthesizes the canonical URL from the ID rather than
    leaving a missing key."""
    stub_sheets_for_create.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "ABC123",
        "properties": {"title": "T"},
    }
    result = create_spreadsheet(MagicMock(), "T")
    assert result["url"] == "https://docs.google.com/spreadsheets/d/ABC123/edit"


def test_create_spreadsheet_falls_back_to_input_title_when_omitted(
    stub_sheets_for_create,
):
    """Defensive: if Sheets ever omits the title from its response,
    the envelope falls back to the (stripped) input title."""
    stub_sheets_for_create.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "ABC123",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/ABC123/edit",
        # No ``properties`` key.
    }
    result = create_spreadsheet(MagicMock(), "  Fallback Title  ")
    assert result["title"] == "Fallback Title"


# ---------------------------------------------------------------------
# format_range — composes the batch builder + dispatches batchUpdate
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_format():
    sheets = MagicMock(name="sheets-v4-stub-format")
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_format_batch_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().batchUpdate.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate() call captured spreadsheetId")


def test_format_range_builds_repeat_cell_request(stub_sheets_for_format):
    """The api layer composes exactly ONE repeatCell request from the
    flat kwargs and dispatches it via batchUpdate."""
    format_range(
        MagicMock(),
        "SPREAD-ABC",
        sheet_id=0,
        start_row=0,
        end_row=1,
        start_col=0,
        end_col=3,
        bold=True,
    )
    kw = _last_format_batch_kwargs(stub_sheets_for_format)
    assert kw["spreadsheetId"] == "SPREAD-ABC"
    requests = kw["body"]["requests"]
    assert len(requests) == 1
    rc = requests[0]["repeatCell"]
    assert rc["range"] == {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert rc["cell"] == {"userEnteredFormat": {"textFormat": {"bold": True}}}
    assert rc["fields"] == "userEnteredFormat.textFormat.bold"


def test_format_range_converts_color_tuples(stub_sheets_for_format):
    """``(r, g, b)`` tuples become Sheets Color dicts (foreground under
    textFormat, background at the top level)."""
    format_range(
        MagicMock(),
        "SPREAD1",
        sheet_id=0,
        foreground_color=(1.0, 0.0, 0.0),
        background_color=(0.0, 0.0, 1.0),
    )
    kw = _last_format_batch_kwargs(stub_sheets_for_format)
    fmt = kw["body"]["requests"][0]["repeatCell"]["cell"]["userEnteredFormat"]
    assert fmt["textFormat"]["foregroundColor"] == {
        "red": 1.0, "green": 0.0, "blue": 0.0,
    }
    assert fmt["backgroundColor"] == {"red": 0.0, "green": 0.0, "blue": 1.0}


def test_format_range_returns_batch_envelope(stub_sheets_for_format):
    result = format_range(MagicMock(), "SPREAD1", sheet_id=0, bold=True)
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "total_requests": 1,
        "replies": [{}],
    }


def test_format_range_rejects_empty_format(stub_sheets_for_format):
    """No format options -> ValueError (empty repeatCell is a no-op
    Sheets rejects), surfaced before the round-trip by the builder."""
    with pytest.raises(ValueError, match="fmt is empty"):
        format_range(MagicMock(), "SPREAD1", sheet_id=0)


def test_format_range_propagates_grid_validation(stub_sheets_for_format):
    """An inverted GridRange is caught by the builder, not Sheets."""
    with pytest.raises(ValueError, match="end_row.*must be >"):
        format_range(
            MagicMock(), "SPREAD1", sheet_id=0,
            start_row=5, end_row=2, bold=True,
        )


# ---------------------------------------------------------------------
# append_rows — values.append (race-free), call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_append():
    sheets = MagicMock(name="sheets-v4-stub-append")
    sheets.spreadsheets().values().append().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "tableRange": "Sheet1!A1:B2",
        "updates": {
            "spreadsheetId": "SPREAD1",
            "updatedRange": "Sheet1!A3:B3",
            "updatedRows": 1,
            "updatedColumns": 2,
            "updatedCells": 2,
        },
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_append_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().values().append.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no values().append() call captured spreadsheetId")


def test_append_rows_rejects_empty_values():
    with pytest.raises(ValueError, match="values cannot be empty"):
        append_rows(MagicMock(), "SPREAD1", [])


def test_append_rows_rejects_non_list_of_lists():
    with pytest.raises(ValueError, match="2D row-major"):
        append_rows(MagicMock(), "SPREAD1", ["a", "b"])


def test_append_rows_uses_append_endpoint_not_update(stub_sheets_for_append):
    """The race-free path MUST go through values().append (server-side
    last-row detection), NOT values().update (the racey precomputed-range
    pattern)."""
    append_rows(MagicMock(), "SPREAD1", [["x", "y"]])
    assert stub_sheets_for_append.spreadsheets().values().append.called
    assert not stub_sheets_for_append.spreadsheets().values().update.call_args_list


def test_append_rows_passes_insert_rows_and_user_entered(stub_sheets_for_append):
    """PINNED INVARIANTS: insertDataOption=INSERT_ROWS (push existing rows
    down, never overwrite) + valueInputOption DEFAULTS to USER_ENTERED
    (formulas/dates parse, consistent with write_range)."""
    append_rows(MagicMock(), "SPREAD-XYZ", [["=SUM(A1:A2)"]])
    kw = _last_append_kwargs(stub_sheets_for_append)
    assert kw["spreadsheetId"] == "SPREAD-XYZ"
    assert kw["valueInputOption"] == "USER_ENTERED"
    assert kw["insertDataOption"] == "INSERT_ROWS"
    assert kw["body"] == {"values": [["=SUM(A1:A2)"]]}


def test_append_rows_RAW_keeps_leading_equals_literal(stub_sheets_for_append):
    """RAW SAFETY for append: value_input_option="RAW" reaches the API and
    a leading ``=`` is forwarded verbatim — so Sheets appends the literal
    string ``"=1+1"``, not the evaluated formula. insertDataOption stays
    INSERT_ROWS regardless of the value-input mode."""
    append_rows(
        MagicMock(), "SPREAD1", [["=1+1"]],
        value_input_option="RAW",
    )
    kw = _last_append_kwargs(stub_sheets_for_append)
    assert kw["valueInputOption"] == "RAW"
    assert kw["insertDataOption"] == "INSERT_ROWS"
    assert kw["body"] == {"values": [["=1+1"]]}


def test_append_rows_rejects_unknown_value_input_option(stub_sheets_for_append):
    with pytest.raises(ValueError, match="value_input_option must be one of"):
        append_rows(
            MagicMock(), "SPREAD1", [["x"]],
            value_input_option="USER",
        )


def test_append_rows_default_search_range(stub_sheets_for_append):
    """Omitted range falls back to DEFAULT_RANGE (used to LOCATE the table,
    not as the write destination)."""
    append_rows(MagicMock(), "SPREAD1", [["x"]])
    kw = _last_append_kwargs(stub_sheets_for_append)
    assert kw["range"] == DEFAULT_RANGE


def test_append_rows_returns_envelope_from_updates_block(stub_sheets_for_append):
    """values.append nests the write result under ``updates``; the envelope
    flattens it to {updated_range, updated_cells, updated_rows} — where the
    rows actually LANDED (A3 here, not A1)."""
    result = append_rows(MagicMock(), "SPREAD1", [["x", "y"]])
    assert result == {
        "updated_range": "Sheet1!A3:B3",
        "updated_cells": 2,
        "updated_rows": 1,
    }


def test_append_rows_defaults_counts_when_updates_omitted(stub_sheets_for_append):
    """Defensive: if Sheets omits ``updates``, the envelope defaults counts
    to 0 / range to the search range rather than KeyError."""
    stub_sheets_for_append.spreadsheets().values().append().execute.return_value = {
        "spreadsheetId": "SPREAD1",
    }
    result = append_rows(MagicMock(), "SPREAD1", [["x"]], range_str="A1:Z9")
    assert result == {
        "updated_range": "A1:Z9",
        "updated_cells": 0,
        "updated_rows": 0,
    }


# ---------------------------------------------------------------------
# add_sheet / delete_sheet / rename_sheet — tab lifecycle via batchUpdate
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_lifecycle():
    sheets = MagicMock(name="sheets-v4-stub-lifecycle")
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_lifecycle_batch_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().batchUpdate.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate() call captured spreadsheetId")


def test_add_sheet_dispatches_add_sheet_request(stub_sheets_for_lifecycle):
    """add_sheet composes exactly one addSheet request and dispatches it."""
    add_sheet(MagicMock(), "SPREAD-ABC", "Summary", index=1)
    kw = _last_lifecycle_batch_kwargs(stub_sheets_for_lifecycle)
    assert kw["spreadsheetId"] == "SPREAD-ABC"
    requests = kw["body"]["requests"]
    assert len(requests) == 1
    assert requests[0] == {
        "addSheet": {"properties": {"title": "Summary", "index": 1}}
    }


def test_add_sheet_returns_assigned_gid_from_reply(stub_sheets_for_lifecycle):
    """Sheets assigns the new tab gid server-side; add_sheet surfaces it
    (parsed from replies[0].addSheet.properties.sheetId)."""
    stub_sheets_for_lifecycle.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [
            {"addSheet": {"properties": {
                "sheetId": 998877, "title": "Summary", "index": 1,
            }}}
        ],
    }
    result = add_sheet(MagicMock(), "SPREAD1", "Summary", index=1)
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "sheet_id": 998877,
        "title": "Summary",
        "index": 1,
    }


def test_add_sheet_rejects_blank_title(stub_sheets_for_lifecycle):
    with pytest.raises(ValueError, match="title cannot be empty"):
        add_sheet(MagicMock(), "SPREAD1", "   ")


def test_delete_sheet_dispatches_delete_sheet_request(stub_sheets_for_lifecycle):
    delete_sheet(MagicMock(), "SPREAD-ABC", 12345)
    kw = _last_lifecycle_batch_kwargs(stub_sheets_for_lifecycle)
    assert kw["spreadsheetId"] == "SPREAD-ABC"
    assert kw["body"]["requests"] == [{"deleteSheet": {"sheetId": 12345}}]


def test_delete_sheet_returns_echo_envelope(stub_sheets_for_lifecycle):
    result = delete_sheet(MagicMock(), "SPREAD1", 42)
    assert result == {"spreadsheet_id": "SPREAD1", "deleted_sheet_id": 42}


def test_delete_sheet_rejects_negative_gid(stub_sheets_for_lifecycle):
    with pytest.raises(ValueError, match="sheet_id must be >= 0"):
        delete_sheet(MagicMock(), "SPREAD1", -1)


def test_rename_sheet_dispatches_scoped_update(stub_sheets_for_lifecycle):
    """rename_sheet composes updateSheetProperties masked to title only."""
    rename_sheet(MagicMock(), "SPREAD-ABC", 0, "Renamed")
    kw = _last_lifecycle_batch_kwargs(stub_sheets_for_lifecycle)
    assert kw["body"]["requests"] == [{
        "updateSheetProperties": {
            "properties": {"sheetId": 0, "title": "Renamed"},
            "fields": "title",
        }
    }]


def test_rename_sheet_returns_echo_envelope(stub_sheets_for_lifecycle):
    result = rename_sheet(MagicMock(), "SPREAD1", 7, "  Tidy  ")
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "sheet_id": 7,
        "title": "Tidy",
    }


def test_rename_sheet_rejects_blank_title(stub_sheets_for_lifecycle):
    with pytest.raises(ValueError, match="title cannot be empty"):
        rename_sheet(MagicMock(), "SPREAD1", 0, "")


# ---------------------------------------------------------------------
# apply_conditional_format — composes the builder + dispatches batchUpdate
# ---------------------------------------------------------------------
# (The pure add_conditional_format_rule_request builder is unit-tested in
# test_batch.py; here we cover the api wrapper: grid assembly, color-tuple
# conversion, the request reaching batchUpdate, and the envelope.)


@pytest.fixture
def stub_sheets_for_cond():
    sheets = MagicMock(name="sheets-v4-stub-cond")
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_cond_batch_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().batchUpdate.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate() call captured spreadsheetId")


def test_apply_conditional_format_builds_rule_request(stub_sheets_for_cond):
    """The api layer composes ONE addConditionalFormatRule request with the
    grid, the boolean condition (+ values), and the match format."""
    apply_conditional_format(
        MagicMock(),
        "SPREAD-ABC",
        sheet_id=0,
        condition_type="NUMBER_GREATER",
        start_row=1,
        end_row=100,
        start_col=2,
        end_col=3,
        values=["1000"],
        background_color=(1.0, 0.0, 0.0),
    )
    kw = _last_cond_batch_kwargs(stub_sheets_for_cond)
    assert kw["spreadsheetId"] == "SPREAD-ABC"
    requests = kw["body"]["requests"]
    assert len(requests) == 1
    rule = requests[0]["addConditionalFormatRule"]["rule"]
    assert rule["ranges"] == [{
        "sheetId": 0,
        "startRowIndex": 1,
        "endRowIndex": 100,
        "startColumnIndex": 2,
        "endColumnIndex": 3,
    }]
    boolean = rule["booleanRule"]
    assert boolean["condition"] == {
        "type": "NUMBER_GREATER",
        "values": [{"userEnteredValue": "1000"}],
    }
    assert boolean["format"]["backgroundColor"] == {
        "red": 1.0, "green": 0.0, "blue": 0.0,
    }


def test_apply_conditional_format_valueless_condition(stub_sheets_for_cond):
    """BLANK takes no comparison value — the condition carries no
    ``values`` key, and a bold-only format is accepted."""
    apply_conditional_format(
        MagicMock(),
        "SPREAD1",
        sheet_id=0,
        condition_type="BLANK",
        bold=True,
    )
    kw = _last_cond_batch_kwargs(stub_sheets_for_cond)
    cond = kw["body"]["requests"][0]["addConditionalFormatRule"]["rule"][
        "booleanRule"
    ]["condition"]
    assert cond == {"type": "BLANK"}


def test_apply_conditional_format_returns_batch_envelope(stub_sheets_for_cond):
    result = apply_conditional_format(
        MagicMock(), "SPREAD1", sheet_id=0,
        condition_type="NOT_BLANK", bold=True,
    )
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "total_requests": 1,
        "replies": [{}],
    }


def test_apply_conditional_format_rejects_empty_format(stub_sheets_for_cond):
    """A rule with neither background_color nor bold does nothing — the
    builder rejects it before the round-trip."""
    with pytest.raises(ValueError, match="needs a format to apply"):
        apply_conditional_format(
            MagicMock(), "SPREAD1", sheet_id=0,
            condition_type="NUMBER_GREATER", values=["5"],
        )


def test_apply_conditional_format_propagates_grid_validation(stub_sheets_for_cond):
    """An inverted GridRange is caught by the grid_range builder."""
    with pytest.raises(ValueError, match="end_col.*must be >"):
        apply_conditional_format(
            MagicMock(), "SPREAD1", sheet_id=0,
            condition_type="NOT_BLANK", bold=True,
            start_col=5, end_col=2,
        )


# ---------------------------------------------------------------------
# clear_range — values.clear (values-only wipe), call shape + envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_clear():
    sheets = MagicMock(name="sheets-v4-stub-clear")
    sheets.spreadsheets().values().clear().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "clearedRange": "Sheet1!A1:B2",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_clear_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().values().clear.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no values().clear() call captured spreadsheetId")


def test_clear_range_uses_clear_endpoint(stub_sheets_for_clear):
    """The values-only wipe MUST go through values().clear (formatting
    preserved), NOT values().update with blanks (which the docstring
    distinguishes from a true clear)."""
    clear_range(MagicMock(), "SPREAD1", "A1:B2")
    assert stub_sheets_for_clear.spreadsheets().values().clear.called


def test_clear_range_passes_spreadsheetId_and_range(stub_sheets_for_clear):
    clear_range(MagicMock(), "SPREAD-XYZ", "Sheet2!B2:D10")
    kw = _last_clear_kwargs(stub_sheets_for_clear)
    assert kw["spreadsheetId"] == "SPREAD-XYZ"
    assert kw["range"] == "Sheet2!B2:D10"
    # values.clear takes an empty body (the range carries the target).
    assert kw["body"] == {}


def test_clear_range_returns_flat_envelope(stub_sheets_for_clear):
    """Maps Sheets' ``clearedRange`` → ``cleared_range`` (snake_case)."""
    result = clear_range(MagicMock(), "SPREAD1", "A1:B2")
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "cleared_range": "Sheet1!A1:B2",
    }


def test_clear_range_range_fallback_when_sheets_omits_it(stub_sheets_for_clear):
    """Defensive: if Sheets omits ``clearedRange``, the envelope falls back
    to the requested range rather than KeyError."""
    stub_sheets_for_clear.spreadsheets().values().clear().execute.return_value = {
        "spreadsheetId": "SPREAD1",
    }
    result = clear_range(MagicMock(), "SPREAD1", "A1:A1")
    assert result["cleared_range"] == "A1:A1"


def test_clear_range_rejects_blank_range(stub_sheets_for_clear):
    """Clearing "nothing" is a caller bug — reject blank range client-side."""
    with pytest.raises(ValueError, match="range_str cannot be empty"):
        clear_range(MagicMock(), "SPREAD1", "   ")


# ---------------------------------------------------------------------
# duplicate_sheet / freeze / protect_range — batchUpdate api wrappers
# ---------------------------------------------------------------------
# (The pure builders are unit-tested in test_batch.py; here we cover the
# api wrappers: request reaching batchUpdate + the envelope.)


@pytest.fixture
def stub_sheets_for_batch_api():
    sheets = MagicMock(name="sheets-v4-stub-batch-api")
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_batch_api_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().batchUpdate.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate() call captured spreadsheetId")


def test_duplicate_sheet_dispatches_duplicate_request(stub_sheets_for_batch_api):
    duplicate_sheet(
        MagicMock(), "SPREAD-ABC", 0,
        new_sheet_name="Copy", insert_index=2,
    )
    kw = _last_batch_api_kwargs(stub_sheets_for_batch_api)
    assert kw["spreadsheetId"] == "SPREAD-ABC"
    assert kw["body"]["requests"] == [{
        "duplicateSheet": {
            "sheetId": 0,
            "insertSheetIndex": 2,
            "newSheetName": "Copy",
        }
    }]


def test_duplicate_sheet_returns_assigned_gid_from_reply(stub_sheets_for_batch_api):
    """Sheets assigns the copy a new gid server-side; duplicate_sheet
    surfaces it (parsed from replies[0].duplicateSheet.properties)."""
    stub_sheets_for_batch_api.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [
            {"duplicateSheet": {"properties": {
                "sheetId": 424242, "title": "Copy of Sheet1", "index": 1,
            }}}
        ],
    }
    result = duplicate_sheet(MagicMock(), "SPREAD1", 0)
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "sheet_id": 424242,
        "title": "Copy of Sheet1",
        "index": 1,
    }


def test_duplicate_sheet_rejects_negative_source(stub_sheets_for_batch_api):
    with pytest.raises(ValueError, match="source_sheet_id must be >= 0"):
        duplicate_sheet(MagicMock(), "SPREAD1", -1)


def test_freeze_dispatches_scoped_update(stub_sheets_for_batch_api):
    """freeze composes updateSheetProperties masked to exactly the frozen
    counts supplied."""
    freeze(MagicMock(), "SPREAD-ABC", 0, frozen_row_count=1)
    kw = _last_batch_api_kwargs(stub_sheets_for_batch_api)
    assert kw["body"]["requests"] == [{
        "updateSheetProperties": {
            "properties": {
                "sheetId": 0,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }]


def test_freeze_returns_batch_envelope(stub_sheets_for_batch_api):
    result = freeze(MagicMock(), "SPREAD1", 0, frozen_column_count=2)
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "total_requests": 1,
        "replies": [{}],
    }


def test_freeze_rejects_no_counts(stub_sheets_for_batch_api):
    with pytest.raises(ValueError, match="at least one of frozen_row_count"):
        freeze(MagicMock(), "SPREAD1", 0)


def test_protect_range_builds_protected_range_request(stub_sheets_for_batch_api):
    """protect_range composes one addProtectedRange with the grid + the
    editor allow-list."""
    protect_range(
        MagicMock(), "SPREAD-ABC", sheet_id=0,
        start_row=0, end_row=10, start_col=0, end_col=1,
        description="Locked", editor_emails=["a@x.com"],
    )
    kw = _last_batch_api_kwargs(stub_sheets_for_batch_api)
    pr = kw["body"]["requests"][0]["addProtectedRange"]["protectedRange"]
    assert pr["range"] == {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 10,
        "startColumnIndex": 0,
        "endColumnIndex": 1,
    }
    assert pr["description"] == "Locked"
    assert pr["editors"] == {"users": ["a@x.com"]}


def test_protect_range_warning_only(stub_sheets_for_batch_api):
    protect_range(
        MagicMock(), "SPREAD1", sheet_id=0, warning_only=True,
    )
    kw = _last_batch_api_kwargs(stub_sheets_for_batch_api)
    pr = kw["body"]["requests"][0]["addProtectedRange"]["protectedRange"]
    assert pr["warningOnly"] is True


def test_protect_range_returns_batch_envelope(stub_sheets_for_batch_api):
    result = protect_range(MagicMock(), "SPREAD1", sheet_id=0)
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "total_requests": 1,
        "replies": [{}],
    }


def test_protect_range_rejects_warning_with_editors(stub_sheets_for_batch_api):
    with pytest.raises(ValueError, match="incompatible with warning_only"):
        protect_range(
            MagicMock(), "SPREAD1", sheet_id=0,
            warning_only=True, editor_emails=["a@x.com"],
        )


def test_protect_range_propagates_grid_validation(stub_sheets_for_batch_api):
    """An inverted GridRange is caught by the grid_range builder."""
    with pytest.raises(ValueError, match="end_row.*must be >"):
        protect_range(
            MagicMock(), "SPREAD1", sheet_id=0,
            start_row=5, end_row=2,
        )


# ---------------------------------------------------------------------
# insert_dimension / delete_dimension / merge_cells /
# set_data_validation / add_chart — batchUpdate api wrappers
#
# (Builder shapes are unit-tested in test_batch.py; these cover the api
# wrappers: the request reaching batchUpdate + the envelope.)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_dim():
    sheets = MagicMock(name="sheets-v4-stub-dim")
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_dim_batch_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().batchUpdate.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate() call captured spreadsheetId")


def test_insert_dimension_dispatches_insert_dimension_request(stub_sheets_for_dim):
    insert_dimension(
        MagicMock(), "SPREAD-ABC", 0,
        dimension="ROWS", start_index=2, end_index=5,
    )
    kw = _last_dim_batch_kwargs(stub_sheets_for_dim)
    assert kw["spreadsheetId"] == "SPREAD-ABC"
    assert kw["body"]["requests"] == [
        {"insertDimension": {
            "range": {
                "sheetId": 0, "dimension": "ROWS",
                "startIndex": 2, "endIndex": 5,
            },
            "inheritFromBefore": False,
        }}
    ]


def test_insert_dimension_returns_flat_envelope(stub_sheets_for_dim):
    result = insert_dimension(
        MagicMock(), "SPREAD1", 0,
        dimension="COLUMNS", start_index=0, end_index=1,
    )
    assert result["spreadsheet_id"] == "SPREAD1"
    assert result["total_requests"] == 1
    assert result["replies"] == [{}]


def test_delete_dimension_dispatches_delete_dimension_request(stub_sheets_for_dim):
    delete_dimension(
        MagicMock(), "SPREAD-ABC", 7,
        dimension="COLUMNS", start_index=0, end_index=2,
    )
    kw = _last_dim_batch_kwargs(stub_sheets_for_dim)
    assert kw["body"]["requests"] == [
        {"deleteDimension": {"range": {
            "sheetId": 7, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": 2,
        }}}
    ]


def test_delete_dimension_rejects_bad_dimension(stub_sheets_for_dim):
    with pytest.raises(ValueError, match="dimension must be one of"):
        delete_dimension(
            MagicMock(), "SPREAD1", 0,
            dimension="X", start_index=0, end_index=1,
        )


def test_merge_cells_dispatches_merge_cells_request(stub_sheets_for_dim):
    merge_cells(
        MagicMock(), "SPREAD-ABC", 0,
        start_row=0, end_row=1, start_col=0, end_col=3,
    )
    kw = _last_dim_batch_kwargs(stub_sheets_for_dim)
    assert kw["body"]["requests"] == [
        {"mergeCells": {
            "range": {
                "sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 3,
            },
            "mergeType": "MERGE_ALL",
        }}
    ]


def test_merge_cells_custom_merge_type_propagates(stub_sheets_for_dim):
    merge_cells(
        MagicMock(), "SPREAD1", 0,
        start_row=0, end_row=3, start_col=0, end_col=1,
        merge_type="MERGE_COLUMNS",
    )
    kw = _last_dim_batch_kwargs(stub_sheets_for_dim)
    assert kw["body"]["requests"][0]["mergeCells"]["mergeType"] == "MERGE_COLUMNS"


def test_set_data_validation_dispatches_request(stub_sheets_for_dim):
    set_data_validation(
        MagicMock(), "SPREAD-ABC", 0,
        condition_type="ONE_OF_LIST",
        start_row=1, end_row=10, start_col=0, end_col=1,
        values=["A", "B"],
    )
    kw = _last_dim_batch_kwargs(stub_sheets_for_dim)
    req = kw["body"]["requests"][0]["setDataValidation"]
    assert req["range"]["sheetId"] == 0
    assert req["rule"]["condition"]["type"] == "ONE_OF_LIST"
    assert req["rule"]["condition"]["values"] == [
        {"userEnteredValue": "A"}, {"userEnteredValue": "B"},
    ]


def test_set_data_validation_rejects_blank_condition(stub_sheets_for_dim):
    with pytest.raises(ValueError, match="condition_type cannot be empty"):
        set_data_validation(
            MagicMock(), "SPREAD1", 0, condition_type="",
        )


def test_add_chart_dispatches_add_chart_request(stub_sheets_for_dim):
    add_chart(
        MagicMock(), "SPREAD-ABC",
        chart_type="COLUMN",
        domain_sheet_id=0,
        domain_start_row=0, domain_end_row=5,
        domain_start_col=0, domain_end_col=1,
        series_ranges=[{
            "sheet_id": 0, "start_row": 0, "end_row": 5,
            "start_col": 1, "end_col": 2,
        }],
        anchor_sheet_id=0, anchor_row=1, anchor_col=4,
        title="Sales",
    )
    kw = _last_dim_batch_kwargs(stub_sheets_for_dim)
    spec = kw["body"]["requests"][0]["addChart"]["chart"]["spec"]
    assert spec["basicChart"]["chartType"] == "COLUMN"
    assert spec["title"] == "Sales"
    assert len(spec["basicChart"]["series"]) == 1


def test_add_chart_surfaces_assigned_chart_id_from_reply(stub_sheets_for_dim):
    stub_sheets_for_dim.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{"addChart": {"chart": {"chartId": 555}}}],
    }
    result = add_chart(
        MagicMock(), "SPREAD1",
        chart_type="BAR",
        domain_sheet_id=0,
        domain_start_row=0, domain_end_row=3,
        domain_start_col=0, domain_end_col=1,
        series_ranges=[{
            "sheet_id": 0, "start_row": 0, "end_row": 3,
            "start_col": 1, "end_col": 2,
        }],
        anchor_sheet_id=0, anchor_row=0, anchor_col=0,
    )
    assert result["spreadsheet_id"] == "SPREAD1"
    assert result["chart_id"] == 555
    assert result["total_requests"] == 1


def test_add_chart_rejects_empty_series(stub_sheets_for_dim):
    # The api passes an empty series_ranges straight to the builder, which
    # raises with its own param name (series_grids).
    with pytest.raises(ValueError, match="series_grids cannot be empty"):
        add_chart(
            MagicMock(), "SPREAD1",
            chart_type="BAR",
            domain_sheet_id=0,
            domain_start_row=0, domain_end_row=3,
            domain_start_col=0, domain_end_col=1,
            series_ranges=[],
            anchor_sheet_id=0, anchor_row=0, anchor_col=0,
        )
