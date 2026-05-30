"""Soft-failure contract tests for trash/untrash/move.

These tools MUST return failures as data (a dict with ``reason`` set)
instead of raising — that's the contract that lets batch cleanup do
skip-and-continue. If a future refactor accidentally re-raises, these
tests catch it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

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
    """Yield a fake drive service via the M2 GoogleAPIClient port.

    **v2.1.2 (M2)**: pre-v2.1.2 this fixture used
    ``patch("appscriptly.drive_api.get_service")``, which required
    knowing exactly which module imported ``get_service``. The
    ``with_google_api_client`` + ``InMemoryGoogleAPIClient`` pattern
    (introduced in this PR's M2 port) routes through the same single
    facade that production uses — no import-binding awareness needed.
    """
    from appscriptly.google_api_client import (
        InMemoryGoogleAPIClient,
        with_google_api_client,
    )

    drive = MagicMock()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
    })):
        yield drive


# -----------------------------------------------------------------
# trash_drive_file
# -----------------------------------------------------------------


def test_trash_returns_soft_failure_on_404(mock_drive):
    """Bug-class guard: 404 must come back as data, not as a raised exception."""
    from appscriptly.services.drive.api import trash_drive_file

    mock_drive.files().get().execute.side_effect = _mock_http_error(404)
    result = trash_drive_file(MagicMock(), "BOGUS_ID")

    assert result["trashed"] is False
    assert result["reason"] == "not_found"
    assert "BOGUS_ID" in result["message"]


def test_trash_returns_soft_failure_on_app_not_authorized(mock_drive):
    """0.19.0 regression class: 403 appNotAuthorizedToFile must NOT abort."""
    from appscriptly.services.drive.api import trash_drive_file

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
    from appscriptly.services.drive.api import trash_drive_file

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
    from appscriptly.services.drive.api import trash_drive_file

    mock_drive.files().get().execute.side_effect = _mock_http_error(500)
    with pytest.raises(HttpError):
        trash_drive_file(MagicMock(), "F")


# -----------------------------------------------------------------
# untrash_drive_file (mirror trash contract)
# -----------------------------------------------------------------


def test_untrash_returns_soft_failure_on_404(mock_drive):
    from appscriptly.services.drive.api import untrash_drive_file

    mock_drive.files().get().execute.side_effect = _mock_http_error(404)
    result = untrash_drive_file(MagicMock(), "BOGUS")
    assert result["trashed"] is False
    assert result["reason"] == "not_found"


def test_untrash_idempotent_on_active_file(mock_drive):
    """Untrashing a non-trashed file: was_already_active=True, no error."""
    from appscriptly.services.drive.api import untrash_drive_file

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


def test_owned_by_app_agrees_with_trash_outcome(mock_drive):
    """v0.19.0 regression guard (unit-level). find_doc_by_title's
    ``owned_by_app`` MUST agree with whether trash_drive_file actually
    succeeds on the same file. Pre-v0.19.1 find used
    ``capabilities.canTrash`` (USER-level) but trash actually checks
    APP-level authorization — they disagreed for files the user owned
    but uploaded outside the app's scope.

    Tested by mocking Drive such that the no-op write-probe and the
    real trash update return CONSISTENT results, then asserting the
    two functions agree on every scenario.

    Live equivalent: ``tests/integration/test_consistency_owned_by_app.py``.
    """
    from appscriptly.services.drive.api import find_doc_by_title, trash_drive_file

    file_meta = {
        "id": "F", "name": "test.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "trashed": False, "owners": [], "capabilities": {},
    }

    def setup_drive_state(*, probe_succeeds: bool, trash_succeeds: bool):
        """Configure the Drive mock for a given (probe, trash) outcome pair."""
        mock_drive.files().list().execute.return_value = {"files": [file_meta]}

        # Build a fake batch that records callbacks and replays them on
        # execute() with the configured success/failure.
        batch = MagicMock()
        batch._callbacks = []

        def batch_add(_req, callback=None):
            batch._callbacks.append(callback)

        def batch_execute():
            for cb in batch._callbacks:
                if probe_succeeds:
                    cb("req1", {"id": "F"}, None)
                else:
                    cb("req1", None, _mock_http_error(403, "appNotAuthorizedToFile"))

        batch.add.side_effect = batch_add
        batch.execute.side_effect = batch_execute
        mock_drive.new_batch_http_request.return_value = batch

        # Trash path: get() always succeeds (file exists), update()
        # mirrors the probe outcome.
        mock_drive.files().get().execute.return_value = file_meta
        if trash_succeeds:
            mock_drive.files().update().execute.return_value = {
                **file_meta, "trashed": True,
            }
            mock_drive.files().update().execute.side_effect = None
        else:
            mock_drive.files().update().execute.side_effect = _mock_http_error(
                403, "appNotAuthorizedToFile",
            )

    # ---- Scenario 1: app-owned file. Probe + trash both succeed. ----
    setup_drive_state(probe_succeeds=True, trash_succeeds=True)
    # v2.2.1 (R33 Gap #3): verify_writable default flipped to False so
    # the read-only tool stops silently writing the Drive audit log
    # on every call. This test exercises the probe path on purpose
    # (the whole point is owned_by_app must agree with the live trash
    # outcome), so it opts in explicitly.
    search = find_doc_by_title(
        MagicMock(), "test.docx", exact=True, verify_writable=True,
    )
    owned_by_app_1 = search["matches"][0]["owned_by_app"]
    trash_result_1 = trash_drive_file(MagicMock(), "F")
    trash_succeeded_1 = trash_result_1.get("reason") is None

    assert owned_by_app_1 == trash_succeeded_1, (
        f"app-owned scenario: find said owned_by_app={owned_by_app_1} "
        f"but trash succeeded={trash_succeeded_1} (result={trash_result_1!r}). "
        "Cross-tool consistency BROKEN — the v0.19.0 bug class."
    )
    assert owned_by_app_1 is True

    # ---- Scenario 2: external file. Probe + trash both 403. ----
    setup_drive_state(probe_succeeds=False, trash_succeeds=False)
    # Same v2.2.1 opt-in as Scenario 1 — the cross-tool consistency
    # check needs owned_by_app populated, which only happens with
    # verify_writable=True.
    search = find_doc_by_title(
        MagicMock(), "test.docx", exact=True, verify_writable=True,
    )
    owned_by_app_2 = search["matches"][0]["owned_by_app"]
    trash_result_2 = trash_drive_file(MagicMock(), "F")
    trash_succeeded_2 = trash_result_2.get("reason") is None

    assert owned_by_app_2 == trash_succeeded_2, (
        f"external-file scenario: find said owned_by_app={owned_by_app_2} "
        f"but trash succeeded={trash_succeeded_2} (result={trash_result_2!r}). "
        "Cross-tool consistency BROKEN — the v0.19.0 bug class."
    )
    assert owned_by_app_2 is False


def test_move_returns_soft_failure_on_folder_not_found(mock_drive):
    from appscriptly.services.drive.api import move_to_folder

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
