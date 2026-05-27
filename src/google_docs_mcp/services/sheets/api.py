"""Google Sheets REST wrapper (v2.3.1 — minimal start).

Only the range-shaped surface ships in this PR:

  * ``read_range``  — ``spreadsheets.values.get``
  * ``write_range`` — ``spreadsheets.values.update``
  * ``create_spreadsheet`` — ``spreadsheets.create`` (creates an
    empty sheet so the read/write tools have something to target
    in a single-call workflow; pure API call, no schema acrobatics)

The ``batchUpdate`` tagged-union (40+ request types: formatting,
charts, pivots, named ranges, conditional formats, etc.) is
DELIBERATELY DEFERRED to a follow-up PR per the multi-service
feasibility audit ("Sheets — pattern stretch. batchUpdate has no
precedent in the foundation"). The minimal start lets us prove the
foundation extends to a new Google service without needing to first
design the tagged-union abstraction.

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

from google_docs_mcp.google_api_client import execute_with_retry
from google_docs_mcp.google_clients import get_service

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
