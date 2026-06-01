"""Per-tool behavior tests for services/sheets/tools.py (v2.3.1).

Mirrors ``tests/unit/services/drive/test_tools.py`` exactly — the
canonical per-tool happy-path coverage at the decorator-envelope
boundary, using the same ``InMemoryGoogleAPIClient`` + monkeypatched
``_get_credentials_fn`` fixture pattern.

The 3 sheets tools (v2.3.1 minimal start):

  1. gsheets_read_range         — values.get
  2. gsheets_write_range        — values.update
  3. gsheets_create_spreadsheet — spreadsheets.create

Per-tool API-shape coverage (``valueInputOption``, body shape, fields
mask, response envelope) lives in ``test_api.py``; this file covers
the tool-layer envelope: decorator's ``_get_credentials_fn``
injection, ``@workspace_tool(creds=True)`` wrapping, parameter
forwarding from the decorated function into the api module.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.sheets import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True) envelope doesn't try real OAuth.
    Sister to the same fixture in tests/unit/services/drive/test_tools.py."""
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    # Also swap the module-level binding (parity with drive tools' batch
    # path, even though sheets tools currently have no _run_batch).
    monkeypatch.setattr(tools, "_get_credentials", lambda: stub_creds)


@pytest.fixture
def sheets_stub():
    """A Sheets v4 Resource stub with all three method chains pre-wired
    to return plausible default responses. Individual tests override
    per-call as needed."""
    sheets = MagicMock(name="sheets-v4-stub")
    sheets.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:Z1000",
        "values": [],
    }
    sheets.spreadsheets().values().update().execute.return_value = {
        "spreadsheetId": "S1",
        "updatedRange": "Sheet1!A1",
        "updatedCells": 1,
    }
    sheets.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "NEW-1",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/NEW-1/edit",
        "properties": {"title": "T"},
    }
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "S1",
        "replies": [{}],
    }
    return sheets


@pytest.fixture
def with_sheets_stub(sheets_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("sheets", "v4"): sheets_stub,
    })):
        yield sheets_stub


# ---------------------------------------------------------------------
# 1. gsheets_read_range — happy path through the decorator envelope
# ---------------------------------------------------------------------


def test_gsheets_read_range_returns_envelope_for_blank_sheet(with_sheets_stub):
    """Default Sheets stub returns empty values; tool surfaces
    ``{range, values: []}`` through the standard envelope."""
    result = tools.gsheets_read_range(spreadsheet_id="SPREAD1")
    assert result == {"range": "Sheet1!A1:Z1000", "values": []}


def test_gsheets_read_range_passes_explicit_range_through(with_sheets_stub):
    """An explicit ``range`` argument reaches the Sheets API verbatim
    — exercised via inspecting the call args on the stub."""
    tools.gsheets_read_range(
        spreadsheet_id="SPREAD1", range="Sheet2!B2:D10",
    )
    last_call = with_sheets_stub.spreadsheets().values().get.call_args_list[-1]
    assert last_call.kwargs["range"] == "Sheet2!B2:D10"


def test_gsheets_read_range_surfaces_values_when_sheets_returns_them(
    with_sheets_stub,
):
    """Sheets returns rows of cells; the tool passes them through
    unchanged in ``values``."""
    with_sheets_stub.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:B2",
        "values": [["a", "b"], ["c", "d"]],
    }
    result = tools.gsheets_read_range(
        spreadsheet_id="SPREAD1", range="A1:B2",
    )
    assert result["values"] == [["a", "b"], ["c", "d"]]


# ---------------------------------------------------------------------
# 2. gsheets_write_range — happy path + validation
# ---------------------------------------------------------------------


def test_gsheets_write_range_happy_path(with_sheets_stub):
    """Standard write returns the ``{updated_range, updated_cells}``
    envelope. Tool layer pass-through of api function."""
    with_sheets_stub.spreadsheets().values().update().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "updatedRange": "Sheet1!A1:B2",
        "updatedCells": 4,
    }
    result = tools.gsheets_write_range(
        spreadsheet_id="SPREAD1",
        range="A1:B2",
        values=[["a", "b"], ["c", "d"]],
    )
    assert result == {"updated_range": "Sheet1!A1:B2", "updated_cells": 4}


def test_gsheets_write_range_validation_propagates_through_tool(
    with_sheets_stub,
):
    """Pre-API validation (empty values, non-list-of-lists) bubbles
    from the api module through the decorator envelope as ValueError.
    The decorator wraps it into a structured response for cloud-mode
    callers, but raises the bare ValueError in test contexts."""
    with pytest.raises(ValueError, match="values cannot be empty"):
        tools.gsheets_write_range(
            spreadsheet_id="SPREAD1",
            range="A1",
            values=[],
        )


# ---------------------------------------------------------------------
# 3. gsheets_create_spreadsheet — happy path + validation
# ---------------------------------------------------------------------


def test_gsheets_create_spreadsheet_happy_path(with_sheets_stub):
    """Create returns the flat ``{spreadsheet_id, url, title}``
    envelope ready for piping into read_range / write_range."""
    with_sheets_stub.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "SPREAD-NEW",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/SPREAD-NEW/edit",
        "properties": {"title": "Forecast 2026"},
    }
    result = tools.gsheets_create_spreadsheet(title="Forecast 2026")
    assert result == {
        "spreadsheet_id": "SPREAD-NEW",
        "url": "https://docs.google.com/spreadsheets/d/SPREAD-NEW/edit",
        "title": "Forecast 2026",
    }


def test_gsheets_create_spreadsheet_rejects_blank_title(with_sheets_stub):
    """Blank-title rejection from the api module bubbles up cleanly."""
    with pytest.raises(ValueError, match="title cannot be empty"):
        tools.gsheets_create_spreadsheet(title="   ")


# ---------------------------------------------------------------------
# 4. gsheets_format_range — happy path + validation (batchUpdate seam)
# ---------------------------------------------------------------------


def test_gsheets_format_range_happy_path(with_sheets_stub):
    """A format call dispatches a repeatCell batchUpdate and returns the
    flat ``{spreadsheet_id, total_requests, replies}`` envelope."""
    result = tools.gsheets_format_range(
        spreadsheet_id="SPREAD1",
        sheet_id=0,
        start_row=0,
        end_row=1,
        bold=True,
    )
    # ``batch_update`` echoes the INPUT spreadsheet_id arg (not the
    # ``spreadsheetId`` field from the API response), so the envelope
    # carries "SPREAD1" regardless of what the stub response says.
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "total_requests": 1,
        "replies": [{}],
    }


def test_gsheets_format_range_forwards_options_to_batchUpdate(with_sheets_stub):
    """Tool-layer pass-through: the flat kwargs reach the Sheets
    batchUpdate as a single repeatCell with the expected field mask."""
    tools.gsheets_format_range(
        spreadsheet_id="SPREAD1",
        sheet_id=2,
        start_row=0,
        end_row=1,
        start_col=0,
        end_col=2,
        bold=True,
        horizontal_alignment="CENTER",
    )
    # The shared fixture pre-calls ``batchUpdate()`` (no args) during
    # setup, which records a kwarg-less entry in call_args_list; pick
    # the most recent REAL call (the one carrying spreadsheetId).
    real_calls = [
        c
        for c in with_sheets_stub.spreadsheets().batchUpdate.call_args_list
        if "spreadsheetId" in c.kwargs
    ]
    assert real_calls, "no batchUpdate() call captured spreadsheetId"
    last_call = real_calls[-1]
    requests = last_call.kwargs["body"]["requests"]
    assert len(requests) == 1
    rc = requests[0]["repeatCell"]
    assert rc["range"]["sheetId"] == 2
    assert set(rc["fields"].split(",")) == {
        "userEnteredFormat.textFormat.bold",
        "userEnteredFormat.horizontalAlignment",
    }


def test_gsheets_format_range_rejects_empty_format(with_sheets_stub):
    """No format options -> ValueError bubbles through the decorator
    envelope (the builder rejects an empty repeatCell before the call)."""
    with pytest.raises(ValueError, match="fmt is empty"):
        tools.gsheets_format_range(spreadsheet_id="SPREAD1", sheet_id=0)


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: _get_credentials_fn is invoked
# ---------------------------------------------------------------------


def test_gsheets_read_range_invokes_get_credentials_fn(
    with_sheets_stub, monkeypatch,
):
    """Canary identical to the drive test_tools.py pattern: the
    @workspace_tool(creds=True) decorator MUST call
    _get_credentials_fn before delegating to the body. If a refactor
    ever renames it, this fires."""
    call_count = {"n": 0}

    def counting_creds_fn():
        call_count["n"] += 1
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(
        decorators, "_get_credentials_fn", counting_creds_fn
    )
    tools.gsheets_read_range(spreadsheet_id="SPREAD1")
    assert call_count["n"] == 1, (
        "_get_credentials_fn was not called exactly once — the "
        "decorator envelope may have changed or the fixture missed."
    )


# ---------------------------------------------------------------------
# 5. gsheets_append_rows — happy path + validation (values.append)
# ---------------------------------------------------------------------


def test_gsheets_append_rows_happy_path(with_sheets_stub):
    """Append goes through values.append and returns the flat
    ``{updated_range, updated_cells, updated_rows}`` envelope."""
    with_sheets_stub.spreadsheets().values().append().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "updates": {
            "updatedRange": "Sheet1!A5:B5",
            "updatedCells": 2,
            "updatedRows": 1,
        },
    }
    result = tools.gsheets_append_rows(
        spreadsheet_id="SPREAD1",
        values=[["a", "b"]],
    )
    assert result == {
        "updated_range": "Sheet1!A5:B5",
        "updated_cells": 2,
        "updated_rows": 1,
    }


def test_gsheets_append_rows_forwards_range_and_append_options(with_sheets_stub):
    """Tool-layer pass-through: the ``range`` arg + the pinned append
    options reach values.append verbatim."""
    with_sheets_stub.spreadsheets().values().append().execute.return_value = {
        "updates": {"updatedRange": "S!A1", "updatedCells": 1, "updatedRows": 1},
    }
    tools.gsheets_append_rows(
        spreadsheet_id="SPREAD1",
        values=[["x"]],
        range="Sheet2!A:Z",
    )
    real_calls = [
        c
        for c in with_sheets_stub.spreadsheets().values().append.call_args_list
        if "spreadsheetId" in c.kwargs
    ]
    assert real_calls, "no values().append() call captured spreadsheetId"
    kw = real_calls[-1].kwargs
    assert kw["range"] == "Sheet2!A:Z"
    assert kw["valueInputOption"] == "USER_ENTERED"
    assert kw["insertDataOption"] == "INSERT_ROWS"


def test_gsheets_append_rows_validation_propagates(with_sheets_stub):
    """Empty-values rejection bubbles from the api module through the
    decorator envelope as ValueError."""
    with pytest.raises(ValueError, match="values cannot be empty"):
        tools.gsheets_append_rows(spreadsheet_id="SPREAD1", values=[])


# ---------------------------------------------------------------------
# 6-8. gsheets_add_sheet / delete_sheet / rename_sheet — tab lifecycle
# ---------------------------------------------------------------------


def test_gsheets_add_sheet_happy_path(with_sheets_stub):
    """add_sheet surfaces the gid Sheets assigned the new tab."""
    with_sheets_stub.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [
            {"addSheet": {"properties": {
                "sheetId": 555, "title": "Summary", "index": 1,
            }}}
        ],
    }
    result = tools.gsheets_add_sheet(
        spreadsheet_id="SPREAD1", title="Summary", index=1,
    )
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "sheet_id": 555,
        "title": "Summary",
        "index": 1,
    }


def test_gsheets_add_sheet_validation_propagates(with_sheets_stub):
    with pytest.raises(ValueError, match="title cannot be empty"):
        tools.gsheets_add_sheet(spreadsheet_id="SPREAD1", title="  ")


def test_gsheets_delete_sheet_happy_path(with_sheets_stub):
    """delete_sheet dispatches a deleteSheet batchUpdate and echoes the
    removed gid."""
    result = tools.gsheets_delete_sheet(spreadsheet_id="SPREAD1", sheet_id=42)
    assert result == {"spreadsheet_id": "SPREAD1", "deleted_sheet_id": 42}
    real_calls = [
        c
        for c in with_sheets_stub.spreadsheets().batchUpdate.call_args_list
        if "spreadsheetId" in c.kwargs
    ]
    assert real_calls[-1].kwargs["body"]["requests"] == [
        {"deleteSheet": {"sheetId": 42}}
    ]


def test_gsheets_delete_sheet_validation_propagates(with_sheets_stub):
    with pytest.raises(ValueError, match="sheet_id must be >= 0"):
        tools.gsheets_delete_sheet(spreadsheet_id="SPREAD1", sheet_id=-1)


def test_gsheets_rename_sheet_happy_path(with_sheets_stub):
    """rename_sheet dispatches a title-scoped updateSheetProperties and
    echoes the new name."""
    result = tools.gsheets_rename_sheet(
        spreadsheet_id="SPREAD1", sheet_id=0, title="Renamed",
    )
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "sheet_id": 0,
        "title": "Renamed",
    }
    real_calls = [
        c
        for c in with_sheets_stub.spreadsheets().batchUpdate.call_args_list
        if "spreadsheetId" in c.kwargs
    ]
    assert real_calls[-1].kwargs["body"]["requests"] == [{
        "updateSheetProperties": {
            "properties": {"sheetId": 0, "title": "Renamed"},
            "fields": "title",
        }
    }]


def test_gsheets_rename_sheet_validation_propagates(with_sheets_stub):
    with pytest.raises(ValueError, match="title cannot be empty"):
        tools.gsheets_rename_sheet(
            spreadsheet_id="SPREAD1", sheet_id=0, title="",
        )
