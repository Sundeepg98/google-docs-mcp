"""Soft-failure contract tests for trash/untrash/move.

These tools MUST return failures as data (a dict with ``reason`` set)
instead of raising — that's the contract that lets batch cleanup do
skip-and-continue. If a future refactor accidentally re-raises, these
tests catch it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError


def _mock_http_error(status_code: int, reason_code: str = "") -> HttpError:
    """Build a fake HttpError with the structure googleapiclient produces."""
    resp = MagicMock()
    resp.status = status_code
    resp.reason = "Forbidden" if status_code == 403 else "Not Found"
    content = (
        f'{{"error":{{"code":{status_code},"errors":'
        f'[{{"reason":"{reason_code}","message":"mocked"}}]}}}}'
    ).encode("utf-8")
    err = HttpError(resp, content)
    # error_details is what our code checks; populate it directly.
    err.error_details = [{"reason": reason_code, "message": "mocked"}]
    return err


@pytest.fixture
def mock_drive():
    """Yield a fake drive service whose .files() returns a mock chain."""
    with patch("google_docs_mcp.drive_api.build") as build_mock:
        drive = MagicMock()
        build_mock.return_value = drive
        yield drive


# -----------------------------------------------------------------
# trash_drive_file
# -----------------------------------------------------------------


def test_trash_returns_soft_failure_on_404(mock_drive):
    """Bug-class guard: 404 must come back as data, not as a raised exception."""
    from google_docs_mcp.drive_api import trash_drive_file

    mock_drive.files().get().execute.side_effect = _mock_http_error(404)
    result = trash_drive_file(MagicMock(), "BOGUS_ID")

    assert result["trashed"] is False
    assert result["reason"] == "not_found"
    assert "BOGUS_ID" in result["message"]


def test_trash_returns_soft_failure_on_app_not_authorized(mock_drive):
    """0.19.0 regression class: 403 appNotAuthorizedToFile must NOT abort."""
    from google_docs_mcp.drive_api import trash_drive_file

    mock_drive.files().get().execute.return_value = {
        "id": "F", "name": "external.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "trashed": False,
    }
    mock_drive.files().update().execute.side_effect = _mock_http_error(
        403, "appNotAuthorizedToFile"
    )
    result = trash_drive_file(MagicMock(), "F")

    assert result["trashed"] is False
    assert result["reason"] == "app_not_authorized"
    assert result["name"] == "external.docx"


def test_trash_idempotent_on_already_trashed(mock_drive):
    """Re-trashing must succeed and flag was_already_trashed=True."""
    from google_docs_mcp.drive_api import trash_drive_file

    mock_drive.files().get().execute.return_value = {
        "id": "F", "name": "n", "mimeType": "x", "trashed": True,
    }
    mock_drive.files().update().execute.return_value = {
        "id": "F", "name": "n", "mimeType": "x", "trashed": True,
    }
    result = trash_drive_file(MagicMock(), "F")

    assert result["trashed"] is True
    assert result["was_already_trashed"] is True


def test_trash_unknown_error_still_raises(mock_drive):
    """A genuine bug (500, network) MUST surface — not be swallowed silently."""
    from google_docs_mcp.drive_api import trash_drive_file

    mock_drive.files().get().execute.side_effect = _mock_http_error(500)
    with pytest.raises(HttpError):
        trash_drive_file(MagicMock(), "F")


# -----------------------------------------------------------------
# untrash_drive_file (mirror trash contract)
# -----------------------------------------------------------------


def test_untrash_returns_soft_failure_on_404(mock_drive):
    from google_docs_mcp.drive_api import untrash_drive_file

    mock_drive.files().get().execute.side_effect = _mock_http_error(404)
    result = untrash_drive_file(MagicMock(), "BOGUS")
    assert result["trashed"] is False
    assert result["reason"] == "not_found"


def test_untrash_idempotent_on_active_file(mock_drive):
    """Untrashing a non-trashed file: was_already_active=True, no error."""
    from google_docs_mcp.drive_api import untrash_drive_file

    mock_drive.files().get().execute.return_value = {
        "id": "F", "name": "n", "mimeType": "x", "trashed": False,
    }
    mock_drive.files().update().execute.return_value = {
        "id": "F", "name": "n", "mimeType": "x", "trashed": False,
    }
    result = untrash_drive_file(MagicMock(), "F")
    assert result["trashed"] is False
    assert result["was_already_active"] is True


# -----------------------------------------------------------------
# move_to_folder
# -----------------------------------------------------------------


def test_move_returns_soft_failure_on_folder_not_found(mock_drive):
    from google_docs_mcp.drive_api import move_to_folder

    # file exists, folder doesn't
    mock_drive.files().get().execute.side_effect = [
        {"id": "F", "name": "n", "mimeType": "x", "parents": ["root"]},
        _mock_http_error(404),
    ]
    # second call is the folder lookup; need to make side_effect cooperate
    # with the actual call sequence
    call_count = {"n": 0}
    def get_side_effect(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            mock_return = MagicMock()
            mock_return.execute.return_value = {
                "id": "F", "name": "n", "mimeType": "x", "parents": ["root"]
            }
            return mock_return
        else:
            mock_return = MagicMock()
            mock_return.execute.side_effect = _mock_http_error(404)
            return mock_return
    mock_drive.files().get.side_effect = get_side_effect

    result = move_to_folder(MagicMock(), "F", "BOGUS_FOLDER")
    assert result["reason"] == "folder_not_found"
