"""Discriminating tests for Wave 5 S1 - gdrive_copy_file (files.copy).

Stubs ``files().copy()`` and asserts on the body / fields it built plus
the flat ``{file_id, name, url}`` envelope, mirroring the create_folder
capture pattern in ``test_api.py``. A revert (wrong body, missing
webViewLink field, a stray parents= that would relocate the copy)
fails here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.drive.api import copy_drive_file


@pytest.fixture
def stub_drive_for_copy():
    """A Drive stub whose files().copy().execute() returns a plausible copy."""
    drive = MagicMock(name="drive-v3-stub-copy")
    drive.files().copy().execute.return_value = {
        "id": "COPY-NEW",
        "name": "Invoice 1234",
        "webViewLink": "https://docs.google.com/document/d/COPY-NEW/edit",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def _last_copy_kwargs(drive: MagicMock) -> dict:
    """The kwargs of the most recent files().copy(...) call carrying a body."""
    for call in reversed(drive.files().copy.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs
    raise AssertionError("no files().copy() call captured a body= kwarg")


def test_copy_file_rejects_blank_title():
    """Empty / whitespace title is a caller bug - rejected client-side
    BEFORE the round-trip, so MagicMock creds suffice."""
    with pytest.raises(ValueError, match="title cannot be the empty string"):
        copy_drive_file(MagicMock(), "FILE1", "   ")
    with pytest.raises(ValueError, match="title cannot be the empty string"):
        copy_drive_file(MagicMock(), "FILE1", "")


def test_copy_file_passes_title_as_name(stub_drive_for_copy):
    """A title lands as body['name'] and copies the given fileId."""
    copy_drive_file(MagicMock(), "FILE1", "Invoice 1234")
    kw = _last_copy_kwargs(stub_drive_for_copy)
    assert kw["fileId"] == "FILE1"
    assert kw["body"]["name"] == "Invoice 1234"


def test_copy_file_omits_name_when_no_title(stub_drive_for_copy):
    """No title -> body carries NO 'name' key, so Drive applies its own
    'Copy of <original>' default (passing name=None would 400)."""
    copy_drive_file(MagicMock(), "FILE1")
    kw = _last_copy_kwargs(stub_drive_for_copy)
    assert "name" not in kw["body"]


def test_copy_file_requests_webviewlink_field(stub_drive_for_copy):
    """The copy MUST request webViewLink so the returned url is Drive's
    own canonical link (correct for any file type), not a guessed URL."""
    copy_drive_file(MagicMock(), "FILE1")
    kw = _last_copy_kwargs(stub_drive_for_copy)
    assert "webViewLink" in kw["fields"]


def test_copy_file_sets_no_parents(stub_drive_for_copy):
    """The body carries NO 'parents' key, so the tool never force-relocates
    the copy; Drive applies its own default placement (My Drive root in v3).
    A stray parents= would move the copy somewhere unexpected."""
    copy_drive_file(MagicMock(), "FILE1", "Invoice 1234")
    kw = _last_copy_kwargs(stub_drive_for_copy)
    assert "parents" not in kw["body"]


def test_copy_file_maps_result_to_id_name_url(stub_drive_for_copy):
    """The reply's id/name/webViewLink map to file_id/name/url."""
    result = copy_drive_file(MagicMock(), "FILE1", "Invoice 1234")
    assert result == {
        "file_id": "COPY-NEW",
        "name": "Invoice 1234",
        "url": "https://docs.google.com/document/d/COPY-NEW/edit",
    }


def test_copy_file_url_falls_back_without_webviewlink():
    """If Drive omits webViewLink, url falls back to a generic Drive link
    built from the new id (never empty)."""
    drive = MagicMock(name="drive-v3-no-link")
    drive.files().copy().execute.return_value = {"id": "COPY-2", "name": "x"}
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = copy_drive_file(MagicMock(), "FILE1")
    assert result["file_id"] == "COPY-2"
    assert result["url"] == "https://drive.google.com/file/d/COPY-2/view"
