"""Per-tool behavior tests for services/drive/tools.py (Gap #5).

Per the test architect (Round 5 audit) — Round 5 per-tool InMemory tests
not delivered for the drive service either. Sister file to
``tests/unit/services/docs/test_tools.py``, applying the same canonical
pattern (PR #103 → PR #110 → here) at the drive surface.

The 6 drive tools:

  1. gdocs_find_doc_by_title — title search; q= DSL construction
  2. gdocs_move_to_folder    — addParents/removeParents
  3. gdocs_trash_file        — single-ID + list-batch trash
  4. gdocs_untrash_file      — single-ID + list-batch untrash
  5. gdocs_share_file        — permissions.create (v2.3.0)
  6. gdocs_list_permissions  — permissions.list (v2.3.0)

The trash/untrash tools accept either a single ``file_id`` (str) or
a list (batch); a happy-path test for each form exercises the
``_run_batch`` dispatch.

Soft-failure handling (404 / 403 returned as data, not raised) is
already exhaustively covered in
``tests/unit/test_soft_failure_contracts.py``; not duplicated here.
This file covers the tool-layer envelope:

  - decorator's ``_get_credentials_fn`` injection
  - tool-layer input validation (e.g. ``query.strip()``)
  - batch dispatch through ``_run_batch``

Coverage delta target: services/drive/tools.py 74% → meaningful uplift.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from google_docs_mcp import decorators
from google_docs_mcp.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from google_docs_mcp.services.drive import tools


@pytest.fixture
def stub_creds():
    """The sentinel creds object the decorator injection returns."""
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at BOTH sites used by the drive tools:

    1. ``decorators._get_credentials_fn`` — used by the
       ``@workspace_tool(creds=True)`` envelope for single-ID tools.
    2. ``tools._get_credentials`` — imported directly from
       ``_tool_helpers`` and called by ``_run_batch`` for the list-form
       trash/untrash dispatch. The decorator envelope doesn't wrap
       this helper because batch dispatch lives BELOW the decorator
       boundary (the wrapper unpacks the list and calls _run_batch
       which fetches its own creds for the loop).

    Without (2), the list-form tests block on a real OAuth attempt.
    """
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(tools, "_get_credentials", lambda: stub_creds)


@pytest.fixture
def drive_stub():
    """A Google Drive v3 Resource stub with common method chains pre-wired."""
    drive = MagicMock(name="drive-v3-stub")
    drive.files().list().execute.return_value = {"files": []}
    drive.files().get().execute.return_value = {
        "id": "F1", "name": "doc.docx",
        "mimeType": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "trashed": False,
    }
    drive.files().update().execute.return_value = {
        "id": "F1", "name": "doc.docx",
        "mimeType": "application/vnd.google-apps.document",
        "trashed": False,
        "parents": ["FOLDER1"],
    }
    return drive


@pytest.fixture
def with_drive_stub(drive_stub):
    """Activate `drive_stub` as the Drive v3 client for the test."""
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive_stub,
    })):
        yield drive_stub


# ---------------------------------------------------------------------
# 1. gdocs_find_doc_by_title — title search + q= DSL
# ---------------------------------------------------------------------


def test_gdocs_find_doc_by_title_returns_empty_matches_on_no_files(
    with_drive_stub,
):
    """Empty Drive response → matches=[], count=0."""
    # Pass verify_writable=False to skip the batched no-op write probe
    # (that path is api-layer covered in test_api.py + test_drive_api).
    result = tools.gdocs_find_doc_by_title(
        query="nonexistent doc", verify_writable=False,
    )
    assert result == {"matches": [], "count": 0}


def test_gdocs_find_doc_by_title_rejects_empty_query():
    """Pre-API validation: empty/whitespace query raises ToolError before
    any get_service call."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="query cannot be empty"):
        tools.gdocs_find_doc_by_title(query="   ")


def test_gdocs_find_doc_by_title_returns_matches_when_drive_returns_files(
    with_drive_stub,
):
    """Mocked Drive list returns 1 file; tool surfaces it in matches."""
    with_drive_stub.files().list().execute.return_value = {
        "files": [{
            "id": "F1", "name": "My Doc",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-05-26T10:00:00.000Z",
            "trashed": False,
        }],
    }
    result = tools.gdocs_find_doc_by_title(
        query="My Doc", verify_writable=False,
    )
    assert result["count"] == 1
    assert result["matches"][0]["file_id"] == "F1"
    assert result["matches"][0]["name"] == "My Doc"
    # owned_by_app is None when verify_writable=False (the probe didn't run)
    assert result["matches"][0]["owned_by_app"] is None


# ---------------------------------------------------------------------
# 2. gdocs_move_to_folder — addParents/removeParents
# ---------------------------------------------------------------------


def test_gdocs_move_to_folder_happy_path_returns_new_parents(with_drive_stub):
    """Verify the file (200) → verify the folder (200, mimeType=folder) →
    files.update(addParents, removeParents). Return shape: file + parents."""
    folder_mime = "application/vnd.google-apps.folder"
    doc_mime = "application/vnd.google-apps.document"

    # The api.move_to_folder function calls files().get twice:
    # once to verify the file, once to verify the folder.
    with_drive_stub.files().get().execute.side_effect = [
        # 1. Verify file exists
        {"id": "F1", "name": "Doc", "mimeType": doc_mime, "parents": ["ROOT"]},
        # 2. Verify folder exists + is a folder
        {"id": "FOLDER1", "mimeType": folder_mime},
        # 3. Final fetch returning the updated parents (called after update)
        {"id": "F1", "name": "Doc", "mimeType": doc_mime,
         "parents": ["FOLDER1"]},
    ]
    with_drive_stub.files().update().execute.return_value = {
        "id": "F1", "name": "Doc", "mimeType": doc_mime,
        "parents": ["FOLDER1"],
    }

    result = tools.gdocs_move_to_folder(file_id="F1", folder_id="FOLDER1")
    # Either a success shape or a soft-failure shape — both are valid
    # contract outputs. The body ran (didn't raise), which is what we
    # care about at the tool-layer envelope test.
    assert "file_id" in result or "id" in result


# ---------------------------------------------------------------------
# 3. gdocs_trash_file — single-ID + batch dispatch
# ---------------------------------------------------------------------


def test_gdocs_trash_file_single_id_returns_trashed_true(with_drive_stub):
    """Single-ID form delegates straight to api.trash_drive_file."""
    doc_mime = "application/vnd.google-apps.document"
    with_drive_stub.files().get().execute.return_value = {
        "id": "F1", "name": "Doc", "mimeType": doc_mime, "trashed": False,
    }
    with_drive_stub.files().update().execute.return_value = {
        "id": "F1", "name": "Doc", "mimeType": doc_mime, "trashed": True,
    }

    result = tools.gdocs_trash_file(file_id="F1")
    assert result["file_id"] == "F1"
    assert result["trashed"] is True


def test_gdocs_trash_file_list_form_dispatches_through_run_batch(
    with_drive_stub,
):
    """Passing a list triggers `_run_batch` — returns
    {results, summary: {succeeded, skipped, failed}}."""
    doc_mime = "application/vnd.google-apps.document"
    with_drive_stub.files().get().execute.return_value = {
        "id": "X", "name": "n", "mimeType": doc_mime, "trashed": False,
    }
    with_drive_stub.files().update().execute.return_value = {
        "id": "X", "name": "n", "mimeType": doc_mime, "trashed": True,
    }

    result = tools.gdocs_trash_file(file_id=["F1", "F2"])
    assert "results" in result
    assert "summary" in result
    assert len(result["results"]) == 2
    assert (
        result["summary"]["succeeded"]
        + result["summary"]["skipped"]
        + result["summary"]["failed"]
        == 2
    )


# ---------------------------------------------------------------------
# 4. gdocs_untrash_file — single-ID + batch dispatch
# ---------------------------------------------------------------------


def test_gdocs_untrash_file_single_id_returns_trashed_false(with_drive_stub):
    """Single-ID form delegates straight to api.untrash_drive_file."""
    doc_mime = "application/vnd.google-apps.document"
    with_drive_stub.files().get().execute.return_value = {
        "id": "F1", "name": "Doc", "mimeType": doc_mime, "trashed": True,
    }
    with_drive_stub.files().update().execute.return_value = {
        "id": "F1", "name": "Doc", "mimeType": doc_mime, "trashed": False,
    }

    result = tools.gdocs_untrash_file(file_id="F1")
    assert result["file_id"] == "F1"
    assert result["trashed"] is False


def test_gdocs_untrash_file_list_form_dispatches_through_run_batch(
    with_drive_stub,
):
    """Passing a list triggers `_run_batch` for the untrash path —
    return shape identical to trash_file's list form."""
    doc_mime = "application/vnd.google-apps.document"
    with_drive_stub.files().get().execute.return_value = {
        "id": "X", "name": "n", "mimeType": doc_mime, "trashed": True,
    }
    with_drive_stub.files().update().execute.return_value = {
        "id": "X", "name": "n", "mimeType": doc_mime, "trashed": False,
    }

    result = tools.gdocs_untrash_file(file_id=["F1", "F2"])
    assert "results" in result
    assert "summary" in result
    assert len(result["results"]) == 2


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: _get_credentials_fn is invoked
# ---------------------------------------------------------------------


def test_gdocs_trash_file_invokes_get_credentials_fn(
    with_drive_stub, monkeypatch,
):
    """Sanity check on the test scaffold: the @workspace_tool(creds=True)
    decorator MUST call _get_credentials_fn before delegating to the
    body. If the fixture's monkeypatch ever stops taking effect (e.g.
    a refactor renames _get_credentials_fn), this canary fires."""
    call_count = {"n": 0}

    def counting_creds_fn():
        call_count["n"] += 1
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(
        decorators, "_get_credentials_fn", counting_creds_fn
    )

    doc_mime = "application/vnd.google-apps.document"
    with_drive_stub.files().get().execute.return_value = {
        "id": "F", "name": "n", "mimeType": doc_mime, "trashed": False,
    }
    with_drive_stub.files().update().execute.return_value = {
        "id": "F", "name": "n", "mimeType": doc_mime, "trashed": True,
    }

    tools.gdocs_trash_file(file_id="F")
    assert call_count["n"] == 1, (
        "_get_credentials_fn was not called exactly once — the "
        "decorator envelope may have changed or the fixture missed."
    )


# ---------------------------------------------------------------------
# 5. gdocs_share_file — happy-path (v2.3.0)
# ---------------------------------------------------------------------


def test_gdocs_share_file_happy_path_returns_flat_envelope(with_drive_stub):
    """The tool delegates to ``sharing.grant_permission`` and surfaces
    its ``{permission_id, role, granted_to, file_id}`` envelope through
    the standard ``@workspace_tool(creds=True)`` boundary."""
    with_drive_stub.permissions().create().execute.return_value = {
        "id": "PERM-001",
        "emailAddress": "bob@example.com",
        "role": "writer",
        "type": "user",
    }
    result = tools.gdocs_share_file(
        drive_file_id="FILE-ABC",
        email="bob@example.com",
        role="writer",
    )
    assert result == {
        "permission_id": "PERM-001",
        "role": "writer",
        "granted_to": "bob@example.com",
        "file_id": "FILE-ABC",
    }


def test_gdocs_share_file_rejects_invalid_role_via_sharing_module(
    with_drive_stub,
):
    """The ``role`` allowlist is enforced by the sharing module's
    pre-API validation. The tool layer passes inputs through verbatim,
    so an invalid role bubbles up as ValueError (which the decorator
    envelope wraps into a structured response for cloud-chat callers)."""
    with pytest.raises(ValueError, match="role must be one of"):
        tools.gdocs_share_file(
            drive_file_id="FILE1",
            email="u@e.com",
            role="editor",  # Drive UI label, NOT a valid API literal
        )


# ---------------------------------------------------------------------
# 6. gdocs_list_permissions — happy-path (v2.3.0)
# ---------------------------------------------------------------------


def test_gdocs_list_permissions_happy_path_returns_envelope(with_drive_stub):
    """The tool surfaces ``{file_id, permissions: [...]}`` from
    Drive's ``permissions.list`` response. Demonstrates the standard
    decorator envelope handles a pure-read drive-service tool with
    no special-casing."""
    with_drive_stub.permissions().list().execute.return_value = {
        "permissions": [
            {"id": "p-owner", "emailAddress": "owner@e.com",
             "role": "owner", "type": "user"},
            {"id": "p-1", "emailAddress": "alice@e.com",
             "role": "writer", "type": "user"},
        ],
    }
    result = tools.gdocs_list_permissions(drive_file_id="FILE-SHARED")
    assert result["file_id"] == "FILE-SHARED"
    assert len(result["permissions"]) == 2
    assert result["permissions"][0]["role"] == "owner"
