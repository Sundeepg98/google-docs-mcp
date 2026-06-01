"""Google Drive API wrapper for .docx upload + convert.

Two entry points:

- ``upload_and_convert_docx`` — local-file path: read a .docx from disk,
  upload to Drive with conversion to Google Doc. Used when the MCP is
  called with a filesystem path (Claude Code, Claude Desktop).
- ``fetch_and_convert_drive_docx`` — Drive-ID path: read a .docx that
  already lives in the user's Drive (uploaded by some other app, e.g.
  Claude.ai cloud chat's Drive connector), then upload+convert under
  our own app's ownership. Used when the MCP is called from an
  environment that can't pass local file paths.

Both produce the same downstream result: a Google Doc owned by our
OAuth user, suitable for Docs API operations.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC_MIME = "application/vnd.google-apps.document"

# Drive enforces a 50 MB upload limit and a 1.02M-character cap on the
# resulting Doc. We surface a friendly error if the source exceeds the
# upload size.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def upload_and_convert_docx(
    creds: Credentials,
    docx_path: Path,
    title: str | None = None,
) -> dict:
    """Upload a local ``.docx`` file to Drive and have Drive convert it.

    Returns ``{"doc_id": str, "url": str, "title": str}``. The resulting
    Google Doc is owned by the OAuth user; subsequent Docs API
    operations on it work under the standard ``documents`` scope.
    """
    if not docx_path.exists():
        raise FileNotFoundError(
            f"docx_path not found: {docx_path}. The 'docx_path' parameter "
            "only works when the MCP server can see the file on its own "
            "filesystem — i.e. when running locally as a stdio MCP host "
            "(Claude Code / Claude Desktop). From claude.ai cloud chat the "
            "server cannot see your sandbox's filesystem; instead either "
            "(a) call get_signed_upload_url and POST the .docx bytes to "
            "the returned URL via your Python sandbox, or (b) upload to "
            "Drive first and pass drive_file_id."
        )
    if docx_path.suffix.lower() != ".docx":
        raise ValueError(
            f"Expected a .docx file (got '{docx_path.suffix}'). "
            "Older .doc files aren't accepted by Drive's converter; "
            "convert to .docx first via Word or Google Docs."
        )
    size = docx_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File is {size / 1024 / 1024:.1f} MB; "
            f"Drive upload limit for conversion is {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    drive = get_service("drive", "v3", credentials=creds)
    media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
    body = {
        "name": title or docx_path.stem,
        "mimeType": GDOC_MIME,
    }
    file = drive.files().create(
        body=body, media_body=media, fields="id,name"
    ).execute()
    doc_id = file["id"]
    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "title": file["name"],
    }


def fetch_and_convert_drive_docx(
    creds: Credentials,
    drive_file_id: str,
    title: str | None = None,
) -> dict:
    """Read a .docx already in Drive (any owner) and re-create as a Google Doc.

    The source file's bytes are streamed via Drive's ``files.get_media``
    using our ``drive.readonly`` scope, then re-uploaded via
    ``files.create`` with ``mimeType=GDOC_MIME`` so the conversion runs
    under our app's ownership (``drive.file`` scope). The original .docx
    is left in place — we never modify or delete it.

    This is the workflow for Claude.ai cloud chat: the user attaches
    or generates a .docx in chat, cloud chat uploads it to Drive via
    the Anthropic Drive connector (different app, different scopes),
    then hands the file ID to this tool.
    """
    drive = get_service("drive", "v3", credentials=creds)

    meta = drive.files().get(
        fileId=drive_file_id, fields="id,name,mimeType,size"
    ).execute()
    if meta.get("mimeType") != DOCX_MIME:
        raise ValueError(
            f"Drive file {drive_file_id!r} is not a .docx "
            f"(mimeType: {meta.get('mimeType')!r}). "
            "Convert to .docx via Word or Google Docs first."
        )
    size = int(meta.get("size") or 0)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Drive file is {size / 1024 / 1024:.1f} MB; "
            f"Drive's conversion limit is {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    buf = io.BytesIO()
    request = drive.files().get_media(fileId=drive_file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    buf.seek(0)

    fallback_title = meta["name"]
    if fallback_title.lower().endswith(".docx"):
        fallback_title = fallback_title[:-5]

    media = MediaIoBaseUpload(buf, mimetype=DOCX_MIME, resumable=False)
    file = drive.files().create(
        body={"name": title or fallback_title, "mimeType": GDOC_MIME},
        media_body=media,
        fields="id,name",
    ).execute()
    doc_id = file["id"]
    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "title": file["name"],
        "source_drive_file_id": drive_file_id,
    }


def copy_google_doc(
    creds: Credentials,
    google_doc_id: str,
    title: str | None = None,
) -> dict:
    """Make a working copy of an existing Google Doc (no .docx conversion).

    Used when the user's Drive already has a Google Doc (e.g. because
    they uploaded a .docx via the Drive web UI with auto-convert ON,
    which discards the raw .docx bytes). We can't re-import — there
    are no .docx bytes to re-import. Instead we copy the Google Doc
    and restructure the copy, leaving the original untouched.

    The copy is created with ``drive.files.copy()`` and inherits the
    user's ownership. Our restructure pipeline then modifies the copy
    in place via REST + Apps Script — same code path as the .docx
    conversion case, just without the initial mime-type conversion.
    """
    drive = get_service("drive", "v3", credentials=creds)
    meta = drive.files().get(
        fileId=google_doc_id, fields="id,name,mimeType"
    ).execute()
    if meta.get("mimeType") != GDOC_MIME:
        raise ValueError(
            f"Drive file {google_doc_id!r} is not a Google Doc "
            f"(mimeType: {meta.get('mimeType')!r}). For .docx files "
            "use fetch_and_convert_drive_docx instead."
        )

    fallback_title = (title or meta["name"]) + " (tabified)"
    new_file = drive.files().copy(
        fileId=google_doc_id,
        body={"name": fallback_title},
        fields="id,name",
    ).execute()
    new_id = new_file["id"]
    return {
        "doc_id": new_id,
        "url": f"https://docs.google.com/document/d/{new_id}/edit",
        "title": new_file["name"],
        "source_google_doc_id": google_doc_id,
    }


def untrash_drive_file(creds: Credentials, drive_file_id: str) -> dict:
    """Restore a Drive file from trash to its original location.

    Inverse of ``trash_drive_file``. Sets ``trashed=False`` via
    ``files.update``. Same graceful-error semantics: 404 (file not
    found) and 403 (app_not_authorized) return as soft-failure dicts
    instead of raising, so batch restores can skip-and-continue.

    Idempotent: untrashing a not-currently-trashed file succeeds and
    flags ``was_already_active: true``.

    Returns:
        Success: ``{file_id, name, mimeType, trashed: False,
        was_already_active: bool}``.
        Soft-failure: ``{file_id, trashed: <current>, reason,
        message}`` where ``reason`` is ``"not_found"`` or
        ``"app_not_authorized"``.

        Recovery window: Drive auto-purges trashed files after 30
        days. Beyond that, the file is gone permanently and this
        returns ``not_found``.
    """
    drive = get_service("drive", "v3", credentials=creds)

    try:
        # PR-Δ3.5: gdocs_untrash_file is idempotent=True; wrap.
        # 404/403 propagate to the existing handler (retry policy only
        # covers 429/5xx).
        before = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="id,name,mimeType,trashed"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "trashed": False,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the "
                    "ID; the file may have been permanently deleted "
                    "(beyond the 30-day trash window) or the OAuth user "
                    "lacks any access to it."
                ),
            }
        raise

    was_already_active = not bool(before.get("trashed"))

    try:
        # PR-Δ3.5: setting trashed=False twice is a true no-op; safe to retry.
        updated = execute_with_retry(
            lambda: drive.files().update(
                fileId=drive_file_id,
                body={"trashed": False},
                fields="id,name,mimeType,trashed",
            ).execute(),
            idempotent=True,
            op_name="drive.files.update.untrash",
        )
    except HttpError as e:
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "name": before.get("name"),
                    "mimeType": before.get("mimeType"),
                    "trashed": bool(before.get("trashed")),
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks write access to this file — it "
                        "wasn't created by this app. drive.file scope "
                        "only permits writes to app-created files. To "
                        "untrash, the file's owner must do it via the "
                        "Drive UI."
                    ),
                }
        raise

    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "mimeType": updated.get("mimeType"),
        "trashed": bool(updated.get("trashed")),
        "was_already_active": was_already_active,
    }


def find_doc_by_title(
    creds: Credentials,
    query: str,
    *,
    exact: bool = False,
    include_trashed: bool = False,
    page_size: int = 50,
    verify_writable: bool = False,
) -> dict:
    """Search Drive for Google Docs / .docx files matching a title.

    Newest-first by modified_time. Each match includes whether it's
    trashed and (if ``verify_writable=True``) whether this OAuth app's
    drive.file scope can actually write to it — which is the same
    test that determines whether ``trash_drive_file`` /
    ``untrash_drive_file`` / ``move_to_folder`` will succeed.

    Args:
        query: title text to match.
        exact: True = exact match (``name = 'X'``); False = substring
            (``name contains 'X'``).
        include_trashed: False (default) excludes trashed files.
        page_size: max results to return (Drive API caps at 100).
        verify_writable: False (default; v2.2.1+) — pure read; result
            ``owned_by_app`` is ``None`` (unknown). Pass True to opt
            into a batched no-op-update PROBE per match that triggers
            Drive's drive.file scope check (the SAME check that
            trash/untrash/move runs), populating ``owned_by_app`` as
            ``True``/``False``. Costs one extra batched HTTP request
            per call AND mutates the Drive audit log (the probe is a
            write at the API level even if the value doesn't change).

            **Default flipped to False in v2.2.1 (R33 audit Gap #3 /
            CQRS):** the tool wrapping this function is annotated
            ``readonly=True``, so the default behavior MUST be a pure
            read. Callers who genuinely need the writability check can
            opt in explicitly.

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time,
        trashed, owned_by_app}, ...], "count": int}``.
        ``owned_by_app`` is ``True``/``False`` if probed, ``None`` if
        ``verify_writable=False`` (the default).
    """
    drive = get_service("drive", "v3", credentials=creds)

    # Escape single quotes inside the query — Drive's q DSL requires
    # quoting them with a backslash.
    safe_query = query.replace("'", "\\'")
    operator = "=" if exact else "contains"
    q_parts = [f"name {operator} '{safe_query}'"]
    q_parts.append(
        "(mimeType = 'application/vnd.google-apps.document' OR "
        "mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')"
    )
    if not include_trashed:
        q_parts.append("trashed = false")
    q = " and ".join(q_parts)

    # PR-Δ3.5: gdocs_find_doc_by_title is readonly=True, idempotent=True.
    resp = execute_with_retry(
        lambda: drive.files().list(
            q=q,
            orderBy="modifiedTime desc",
            pageSize=min(max(page_size, 1), 100),
            fields="files(id,name,mimeType,modifiedTime,trashed)",
        ).execute(),
        idempotent=True,
        op_name="drive.files.list",
    )

    matches: list[dict] = []
    for f in resp.get("files", []):
        matches.append({
            "file_id": f["id"],
            "name": f.get("name", ""),
            "mimeType": f.get("mimeType", ""),
            "modified_time": f.get("modifiedTime", ""),
            "trashed": bool(f.get("trashed")),
            "owned_by_app": None,  # filled below if verify_writable
        })

    # Probe writability with a batched no-op update per match. Setting
    # ``trashed`` to its current value is a true no-op (state doesn't
    # change) but still triggers Drive's drive.file scope check — the
    # SAME check that trash_drive_file's real update triggers. If the
    # probe succeeds, the file is writable under this app's scope; if
    # it 403s with appNotAuthorizedToFile, it isn't.
    #
    # NOTE: ``capabilities.canTrash`` and ``capabilities.canEdit`` are
    # USER-LEVEL signals — they reflect what the OAuth user is allowed
    # to do, NOT what this app's scope permits. They wrongly report
    # True for files the user owns but uploaded outside this app.
    # That mismatch is why a no-op probe is the only reliable check.
    if verify_writable and matches:
        write_results: dict[str, bool] = {}

        def make_callback(fid: str):
            def cb(_request_id: str, _response: Any, exception: Any) -> None:
                if exception is None:
                    write_results[fid] = True
                elif isinstance(exception, HttpError) and exception.status_code == 403:
                    # Any 403 means we can't write. The specific
                    # reason we care about is appNotAuthorizedToFile,
                    # but any 403 is "not writable for our purposes."
                    write_results[fid] = False
                else:
                    # Unknown error — be conservative and say "we
                    # don't know" rather than claim writable.
                    write_results[fid] = False
            return cb

        batch = drive.new_batch_http_request()
        for m in matches:
            probe_req = drive.files().update(
                fileId=m["file_id"],
                body={"trashed": m["trashed"]},
                fields="id",
            )
            batch.add(probe_req, callback=make_callback(m["file_id"]))
        batch.execute()

        for m in matches:
            m["owned_by_app"] = write_results.get(m["file_id"], False)

    return {"matches": matches, "count": len(matches)}


def move_to_folder(
    creds: Credentials, drive_file_id: str, folder_id: str
) -> dict:
    """Move a Drive file from its current parents into ``folder_id``.

    Uses ``files.update`` with ``addParents``/``removeParents`` — moves
    in place, no copy. Soft-failure on 403 app_not_authorized and 404
    not_found, matching ``trash_drive_file``'s contract so batch
    workflows can skip-and-continue.

    Returns:
        Success: ``{file_id, name, mimeType, parents: [...]}``.
        Soft-failure: ``{file_id, reason, message, parents?}`` where
        ``reason`` is one of:
        - ``"not_found"`` — file_id doesn't resolve
        - ``"folder_not_found"`` — folder_id doesn't resolve
        - ``"app_not_authorized"`` — drive.file scope can't write
    """
    drive = get_service("drive", "v3", credentials=creds)

    try:
        # PR-Δ3.5: gdocs_move_to_folder is idempotent=True.
        before = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id,
                fields="id,name,mimeType,parents",
            ).execute(),
            idempotent=True,
            op_name="drive.files.get",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the ID."
                ),
            }
        raise

    # Sanity-check the folder exists and is actually a folder. Catching
    # this here gives a clean reason instead of relying on Drive's
    # error message for an invalid addParents value.
    try:
        # PR-Δ3.5: folder existence check is a pure read.
        folder_meta = execute_with_retry(
            lambda: drive.files().get(
                fileId=folder_id, fields="id,mimeType"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get.folder",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "reason": "folder_not_found",
                "message": (
                    f"Target folder {folder_id!r} not found. Verify the ID; "
                    "shared-with-me folders may need the user to add them "
                    "to My Drive first."
                ),
            }
        raise
    if folder_meta.get("mimeType") != "application/vnd.google-apps.folder":
        return {
            "file_id": drive_file_id,
            "reason": "folder_not_found",
            "message": (
                f"Target id {folder_id!r} is not a folder "
                f"(mimeType: {folder_meta.get('mimeType')!r})."
            ),
        }

    current_parents = before.get("parents", []) or []
    if folder_id in current_parents and len(current_parents) == 1:
        # Already in the target folder, no-op.
        return {
            "file_id": drive_file_id,
            "name": before.get("name"),
            "mimeType": before.get("mimeType"),
            "parents": current_parents,
            "note": "file was already in the target folder; no move performed",
        }

    try:
        # PR-Δ3.5: moving to the same folder twice is a no-op; safe to retry.
        updated = execute_with_retry(
            lambda: drive.files().update(
                fileId=drive_file_id,
                addParents=folder_id,
                removeParents=",".join(current_parents) if current_parents else None,
                fields="id,name,mimeType,parents",
            ).execute(),
            idempotent=True,
            op_name="drive.files.update.move",
        )
    except HttpError as e:
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "name": before.get("name"),
                    "mimeType": before.get("mimeType"),
                    "parents": current_parents,
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks write access — file wasn't created "
                        "by this app. drive.file scope only permits writes "
                        "to app-created files."
                    ),
                }
        raise

    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "mimeType": updated.get("mimeType"),
        "parents": updated.get("parents", []),
    }


def create_folder(
    creds: Credentials,
    name: str,
    parent_folder_id: str | None = None,
) -> dict:
    """Create a Drive folder via ``files.create`` (folder mimeType).

    Uses ``files.create`` with
    ``mimeType="application/vnd.google-apps.folder"`` — the documented
    way to create a folder through the Drive API (a folder is just a
    file whose mimeType is the folder type). Optionally nests the new
    folder inside ``parent_folder_id``; omitting it lands the folder in
    Drive root (My Drive).

    The created folder is owned by the OAuth user and — because this
    app created it — is fully writable under the ``drive.file`` scope.
    That makes it a natural destination for ``move_to_folder``: a doc
    created by this app can be filed into a folder created by this app
    without ever touching ``drive.full``.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope.
        name: The folder's display name. Empty / whitespace rejected
            client-side (Drive would create a folder literally named
            "Untitled folder" otherwise, which is rarely intended).
        parent_folder_id: Optional parent folder Drive ID. When given,
            the new folder is created INSIDE it (``parents=[id]``).
            When omitted (default), the folder lands in Drive root.

    Returns:
        ``{folder_id, name, url, parent_folder_id}`` — a flat shape the
        agent can act on. ``folder_id`` feeds straight into
        ``move_to_folder`` (as ``folder_id``) to file documents into the
        new folder. ``url`` deep-links to the folder in the Drive UI.
        ``parent_folder_id`` echoes the parent back (``None`` when the
        folder was created in root) for confirmation.

    Raises:
        ValueError: ``name`` empty / whitespace. Cheap rejection before
            the Drive round-trip.
        HttpError: any non-2xx from Drive — e.g. 404 when
            ``parent_folder_id`` doesn't resolve, or 403
            ``appNotAuthorizedToFile`` when the parent wasn't created by
            this app (``drive.file`` can't write into a folder it
            doesn't own). The tool-layer envelope
            (``_format_http_error``) renders this as a structured
            response.

    Note:
        ``files.create`` is NOT idempotent — calling twice creates two
        distinct folders with the same name (Drive permits duplicate
        names; folders are keyed by ID, not name). The tool wrapper is
        annotated ``idempotent=False`` accordingly, and the call is NOT
        wrapped in ``execute_with_retry`` — a transient retry after a
        request that actually landed would create a duplicate folder.
    """
    if not name or not name.strip():
        raise ValueError(
            "name cannot be empty — Drive requires a folder name. "
            "(An empty name would create a folder literally titled "
            "'Untitled folder'.)"
        )

    drive = get_service("drive", "v3", credentials=creds)
    body: dict[str, Any] = {
        "name": name.strip(),
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        body["parents"] = [parent_folder_id]

    # NOT idempotent — single attempt (matches create_presentation /
    # create_spreadsheet). No execute_with_retry: replaying a create
    # that already landed would spawn a duplicate folder.
    created = drive.files().create(
        body=body,
        fields="id,name",
    ).execute()
    folder_id = created["id"]
    return {
        "folder_id": folder_id,
        "name": created.get("name", name.strip()),
        "url": f"https://drive.google.com/drive/folders/{folder_id}",
        "parent_folder_id": parent_folder_id,
    }


def is_file_trashed(creds: Credentials, drive_file_id: str) -> bool:
    """Return whether the Drive file is currently in trash.

    Used by read-side tools (``get_doc_outline``, ``read_*``) to
    surface ``trashed: true`` in responses so callers know they're
    working with a hidden file. Best-effort — if the lookup itself
    fails (e.g. file deleted permanently), returns False.
    """
    drive = get_service("drive", "v3", credentials=creds)
    try:
        # PR-Δ3.5: pure read used by readonly tool wrappers.
        meta = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="trashed"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get.trashed_check",
        )
        return bool(meta.get("trashed"))
    except Exception:  # noqa: BLE001
        return False


def classify_drive_file(creds: Credentials, drive_file_id: str) -> str:
    """Return the mime type of a Drive file. Used to route to the
    right ingestion function (raw .docx vs already-converted Google Doc)."""
    drive = get_service("drive", "v3", credentials=creds)
    # PR-Δ3.5: pure read; classifies file type so callers can route ingestion.
    meta = execute_with_retry(
        lambda: drive.files().get(
            fileId=drive_file_id, fields="mimeType"
        ).execute(),
        idempotent=True,
        op_name="drive.files.get.classify",
    )
    return meta.get("mimeType", "")


def trash_drive_file(creds: Credentials, drive_file_id: str) -> dict:
    """Move a Drive file to trash (recoverable for 30 days).

    Idempotent: setting ``trashed=True`` on an already-trashed file
    succeeds without error. Uses ``files.update`` (trash) — NEVER
    ``files.delete`` (permanent purge), so the operation is reversible.

    Returns:
        Success: ``{file_id, name, mimeType, trashed: True,
        was_already_trashed: bool}``.
        Soft-failure (NOT raised, returned as data so batch cleanups
        can skip-and-continue): ``{file_id, trashed: False, reason,
        message}`` where ``reason`` is:
        - ``"not_found"`` — file id doesn't resolve (404 on get)
        - ``"app_not_authorized"`` — OAuth app lacks write access
          (file wasn't created by this app; drive.file scope can only
          touch files this app owns; 403 appNotAuthorizedToFile)

        Other errors still raise ``HttpError`` so genuine bugs surface.
    """
    drive = get_service("drive", "v3", credentials=creds)

    # Read current state first so we can flag idempotent no-ops AND
    # detect non-existent IDs early with a clean reason.
    try:
        # PR-Δ3.5: gdocs_trash_file is destructive=True but idempotent=True
        # (trashing twice = same end state). Wrap.
        before = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="id,name,mimeType,trashed"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "trashed": False,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the "
                    "ID; the file may have been permanently deleted or "
                    "the OAuth user lacks any access to it."
                ),
            }
        raise

    was_already_trashed = bool(before.get("trashed"))

    try:
        # PR-Δ3.5: trashing an already-trashed file is a no-op; safe to retry.
        updated = execute_with_retry(
            lambda: drive.files().update(
                fileId=drive_file_id,
                body={"trashed": True},
                fields="id,name,mimeType,trashed",
            ).execute(),
            idempotent=True,
            op_name="drive.files.update.trash",
        )
    except HttpError as e:
        # drive.file scope only grants write to files this app created.
        # Trashing a file uploaded externally (e.g. via the Drive web UI
        # by the user, or by another app) returns 403
        # appNotAuthorizedToFile. Return that as data, not as an
        # exception, so a batch cleanup can skip-and-continue.
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "name": before.get("name"),
                    "mimeType": before.get("mimeType"),
                    "trashed": False,
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks write access to this file — it "
                        "wasn't created by this app. drive.file scope "
                        "only permits writes to app-created files. To "
                        "trash, the file's owner must do it via the "
                        "Drive UI."
                    ),
                }
        raise

    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "mimeType": updated.get("mimeType"),
        "trashed": bool(updated.get("trashed")),
        "was_already_trashed": was_already_trashed,
    }
