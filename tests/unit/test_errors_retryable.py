"""Next-wave polish (2026-07-10): the error envelope's Retryable line.

``friendly_http_error_message`` now tells the caller whether repeating
the call can plausibly succeed (``Retryable: true`` only for Google's
documented-transient 429/5xx statuses) and keeps passing Google's own
reason/details text through verbatim.

Also pins the parity contract between ``errors.RETRYABLE_HTTP_STATUS``
and the retry layer's ``google_api_client._RETRYABLE_STATUS`` — the two
sets are deliberately duplicated (errors.py stays a leaf module) and
this test is the guard that keeps them identical.
"""
from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from appscriptly import errors as errors_mod
from appscriptly.errors import RETRYABLE_HTTP_STATUS, friendly_http_error_message
from appscriptly.google_api_client import _RETRYABLE_STATUS


class _Resp(dict):
    def __init__(self, status: int, reason: str = "Synthetic") -> None:
        super().__init__()
        self.status = status
        self.reason = reason


def _http_error(status: int, content: bytes = b"") -> HttpError:
    return HttpError(
        resp=_Resp(status),
        content=content,
        uri="https://www.googleapis.com/docs/v1/documents/D1",
    )


def test_retryable_sets_match_retry_layer():
    """One source of truth, two homes: the envelope's set must equal the
    retry layer's. If you change one, change both (and this test)."""
    assert RETRYABLE_HTTP_STATUS == _RETRYABLE_STATUS


@pytest.mark.parametrize("status", sorted(RETRYABLE_HTTP_STATUS))
def test_transient_statuses_report_retryable_true(status):
    msg = friendly_http_error_message(_http_error(status))
    assert "Retryable: true" in msg


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_caller_bug_statuses_report_retryable_false(status):
    msg = friendly_http_error_message(_http_error(status))
    assert "Retryable: false" in msg


def test_google_reason_and_details_pass_through_verbatim():
    """The envelope must carry what Google actually said."""
    content = (
        b'{"error": {"code": 500, "message": "Internal error encountered.",'
        b' "status": "INTERNAL"}}'
    )
    msg = friendly_http_error_message(_http_error(500, content))
    assert "Internal error encountered." in msg
    assert msg.startswith("Google API error: 500")


def test_guidance_still_appended_after_retryable_line():
    """The known-fragment guidance layer survives the envelope change."""
    content = (
        b'{"error": {"code": 500, "message": "Internal error encountered."}}'
    )
    msg = friendly_http_error_message(_http_error(500, content))
    assert "Guidance:" in msg


def test_status_falls_back_to_resp_status_when_status_code_missing():
    """Some SDK paths only populate resp.status; the envelope must not
    report 'None' retryability for those."""

    class _BareError:
        resp = _Resp(503)
        reason = "Service Unavailable"
        error_details = ""

    msg = friendly_http_error_message(_BareError())
    assert "Retryable: true" in msg


def test_module_documented_test_path_matches_this_file():
    """errors.py's comment points here; keep the pointer honest."""
    assert "test_errors_retryable" in __name__
    assert hasattr(errors_mod, "RETRYABLE_HTTP_STATUS")
