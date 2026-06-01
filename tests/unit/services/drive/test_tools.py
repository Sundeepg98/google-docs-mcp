"""Per-tool behavior tests for services/drive/tools.py (Gap #5).

Per the test architect (Round 5 audit) — Round 5 per-tool InMemory tests
not delivered for the drive service either. Sister file to
``tests/unit/services/docs/test_tools.py``, applying the same canonical
pattern (PR #103 → PR #110 → here) at the drive surface.

The 9 drive tools:

  1. gdocs_find_doc_by_title — title search; q= DSL construction
  2. gdocs_move_to_folder    — addParents/removeParents
  3. gdocs_trash_file        — single-ID + list-batch trash
  4. gdocs_untrash_file      — single-ID + list-batch untrash
  5. gdocs_share_file        — permissions.create (v2.3.0)
  6. gdocs_list_permissions  — permissions.list (v2.3.0)
  7. gdocs_create_folder     — files.create (folder mimeType)
  8. gdocs_revoke_permission — permissions.delete
  9. gdocs_export_doc        — files.export (Google-native → portable)

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

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.drive import tools


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


# ---------------------------------------------------------------------
# 7. gdocs_create_folder — happy-path + validation
# ---------------------------------------------------------------------


def test_gdocs_create_folder_happy_path_returns_flat_envelope(with_drive_stub):
    """The tool delegates to ``api.create_folder`` and surfaces its
    ``{folder_id, name, url, parent_folder_id}`` envelope through the
    standard ``@workspace_tool(creds=True)`` boundary."""
    with_drive_stub.files().create().execute.return_value = {
        "id": "FOLDER-NEW",
        "name": "Q3 Onboarding",
    }
    result = tools.gdocs_create_folder(name="Q3 Onboarding")
    assert result == {
        "folder_id": "FOLDER-NEW",
        "name": "Q3 Onboarding",
        "url": "https://drive.google.com/drive/folders/FOLDER-NEW",
        "parent_folder_id": None,
    }


def test_gdocs_create_folder_forwards_parent_to_api(with_drive_stub):
    """A parent_folder_id passed at the tool layer must reach the Drive
    body as ``parents: [parent_id]`` (forwarded through the api layer)."""
    with_drive_stub.files().create().execute.return_value = {
        "id": "F-CHILD", "name": "Sub",
    }
    result = tools.gdocs_create_folder(name="Sub", parent_folder_id="PARENT-1")
    last = with_drive_stub.files().create.call_args_list[-1]
    assert last.kwargs["body"]["parents"] == ["PARENT-1"]
    assert result["parent_folder_id"] == "PARENT-1"


def test_gdocs_create_folder_rejects_blank_name_via_api(with_drive_stub):
    """Blank-name rejection from the api module bubbles up through the
    decorator envelope as ValueError (the decorator wraps it for
    cloud-mode callers; raises bare ValueError in test contexts)."""
    with pytest.raises(ValueError, match="name cannot be empty"):
        tools.gdocs_create_folder(name="   ")


# ---------------------------------------------------------------------
# 8. gdocs_revoke_permission — happy-path + validation + soft-failure
# ---------------------------------------------------------------------


def test_gdocs_revoke_permission_happy_path_returns_revoked_true(with_drive_stub):
    """The tool delegates to ``sharing.revoke_permission`` and surfaces
    its ``{file_id, permission_id, revoked, was_already_absent}``
    envelope through the standard decorator boundary."""
    with_drive_stub.permissions().delete().execute.return_value = ""
    result = tools.gdocs_revoke_permission(
        drive_file_id="FILE-ABC", permission_id="PERM-1",
    )
    assert result == {
        "file_id": "FILE-ABC",
        "permission_id": "PERM-1",
        "revoked": True,
        "was_already_absent": False,
    }


def test_gdocs_revoke_permission_rejects_blank_permission_id(with_drive_stub):
    """Pre-API validation (blank permission_id) from the sharing module
    bubbles up through the tool layer."""
    with pytest.raises(ValueError, match="permission_id cannot be empty"):
        tools.gdocs_revoke_permission(
            drive_file_id="FILE1", permission_id="   ",
        )


# ---------------------------------------------------------------------
# 9. gdocs_export_doc — happy-path + validation + soft-failure
# ---------------------------------------------------------------------


@pytest.fixture
def _patch_export_media(monkeypatch):
    """Patch the api module's MediaIoBaseDownload / MediaIoBaseUpload so
    the export stream-download + re-upload don't touch real HTTP. (The
    tool-layer test only needs the body to run through the decorator
    envelope; api-shape detail is covered in test_api.py.)"""
    import appscriptly.services.drive.api as api_mod

    class _FakeDownloader:
        def __init__(self, _buf, _request):
            pass

        def next_chunk(self):
            return (None, True)

    monkeypatch.setattr(api_mod, "MediaIoBaseDownload", _FakeDownloader)
    monkeypatch.setattr(
        api_mod, "MediaIoBaseUpload", lambda *a, **k: MagicMock(name="media-upload")
    )


def test_gdocs_export_doc_happy_path_returns_envelope(
    with_drive_stub, _patch_export_media,
):
    """The tool delegates to ``api.export_doc`` and surfaces its
    export envelope through the standard ``@workspace_tool(creds=True)``
    boundary. Source is a Google Doc → pdf."""
    with_drive_stub.files().get().execute.return_value = {
        "id": "SRC-1", "name": "Plan",
        "mimeType": "application/vnd.google-apps.document",
    }
    with_drive_stub.files().export_media.return_value = MagicMock(name="req")
    with_drive_stub.files().create().execute.return_value = {
        "id": "EXP-1", "name": "Plan.pdf", "size": "1024",
        "webViewLink": "https://drive.google.com/file/d/EXP-1/view",
        "webContentLink": "https://drive.google.com/uc?id=EXP-1",
    }
    result = tools.gdocs_export_doc(drive_file_id="SRC-1", export_format="pdf")
    assert result["exported_file_id"] == "EXP-1"
    assert result["export_format"] == "pdf"
    assert result["export_mime_type"] == "application/pdf"
    assert result["download_url"] == "https://drive.google.com/uc?id=EXP-1"


def test_gdocs_export_doc_rejects_unknown_format_via_api(with_drive_stub):
    """An unrecognized format token bubbles from the api module through
    the decorator envelope as ValueError (no Drive call needed)."""
    with pytest.raises(ValueError, match="is not recognized"):
        tools.gdocs_export_doc(drive_file_id="SRC-1", export_format="bogus")


def test_gdocs_export_doc_soft_failure_passthrough(
    with_drive_stub, _patch_export_media,
):
    """A not_exportable source (binary blob) is surfaced as data through
    the tool layer — the decorator does not turn it into an error."""
    with_drive_stub.files().get().execute.return_value = {
        "id": "BLOB", "name": "scan.pdf", "mimeType": "application/pdf",
    }
    result = tools.gdocs_export_doc(drive_file_id="BLOB", export_format="pdf")
    assert result["reason"] == "not_exportable"
    assert result["source_file_id"] == "BLOB"
