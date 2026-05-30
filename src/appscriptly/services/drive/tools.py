"""Google Drive MCP tool registrations (M3 Phase B — v2.1.4).

This module defines the ``@gdocs_tool``-decorated tool functions for
the Drive file-management service. Importing this module triggers
registration with the live ``mcp`` instance — ``server.py`` performs
the import at the bottom of its module, AFTER constructing ``mcp``
and AFTER ``decorators.register(mcp, ...)`` wires the ``@gdocs_tool``
decorator.

**Tools registered here** (6 drive-service tools):

1. ``gdocs_find_doc_by_title`` — look up a Google Doc / .docx by title (search)
2. ``gdocs_move_to_folder``    — move a file into a Drive folder
3. ``gdocs_untrash_file``      — restore a trashed file (single or batch)
4. ``gdocs_trash_file``        — move a file to trash (single or batch)
5. ``gdocs_share_file``        — grant a user permission on a file (v2.3.0)
6. ``gdocs_list_permissions``  — list who has access to a file (v2.3.0)

The trash/untrash tools accept either a single ``file_id: str`` or a
``list[str]``; the list form delegates to ``_run_batch`` (also lives
in this module since it's drive-specific). Soft-failure handling (404 /
403 returned as data, not raised) is preserved bit-for-bit.

The sharing tools (5, 6) delegate to ``services/drive/sharing.py`` —
a separate sub-module per the multi-service feasibility audit
("sharing model is a different mental domain"). They reuse the same
``google_clients.get_service`` chokepoint and ``drive.file`` OAuth
scope, so no auth surface change shipped with v2.3.0 — only new
behavior on already-granted scopes.

**Import discipline.** This module reaches back into ``server.py`` for
``_get_credentials`` + ``_format_http_error`` via the same
``_get_server_helpers()`` deferred-binding shim as ``services/docs/tools.py``.
Per the M3 Phase B brief: do NOT yet extract those helpers to
``_tool_helpers.py`` — defer until Phase C if it also replicates the
shim (let the second/third consumer drive the shape).
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError

from appscriptly.decorators import workspace_tool
from appscriptly.services.drive.api import (
    find_doc_by_title as _find_doc_by_title,
    move_to_folder as _move_to_folder,
    trash_drive_file as _trash_drive_file,
    untrash_drive_file as _untrash_drive_file,
)
from appscriptly.services.drive.sharing import (
    grant_permission as _grant_permission,
    list_permissions as _list_permissions,
)
from appscriptly.tool_schemas import (
    GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
    GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA,
    GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
    GDOCS_SHARE_FILE_OUTPUT_SCHEMA,
    GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
    GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
)


# Tool-layer helpers — direct import from _tool_helpers.
#
# M3 Phase C (v2.1.5) extraction trigger landing: the Hex specialist's
# Round 2 deferral ("don't extract until the third consumer reveals
# the right abstraction") triggered when gas_deploy/tools.py also
# needed the same 2 helpers. The 3-consumer subset
# {_get_credentials, _format_http_error} now lives in
# _tool_helpers.py — direct top-level import here replaces the
# pre-Phase-C _get_server_helpers() shim. No server.py reach-back.
from appscriptly._tool_helpers import (
    _format_http_error,
    _get_credentials,
)


# ---------------------------------------------------------------------
# _run_batch — drive-specific helper for trash/untrash list inputs
# ---------------------------------------------------------------------


def _run_batch(items: list[str], fn, success_key: str) -> dict:
    """Apply ``fn(creds, file_id)`` to each id, aggregate per-item.

    Used by the batch forms of trash/untrash. Each item's outcome is
    independent — a 403/404 on one doesn't stop the rest. Returns
    ``{results: [...], summary: {succeeded, skipped, failed}}`` where:
    - succeeded = item ended in the desired terminal state
    - skipped   = soft-failure (not_found, app_not_authorized)
    - failed    = unexpected hard error captured per-item
    """
    creds = _get_credentials()
    results: list[dict] = []
    succeeded = 0
    skipped = 0
    failed = 0
    for fid in items:
        try:
            r = fn(creds, fid)
            results.append(r)
            if r.get("reason"):
                skipped += 1
            elif r.get(success_key) is True or (
                success_key == "active" and r.get("trashed") is False
            ):
                succeeded += 1
            else:
                # Defensive — shouldn't happen
                skipped += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            results.append({
                "file_id": fid,
                "reason": "unexpected_error",
                "message": str(e)[:300],
            })
    return {
        "results": results,
        "summary": {
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
        },
    }


# ---------------------------------------------------------------------
# 1. gdocs_find_doc_by_title
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Find a Google Doc by title (search)",
    readonly=True, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
)
def gdocs_find_doc_by_title(
    creds,
    query: str,
    exact: bool = False,
    include_trashed: bool = False,
    verify_writable: bool = False,
) -> dict:
    """Look up a Google Doc / .docx by title — find a file_id from a name.

    USE WHEN: you have a doc name (the user just told you, or it's
    from a past session) and need its file_id to call any other tool.

    Matches return newest-first by modified_time. Each match flags
    ``trashed`` and (optionally, via ``verify_writable=True``)
    ``owned_by_app``:
    - ``trashed: true`` means the file is in Drive Trash (hidden from
      the user's Drive UI; recoverable for 30 days)
    - ``owned_by_app: true`` means this OAuth app's drive.file scope
      can ACTUALLY write to it — i.e. ``gdocs_trash_file`` /
      ``gdocs_untrash_file`` / ``gdocs_move_to_folder`` will succeed.
      This is verified via a batched no-op write probe (NOT inferred
      from user-level capabilities which can disagree).

    Args:
        query: Title text to search for.
        exact: True = exact title match. False (default) = substring
            ("contains") match.
        include_trashed: False (default) excludes trashed files from
            results.
        verify_writable: False (default; v2.2.1+) — pure read; result
            ``owned_by_app`` is ``None`` (unknown). Pass True to opt
            into a batched no-op-update PROBE per match that triggers
            Drive's drive.file scope check, populating ``owned_by_app``
            as ``True``/``False``. Cost: one extra batched HTTP
            request AND a Drive audit-log entry per probed match (the
            no-op update is a write at the API level even though the
            value doesn't change).

            **Default flipped to False in v2.2.1 (R33 audit Gap #3 /
            CQRS):** this tool is annotated ``readonly=True``, so its
            default behavior MUST be a pure read. Pre-v2.2.1 the
            default was True, which silently performed Drive writes
            on every call — a CQRS violation.

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time,
        trashed, owned_by_app}, ...], "count": int}``.
        ``owned_by_app`` is ``True``/``False`` if probed, ``None`` if
        ``verify_writable=False`` (the default).

    Choreography: returns a ``file_id`` that feeds straight into
    ``gdocs_tab_existing_doc`` (drive_file_id), ``gdocs_move_to_folder``,
    ``gdocs_trash_file``, ``gdocs_read_doc`` (as doc_id for Google
    Docs), and ``gdocs_get_doc_outline``. To gate writes on actual
    app-ownership without attempting them first, call again with
    ``verify_writable=True`` (writes the audit log) — otherwise
    ``trash_file`` / ``untrash_file`` / ``move_to_folder`` return a
    structured ``app_not_authorized`` soft-failure response that
    callers can branch on.
    """
    if not query.strip():
        raise ToolError("query cannot be empty")
    return _find_doc_by_title(
        creds, query,
        exact=exact,
        include_trashed=include_trashed,
        verify_writable=verify_writable,
    )


# ---------------------------------------------------------------------
# 2. gdocs_move_to_folder
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Move a file into a Drive folder",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
)
def gdocs_move_to_folder(creds, file_id: str, folder_id: str) -> dict:
    """Move a Drive file into a folder (out of root or wherever it lives).

    USE WHEN: the MCP just created a doc (which lands in Drive root by
    default) and you want to file it into a project / curriculum
    folder. Also works for moving any existing file.

    Uses ``files.update(addParents, removeParents)`` — moves in place,
    not a copy. The file's content and ID are unchanged.

    Soft-failure (returned as data, not raised) matches the trash
    tools' contract so batch workflows can skip-and-continue:
    - ``reason: "not_found"`` — file_id doesn't resolve
    - ``reason: "folder_not_found"`` — folder_id doesn't resolve OR
      points at something that isn't a folder
    - ``reason: "app_not_authorized"`` — OAuth app's drive.file scope
      can't write to this file (file wasn't created by this app)

    Args:
        file_id: The file to move.
        folder_id: The destination folder's Drive ID.

    Returns:
        Success: ``{file_id, name, mimeType, parents: [folder_id, ...]}``.
        No-op (already there): same shape plus ``note`` explaining.
        Soft-failure: ``{file_id, reason, message, ...}``.

    Choreography: file_id typically from ``gdocs_find_doc_by_title`` or
    from a prior create call. ``folder_id`` from the user (URL) or
    ``gdocs_find_doc_by_title`` with mimeType filter — Drive folder
    IDs look identical to file IDs.

    NOTE: same app-ownership constraint as the trash tools — moving a
    file this app didn't create returns ``reason: "app_not_authorized"``.
    """
    return _move_to_folder(creds, file_id, folder_id)


# ---------------------------------------------------------------------
# 3. gdocs_untrash_file
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Restore a file from Drive trash",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
)
def gdocs_untrash_file(creds, file_id: str | list[str]) -> dict:
    """Restore a trashed Drive file back to its original location.

    Inverse of ``gdocs_trash_file``. Ships together so a wrong trash
    call by the agent is recoverable. Works only within Drive's 30-day
    trash window — beyond that the file is permanently gone and this
    returns ``reason: "not_found"``.

    Uses ``files.update(trashed=False)``. Same soft-failure handling
    as ``gdocs_trash_file`` (404 and 403 returned as data, not raised),
    so batch restores can skip-and-continue.

    Args:
        file_id: A single Drive file ID (str) OR a list of IDs for
            batch untrash. List form returns
            ``{results: [...], summary: {succeeded, skipped, failed}}``
            with one result per input ID — independent outcomes.

    Returns (single-ID mode):
        Success: ``{"file_id", "name", "mimeType", "trashed": False,
        "was_already_active": bool}``. ``was_already_active=True``
        means the file wasn't trashed to begin with (idempotent no-op).
        Soft-failure: ``{"file_id", "trashed": <current>, "reason",
        "message"}`` with ``reason`` in {``"not_found"``,
        ``"app_not_authorized"``}.

    Choreography: pairs with ``gdocs_trash_file`` for recovery.

    NOTE: only works on files THIS app created. Files created by
    other apps / users return ``reason: "app_not_authorized"`` — the
    file belongs to its owner and only they can restore it.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _untrash_drive_file, "active")
    return _untrash_drive_file(creds, file_id)


# ---------------------------------------------------------------------
# 4. gdocs_trash_file
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Move a Drive file to trash",
    readonly=False, destructive=True, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
)
def gdocs_trash_file(creds, file_id: str | list[str]) -> dict:
    """Move a Drive file (Google Doc, .docx, anything) to trash.

    USE WHEN: you need to clean up an obsolete Drive file — a
    superseded conversion, a test doc, a broken output. ``gdocs_delete_tab``
    only removes a tab within a doc; this removes the whole document
    (or any other Drive file by ID).

    Uses ``files.update(trashed=True)``, NOT ``files.delete``. The file
    moves to Drive Trash and is recoverable for 30 days. Permanent
    deletion is intentionally not exposed.

    Idempotent: trashing an already-trashed file succeeds and the
    response flags ``was_already_trashed: true``.

    Args:
        file_id: A single Drive file ID (str) OR a list of IDs for
            batch trash. List form returns
            ``{results: [...], summary: {succeeded, skipped, failed}}``
            with one result per input — each item processed
            independently (one soft-failure does not abort the rest).

    Returns (single-ID mode):
        ``{"file_id", "name", "mimeType", "trashed": True,
        "was_already_trashed": bool}``. ``name`` lets the caller confirm
        the right file was touched.

    Choreography: pair with ``gdocs_untrash_file`` for recovery within
    Drive's 30-day trash window. file_id often comes from
    ``gdocs_find_doc_by_title`` or from a prior create call.

    NOTE: only works on files THIS app created. Files created by
    other apps / users return ``reason: "app_not_authorized"`` (HTTP
    403 appNotAuthorizedToFile) — the file belongs to its owner and
    only they can trash it. The agent has no recovery; surface to
    the user.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _trash_drive_file, "trashed")
    return _trash_drive_file(creds, file_id)


# ---------------------------------------------------------------------
# 5. gdocs_share_file (v2.3.0 — first new tool of the multi-service era)
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Grant a user access to a Google Drive file",
    readonly=False,
    destructive=False,
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GDOCS_SHARE_FILE_OUTPUT_SCHEMA,
)
def gdocs_share_file(
    creds,
    drive_file_id: str,
    email: str,
    role: str = "writer",
    notify: bool = True,
    message: str = "",
) -> dict:
    """Grant a user access to a Google Drive file (Doc, Sheet, Slide, .docx).

    USE WHEN: you just created or converted a doc for the user and
    they want to share it with a colleague / reviewer / external
    recipient. Or when the agent needs to programmatically grant
    access (e.g. a shared review folder, a course-distribution flow).

    Uses Drive's ``permissions.create`` REST endpoint. Roles map to
    Drive UI labels: ``"reader"`` = "Viewer", ``"writer"`` = "Editor"
    (DEFAULT), ``"commenter"`` = "Commenter".

    Args:
        drive_file_id: The file to share. From a prior create call,
            ``gdocs_find_doc_by_title``, or the user.
        email: Recipient's email address. Drive validates the address
            format and returns 400 on garbage input.
        role: Permission level — ``"reader"`` / ``"writer"`` (default)
            / ``"commenter"``. Other values rejected client-side.
        notify: When True (default), Drive sends a notification email
            to the recipient (Drive's standard "<owner> shared a doc
            with you" template). False suppresses the email; the
            permission still applies but the recipient has to learn
            of the share through another channel.
        message: Optional custom message included in the notification
            email. Ignored when ``notify=False``.

    Returns:
        ``{permission_id, role, granted_to, file_id}``. Record the
        ``permission_id`` if you might want to revoke the share later
        (a future ``gdocs_revoke_permission`` tool will accept it).

    Choreography: ``drive_file_id`` typically from a recent create
    call (``gdocs_make_tabbed_doc`` / ``gdocs_tab_existing_doc``) or
    from ``gdocs_find_doc_by_title``. Call ``gdocs_list_permissions``
    afterward to verify the share landed.

    NOTE: same app-ownership constraint as the trash / move tools —
    only works on files THIS app's ``drive.file`` scope can write to.
    Sharing a file the app didn't create returns HTTP 403
    ``appNotAuthorizedToFile``; the file's owner must grant access
    via the Drive UI instead.
    """
    return _grant_permission(
        creds,
        drive_file_id=drive_file_id,
        email=email,
        role=role,
        notify=notify,
        message=message,
    )


# ---------------------------------------------------------------------
# 6. gdocs_list_permissions (v2.3.0)
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="List who has access to a Google Drive file",
    # Pure read — no writes, no audit-log side-effects (unlike
    # find_doc_by_title's optional verify_writable probe). The CQRS
    # lesson from R33 audit Gap #3 / PR #107 is preserved by default.
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA,
)
def gdocs_list_permissions(creds, drive_file_id: str) -> dict:
    """List who has access to a Google Drive file — the share roster.

    USE WHEN: confirming a share landed (after ``gdocs_share_file``),
    auditing who can see / edit a sensitive doc, or before a
    teardown to enumerate revoke targets.

    Uses Drive's ``permissions.list`` REST endpoint. Returns the raw
    Drive shape per entry with the four most useful fields surfaced:
    ``id`` (the permission_id — input to a future revoke), ``role``
    (reader / writer / commenter / owner), ``type`` (user / group /
    domain / anyone), ``emailAddress`` (present for user / group;
    absent for domain / anyone shares).

    Args:
        drive_file_id: The file whose permissions to enumerate.

    Returns:
        ``{file_id, permissions: [{id, emailAddress, role, type}, ...]}``.
        ``permissions`` is empty when the file is private (only the
        owner can see it). The owner ALWAYS appears in the list with
        ``role="owner"``.

    Choreography: pair with ``gdocs_share_file`` for verify-after-grant.
    The ``permission_id`` on each entry is the handle a future
    ``gdocs_revoke_permission`` tool will accept.

    NOTE: same app-ownership constraint as the rest of the drive
    tools — ``drive.file`` scope limits visibility to files this app
    created. Files created by other apps return HTTP 403
    ``appNotAuthorizedToFile``; the file's owner must share / inspect
    via the Drive UI instead.
    """
    return _list_permissions(creds, drive_file_id)
