"""Wave-5 Stream S3 retry-wrap sweep: adoption contract for the three
files the S3 sweep touched (``services/docs/api.py``, ``retrofit.py``,
``docx_import.py``).

``test_retry_adoption_in_apis.py`` pins the PR-Delta3.5 adoption. This
file pins the wave-5 S3 additions with the same discriminating shape:

- One WRAPPED idempotent read PER FILE retries on a transient failure
  (503 HttpError or a transport ``ConnectionError``) and returns the
  next attempt's success value. Revert the wrap and the transient kills
  the op (the ``sequence == []`` assertion goes unmet).
- One LEFT-ALONE mutation (``make_doc_with_tabs`` -> ``documents().create``)
  is NOT wrapped: a transient surfaces on the first call, no retry.

Injection uses the standard ``with_google_api_client`` swap so the
production wiring is restored after each ``with`` block.
"""
from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    RetryingGoogleApiClientAdapter,
    with_google_api_client,
)


# ---------------------------------------------------------------------
# Shared helpers (mirror test_retry_adoption_in_apis.py)
# ---------------------------------------------------------------------


class _FakeResp(dict):
    """Mimics googleapiclient.http.HttpRequest.Response (dict + .status)."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.reason = "Synthetic"


def _http_error(status: int) -> HttpError:
    return HttpError(resp=_FakeResp(status), content=b"")


def _seq_executor(sequence: list[Any]) -> Callable[[], Any]:
    """Return an ``.execute`` side_effect that pops ``sequence`` left to
    right, raising any exception element and returning any value element.
    An empty ``sequence`` after the call proves every queued attempt
    (the transient AND the retry) actually ran."""

    def _execute() -> Any:
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    return _execute


def _fast_retrying(inner: InMemoryGoogleAPIClient) -> RetryingGoogleApiClientAdapter:
    """The production retry adapter with near-zero jittered waits so the
    test doesn't sleep the real 1s/2s/4s backoff."""
    return RetryingGoogleApiClientAdapter(
        inner, max_attempts=3, base_wait_seconds=0.0001, max_wait_seconds=0.001,
    )


# ---------------------------------------------------------------------
# WRAPPED read #1 - services/docs/api.py :: append_to_tab
#   docs.documents().get(...) is now execute_with_retry(idempotent=True)
# ---------------------------------------------------------------------


def test_docs_append_to_tab_read_retries_on_transient_5xx():
    """``append_to_tab`` re-fetches the doc to locate the target tab.
    That GET is wrapped; a transient 503 on the first attempt must not
    bubble - the retry's success value flows on and the append completes."""
    doc = {
        "tabs": [
            {
                "tabProperties": {"tabId": "t1"},
                "documentTab": {"body": {"content": [{"endIndex": 10}]}},
            }
        ]
    }
    sequence: list[Any] = [_http_error(503), doc]

    stub_get = MagicMock()
    stub_get.execute.side_effect = _seq_executor(sequence)
    stub_docs = MagicMock()
    stub_docs.documents().get.return_value = stub_get
    # batchUpdate (the append write) is idempotent=False / single-shot;
    # its auto-mocked .execute() returns a throwaway mock - fine.

    inner = InMemoryGoogleAPIClient({("docs", "v1"): stub_docs})

    from appscriptly.services.docs.api import append_to_tab

    with with_google_api_client(_fast_retrying(inner)):
        result = append_to_tab(
            creds=MagicMock(),
            doc_id="d1",
            tab_id="t1",
            content="hello",
            content_format="text",
        )

    assert result == {"tab_id": "t1", "appended_chars": 5}
    assert sequence == [], "the 503 attempt AND the retry must both have run"


# ---------------------------------------------------------------------
# WRAPPED read #2 - retrofit.py :: _fetch_drive_as_docx_bytes
#   drive.files().get(...) metadata read - transport-error retry path
# ---------------------------------------------------------------------


def test_retrofit_fetch_docx_bytes_read_retries_on_transient_transport():
    """``_fetch_drive_as_docx_bytes`` reads Drive metadata then pulls the
    bytes. The metadata GET is wrapped; a transient ``ConnectionError``
    (the transport class, not an HttpError) must retry and succeed."""
    from appscriptly.services.drive.api import DOCX_MIME

    meta = {"name": "Quarterly.docx", "mimeType": DOCX_MIME}
    docx_bytes = b"PK\x03\x04payload"
    meta_seq: list[Any] = [ConnectionError("connection reset"), meta]

    stub_get = MagicMock()
    stub_get.execute.side_effect = _seq_executor(meta_seq)
    stub_media = MagicMock()
    stub_media.execute.return_value = docx_bytes
    stub_drive = MagicMock()
    stub_drive.files().get.return_value = stub_get
    stub_drive.files().get_media.return_value = stub_media

    inner = InMemoryGoogleAPIClient({("drive", "v3"): stub_drive})

    from appscriptly.retrofit import _fetch_drive_as_docx_bytes

    with with_google_api_client(_fast_retrying(inner)):
        result_bytes, name = _fetch_drive_as_docx_bytes(
            creds=MagicMock(), drive_file_id="f1",
        )

    assert result_bytes == docx_bytes
    assert name == "Quarterly.docx"
    assert meta_seq == [], "the transport-error attempt AND the retry must both have run"


# ---------------------------------------------------------------------
# WRAPPED read #3 - docx_import.py :: _expected_final_title
#   drive.files().get(...) metadata read
# ---------------------------------------------------------------------


def test_docx_import_expected_final_title_read_retries_on_transient_5xx():
    """``_expected_final_title`` reads Drive metadata to predict the
    conversion's final title. That GET is wrapped; a transient 503 must
    retry and yield the real title."""
    meta = {
        "name": "Report",
        "mimeType": "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document",
    }
    sequence: list[Any] = [_http_error(503), meta]

    stub_get = MagicMock()
    stub_get.execute.side_effect = _seq_executor(sequence)
    stub_drive = MagicMock()
    stub_drive.files().get.return_value = stub_get

    inner = InMemoryGoogleAPIClient({("drive", "v3"): stub_drive})

    from appscriptly.docx_import import _expected_final_title

    with with_google_api_client(_fast_retrying(inner)):
        title = _expected_final_title(
            creds=MagicMock(),
            docx_path=None,
            drive_file_id="f1",
            title=None,
        )

    # effective_convert_title(None, source_kind="docx", source_name="Report")
    # -> "Report" (docx branch strips a .docx suffix; there is none here).
    assert title == "Report"
    assert sequence == [], "the 503 attempt AND the retry must both have run"


# ---------------------------------------------------------------------
# LEFT-ALONE mutation - services/docs/api.py :: make_doc_with_tabs
#   documents().create(...) is deliberately UNWRAPPED (single-shot).
# ---------------------------------------------------------------------


def test_docs_make_doc_create_mutation_does_not_retry():
    """``make_doc_with_tabs`` opens with ``documents().create`` - a
    non-idempotent mutation the S3 sweep left UNWRAPPED. Under the same
    retrying adapter, a 503 on ``create`` must surface on the FIRST call
    (no replay - a retried create risks a duplicate Google Doc)."""
    calls = 0

    def _raise_503() -> Any:
        nonlocal calls
        calls += 1
        raise _http_error(503)

    stub_create = MagicMock()
    stub_create.execute.side_effect = _raise_503
    stub_docs = MagicMock()
    stub_docs.documents().create.return_value = stub_create

    inner = InMemoryGoogleAPIClient({("docs", "v1"): stub_docs})

    from appscriptly.services.docs.api import make_doc_with_tabs

    with with_google_api_client(_fast_retrying(inner)):
        with pytest.raises(HttpError):
            make_doc_with_tabs(
                creds=MagicMock(),
                title="T",
                tabs=[{"title": "Tab1", "content": "hi"}],
            )

    assert calls == 1, (
        "documents().create is non-idempotent and must NOT be retried; "
        f"expected exactly 1 call, got {calls}"
    )
