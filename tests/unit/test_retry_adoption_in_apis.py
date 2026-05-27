"""End-to-end coverage of PR-Δ3.5's retry adoption in api.py modules.

PR-Δ3 wired ``RetryingGoogleApiClientAdapter`` as the production
default; PR-Δ3.5 adopts ``execute_with_retry`` at the api.py call
sites for readonly + idempotent tools. The unit tests in
``test_retrying_google_api_client.py`` cover the adapter itself in
isolation. This file pins the adoption contract:

- A WRAPPED api function (one whose tool is annotated
  readonly=True or idempotent=True) retries on a transient 503 and
  returns the success response on the next attempt.
- An UNWRAPPED api function (whose tool is annotated
  idempotent=False) does NOT retry — first transient surfaces.

We use the standard ``with_google_api_client`` injection pattern to
swap a controllable stub Resource into the active client. The
production default (`RetryingGoogleApiClientAdapter` composing
`GoogleApiClientAdapter`) is RESTORED after the with-block, so
later tests in the suite see normal production wiring.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from google_docs_mcp.google_api_client import (
    GoogleApiClientAdapter,
    InMemoryGoogleAPIClient,
    RetryingGoogleApiClientAdapter,
    with_google_api_client,
)


# ---------------------------------------------------------------------
# HttpError helper (mirrors test_retrying_google_api_client.py shape)
# ---------------------------------------------------------------------


class _FakeResp(dict):
    """Mimics googleapiclient.http.HttpRequest.Response (dict + .status)."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.reason = "Synthetic"


def _http_error(status: int) -> HttpError:
    return HttpError(resp=_FakeResp(status), content=b"")


# ---------------------------------------------------------------------
# WRAPPED: sheets.read_range — readonly=True, idempotent=True
# ---------------------------------------------------------------------


def test_sheets_read_range_retries_on_transient_5xx():
    """``gsheets_read_range`` is readonly=True; the api.py call site
    wraps the ``.execute()`` in ``execute_with_retry(..., idempotent=True)``.
    A transient 503 on the first attempt must NOT bubble — the next
    attempt's success value flows back."""
    # Build a stub Resource whose chained .spreadsheets().values().get().execute()
    # raises 503 once, then returns a real-looking response.
    final_response = {"range": "A1:Z1000", "values": [["a", "b"]]}
    sequence: list[Any] = [_http_error(503), final_response]

    stub_get = MagicMock()

    def fake_execute():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    stub_get.execute.side_effect = fake_execute
    stub_sheets = MagicMock()
    stub_sheets.spreadsheets().values().get.return_value = stub_get

    # Wrap an InMemory stub in the retry adapter so the facade's
    # execute_with_retry actually triggers retry (the production
    # default does this too, but tests usually want fast jittered
    # waits).
    inner = InMemoryGoogleAPIClient({("sheets", "v4"): stub_sheets})
    retrying = RetryingGoogleApiClientAdapter(
        inner, max_attempts=3, base_wait_seconds=0.0001, max_wait_seconds=0.001,
    )

    from google_docs_mcp.services.sheets.api import read_range

    with with_google_api_client(retrying):
        result = read_range(
            creds=MagicMock(),
            spreadsheet_id="sheet-id",
            range_str="A1:Z1000",
        )

    assert result == {"range": "A1:Z1000", "values": [["a", "b"]]}
    assert sequence == [], "retry sequence not fully consumed"


# ---------------------------------------------------------------------
# WRAPPED: drive find_doc_by_title — readonly=True, idempotent=True
# ---------------------------------------------------------------------


def test_drive_find_doc_by_title_retries_on_transient_5xx():
    """``gdocs_find_doc_by_title`` is readonly=True. The api.py call
    site wraps ``drive.files().list().execute()``."""
    final_response = {
        "files": [
            {
                "id": "doc-1",
                "name": "Hello",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-05-27T00:00:00Z",
                "trashed": False,
            }
        ]
    }
    sequence: list[Any] = [_http_error(503), final_response]

    stub_list = MagicMock()

    def fake_execute():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    stub_list.execute.side_effect = fake_execute
    stub_drive = MagicMock()
    stub_drive.files().list.return_value = stub_list

    inner = InMemoryGoogleAPIClient({("drive", "v3"): stub_drive})
    retrying = RetryingGoogleApiClientAdapter(
        inner, max_attempts=3, base_wait_seconds=0.0001, max_wait_seconds=0.001,
    )

    from google_docs_mcp.services.drive.api import find_doc_by_title

    with with_google_api_client(retrying):
        result = find_doc_by_title(
            creds=MagicMock(),
            query="Hello",
        )

    assert result["count"] == 1
    assert result["matches"][0]["file_id"] == "doc-1"
    assert sequence == []


# ---------------------------------------------------------------------
# UNWRAPPED: gas_deploy install flow — non-idempotent (creates new project)
# ---------------------------------------------------------------------


def test_unwrapped_api_function_does_not_retry_on_transient():
    """``upload_and_convert_docx`` is called from non-idempotent paths
    (``gdocs_tab_existing_doc``, ``/api/convert``). PR-Δ3.5 deliberately
    leaves its ``.execute()`` UNWRAPPED — a partial conversion plus a
    retry would risk a duplicate Google Doc.

    Verify: a 503 on the first attempt propagates immediately; no
    retry is attempted.
    """
    err = _http_error(503)
    calls = 0

    stub_create = MagicMock()

    def fake_execute():
        nonlocal calls
        calls += 1
        raise err

    stub_create.execute.side_effect = fake_execute
    stub_drive = MagicMock()
    stub_drive.files().create.return_value = stub_create

    inner = InMemoryGoogleAPIClient({("drive", "v3"): stub_drive})
    retrying = RetryingGoogleApiClientAdapter(
        inner, max_attempts=3, base_wait_seconds=0.0001, max_wait_seconds=0.001,
    )

    from google_docs_mcp.services.drive.api import upload_and_convert_docx

    # Use a real tiny docx-shaped tempfile so the pre-flight validation
    # passes (the file existence check fires before any .execute()).
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(b"PK\x03\x04")  # ZIP magic — enough to pass suffix + size check
        tmp_path = Path(tmp.name)

    try:
        with with_google_api_client(retrying):
            with pytest.raises(HttpError):
                upload_and_convert_docx(creds=MagicMock(), docx_path=tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    assert calls == 1, (
        "upload_and_convert_docx is non-idempotent; .execute() must be "
        "called exactly once (no retry), but got " + str(calls)
    )


# ---------------------------------------------------------------------
# Production wiring sanity: bare GoogleApiClientAdapter does NOT
# accidentally cause retry to happen at the api.py layer.
# ---------------------------------------------------------------------


def test_facade_falls_back_to_single_call_when_active_client_lacks_retry():
    """The api.py call sites use the facade-level ``execute_with_retry``,
    which gracefully degrades to a single invocation when the active
    client lacks ``execute_with_retry`` (e.g. a bare InMemory adapter
    in a test that opted out).

    This guards against the regression where a wrapped api function
    silently turns into a no-op (returning None) when the active
    client doesn't have the retry method.
    """
    final_response = {"range": "A1:Z10", "values": [["x"]]}

    stub_get = MagicMock()
    stub_get.execute.return_value = final_response
    stub_sheets = MagicMock()
    stub_sheets.spreadsheets().values().get.return_value = stub_get

    # Bare InMemory — no retry layer.
    bare = InMemoryGoogleAPIClient({("sheets", "v4"): stub_sheets})

    from google_docs_mcp.services.sheets.api import read_range

    with with_google_api_client(bare):
        result = read_range(
            creds=MagicMock(),
            spreadsheet_id="sheet-id",
            range_str="A1:Z10",
        )

    assert result == {"range": "A1:Z10", "values": [["x"]]}
    # Suppress unused-import warning from the production adapter import
    # (kept around for documentation of the wired type).
    _ = GoogleApiClientAdapter
