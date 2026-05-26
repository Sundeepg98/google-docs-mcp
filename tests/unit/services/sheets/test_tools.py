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

from google_docs_mcp import decorators
from google_docs_mcp.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from google_docs_mcp.services.sheets import tools


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
