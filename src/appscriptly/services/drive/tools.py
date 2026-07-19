"""Google Drive MCP tool registrations (M3 Phase B — v2.1.4).

This module defines the ``@workspace_tool``-decorated tool functions for
the Drive file-management service. Importing this module triggers
registration with the live ``mcp`` instance — ``server.py`` performs
the import at the bottom of its module, AFTER constructing ``mcp``
and AFTER ``decorators.register(mcp, ...)`` wires the ``@workspace_tool``
decorator.

**Namespace cleanup (chore/tool-namespace-cleanup).** These tools act on
Drive, not Docs, so they were renamed off the historical ``gdocs_``
prefix to the honest ``gdrive_`` prefix. Every old ``gdocs_`` name stays
registered as a DEPRECATED ALIAS (dual-registration) so nothing breaks —
the same model PR-α used for ``gdocs_install_automation`` /
``gdocs_setup_apps_script``. The canonical ``gdrive_*`` body does the
work; each ``gdocs_*`` alias emits a ``DeprecationWarning`` (via
``appscriptly._deprecation.warn_deprecated_alias``) and delegates. Both
names are module-level attrs (the location witnesses require it) and both
register (the declared==registered witness requires it). Planned alias
removal: v3.0.

**Tools registered here** (11 drive-service tools — canonical name →
deprecated alias):

1. ``gdrive_find_doc_by_title``  (alias ``gdocs_find_doc_by_title``)   — look up a Google Doc / .docx by title
2. ``gdrive_move_to_folder``     (alias ``gdocs_move_to_folder``)      — move a file into a Drive folder
3. ``gdrive_untrash_file``       (alias ``gdocs_untrash_file``)        — restore a trashed file (single or batch)
4. ``gdrive_trash_file``         (alias ``gdocs_trash_file``)          — move a file to trash (single or batch)
5. ``gdrive_share_file``         (alias ``gdocs_share_file``)          — grant a user permission on a file (v2.3.0)
6. ``gdrive_list_permissions``   (alias ``gdocs_list_permissions``)    — list who has access to a file (v2.3.0)
7. ``gdrive_create_folder``      (alias ``gdocs_create_folder``)       — create a Drive folder (destination for move)
8. ``gdrive_revoke_permission``  (alias ``gdocs_revoke_permission``)   — revoke a previously-granted share
9. ``gdrive_export_file``        (alias ``gdocs_export_doc``)          — export a Google-native file to PDF/Office/etc.
10. ``gdrive_find_file``         (alias ``gdocs_find_file``)           — find app-accessible files of ANY type (filters)
11. ``gdrive_rename_file``       (NO alias; new in the 2026-07 wave)   — rename a file in place (files.update name)

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

import logging

from fastmcp.exceptions import ToolError

from appscriptly._deprecation import warn_deprecated_alias
from appscriptly.decorators import workspace_tool
from appscriptly.services.drive.api import (
    copy_drive_file as _copy_drive_file,
    create_folder as _create_folder,
    export_doc as _export_doc,
    find_doc_by_title as _find_doc_by_title,
    find_file as _find_file,
    move_to_folder as _move_to_folder,
    rename_file as _rename_file,
    trash_drive_file as _trash_drive_file,
    untrash_drive_file as _untrash_drive_file,
)
from appscriptly.services.drive.sharing import (
    _VALID_ROLES,
    grant_permission as _grant_permission,
    list_permissions as _list_permissions,
    revoke_permission as _revoke_permission,
)
from appscriptly.tool_schemas import (
    GDOCS_CREATE_FOLDER_OUTPUT_SCHEMA,
    GDOCS_EXPORT_DOC_OUTPUT_SCHEMA,
    GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
    GDOCS_FIND_FILE_OUTPUT_SCHEMA,
    GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA,
    GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
    GDOCS_REVOKE_PERMISSION_OUTPUT_SCHEMA,
    GDOCS_SHARE_FILE_OUTPUT_SCHEMA,
    GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
    GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
    GDRIVE_COPY_FILE_OUTPUT_SCHEMA,
    GDRIVE_RENAME_FILE_OUTPUT_SCHEMA,
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


_logger = logging.getLogger(__name__)


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
            # Return a bounded message (guards against a 500+ char Google
            # API body bloating every per-item result in a large batch),
            # but log the FULL exception at debug so the operator doesn't
            # lose the tail when diagnosing a failure.
            _logger.debug(
                "drive batch item %s failed: %s", fid, e, exc_info=True
            )
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
def gdrive_find_doc_by_title(
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


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_find_doc_by_title",
    readonly=True, destructive=False, idempotent=True, external=True,
    # creds=True: delegates to the same api-layer function the canonical
    # uses (the decorator injects creds here too). Delegating to the
    # canonical's creds-stripped wrapper instead would mismatch the
    # static signature; calling the api function directly keeps the alias
    # honest to pyright and replicates only the thin guard clause.
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
    """DEPRECATED — use ``gdrive_find_doc_by_title`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_find_doc_by_title", "gdrive_find_doc_by_title")
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
def gdrive_move_to_folder(creds, file_id: str, folder_id: str) -> dict:
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


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_move_to_folder",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
)
def gdocs_move_to_folder(creds, file_id: str, folder_id: str) -> dict:
    """DEPRECATED — use ``gdrive_move_to_folder`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_move_to_folder", "gdrive_move_to_folder")
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
def gdrive_untrash_file(creds, file_id: str | list[str]) -> dict:
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


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_untrash_file",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
)
def gdocs_untrash_file(creds, file_id: str | list[str]) -> dict:
    """DEPRECATED — use ``gdrive_untrash_file`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_untrash_file", "gdrive_untrash_file")
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
def gdrive_trash_file(creds, file_id: str | list[str]) -> dict:
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


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_trash_file",
    readonly=False, destructive=True, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
)
def gdocs_trash_file(creds, file_id: str | list[str]) -> dict:
    """DEPRECATED — use ``gdrive_trash_file`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_trash_file", "gdrive_trash_file")
    if isinstance(file_id, list):
        return _run_batch(file_id, _trash_drive_file, "trashed")
    return _trash_drive_file(creds, file_id)


# ---------------------------------------------------------------------
# gdrive_rename_file (BUG 2b, 2026-07-10) — canonical only, NO alias
# (the tool never existed under the legacy gdocs_ prefix).
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Rename a Drive file",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDRIVE_RENAME_FILE_OUTPUT_SCHEMA,
)
def gdrive_rename_file(creds, file_id: str, new_name: str) -> dict:
    """Rename a Drive file (Google Doc, .docx, folder, anything) in place.

    USE WHEN: a file carries the wrong name - most commonly a doc left
    behind by an interrupted convert under its temporary import name
    (e.g. "tmpjgehtmo2") - and you want to fix the name WITHOUT
    copying, moving, or re-creating anything.

    Uses ``files.update`` on the ``name`` field only. Metadata-only
    PATCH: the file id, content, comments, sharing, and folder location
    are untouched, so existing links keep working. Same ``drive.file``
    scope surface as ``gdrive_trash_file`` - no additional permission.

    Idempotent: renaming to the name the file already has succeeds and
    simply reports ``previous_name`` equal to ``name``.

    Args:
        file_id: The Drive file ID (from a create call,
            ``gdrive_find_file``, or ``gdrive_find_doc_by_title``).
        new_name: The full new name. For non-Google-native files keep
            the extension in the name (Drive does not add one).

    Returns:
        Success: ``{"file_id", "name", "previous_name", "mimeType"}``.
        Soft-failure (returned as data, not raised): ``{"file_id",
        "reason", "message"}`` with ``reason`` in {``"not_found"``,
        ``"app_not_authorized"``}.

    Choreography: pair with ``gdrive_find_doc_by_title`` /
    ``gdrive_find_file`` to locate stray temp-named files. To relocate
    a file use ``gdrive_move_to_folder``; to remove it use
    ``gdrive_trash_file``.

    NOTE: only works on files THIS app created (``drive.file`` scope).
    Files created by other apps / users return
    ``reason: "app_not_authorized"``.
    """
    return _rename_file(creds, file_id, new_name)


# ---------------------------------------------------------------------
# gdrive_copy_file - canonical only, NO alias (new tool, never had a
# legacy gdocs_ prefix). The template-fill flow enabler.
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Copy a Drive file",
    # Creates a copy (adds state, touches nothing existing) so it is a
    # write, not destructive. NOT idempotent: each call makes another copy.
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDRIVE_COPY_FILE_OUTPUT_SCHEMA,
)
def gdrive_copy_file(creds, file_id: str, title: str | None = None) -> dict:
    """Copy a Drive file (Google Doc, Sheet, Slides, .docx, anything).

    USE WHEN: you want a working COPY of an existing file. The headline
    case is template fill: copy a Google Doc template that already
    carries pre-authored named ranges, then fill the copy with
    ``gdocs_replace_named_range_content`` - the original template is
    never modified.

    Uses Drive ``files.copy``. The copy inherits your ownership; same
    ``drive.file`` scope as ``gdrive_rename_file``, no extra permission.
    Only files THIS app created / can access are copyable.

    Args:
        file_id: The Drive file ID to copy (from a create call,
            ``gdrive_find_file``, or ``gdrive_find_doc_by_title``).
        title: Optional name for the copy. Omit to let Drive name it
            ``"Copy of <original>"``.

    Returns:
        ``{file_id, name, url}`` for the NEW file: its id, name, and
        open-in-browser URL (Drive's ``webViewLink``).

    Choreography: ``gdrive_copy_file`` (copy a pre-marked template) ->
    ``gdocs_replace_named_range_content`` (fill each field in the copy)
    -> ``gdocs_read_doc`` (verify). To rename in place instead use
    ``gdrive_rename_file``; to remove a file use ``gdrive_trash_file``.

    NOTE: requires the base Google OAuth grant like every tool; works
    only on files this app can access (``drive.file`` scope).
    """
    try:
        return _copy_drive_file(creds, file_id, title)
    except ValueError as e:
        raise ToolError(str(e)) from e


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
def gdrive_share_file(
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
        (pass it to ``gdocs_revoke_permission`` to revoke the share).

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
    # Fail-fast at the tool boundary (matching gdocs_set_tab_icons):
    # reject a bad role / blank email as a structured ToolError BEFORE
    # delegating, rather than letting sharing.grant_permission's bare
    # ValueError surface. ``_VALID_ROLES`` is the single source of truth
    # in sharing.py; the delegate re-checks (defense in depth).
    if role not in _VALID_ROLES:
        raise ToolError(
            f"role must be one of {sorted(_VALID_ROLES)}, got {role!r}. "
            "Drive accepts only reader / writer / commenter for "
            "user-type permissions ('editor' is a UI label, not an API "
            "role literal)."
        )
    try:
        return _grant_permission(
            creds,
            drive_file_id=drive_file_id,
            email=email,
            role=role,
            notify=notify,
            message=message,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_share_file",
    readonly=False, destructive=False, idempotent=False, external=True,
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
    """DEPRECATED — use ``gdrive_share_file`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_share_file", "gdrive_share_file")
    if role not in _VALID_ROLES:
        raise ToolError(
            f"role must be one of {sorted(_VALID_ROLES)}, got {role!r}. "
            "Drive accepts only reader / writer / commenter for "
            "user-type permissions ('editor' is a UI label, not an API "
            "role literal)."
        )
    try:
        return _grant_permission(
            creds,
            drive_file_id=drive_file_id,
            email=email,
            role=role,
            notify=notify,
            message=message,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


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
def gdrive_list_permissions(creds, drive_file_id: str) -> dict:
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
    The ``permission_id`` on each entry is the handle
    ``gdocs_revoke_permission`` accepts to revoke that share.

    NOTE: same app-ownership constraint as the rest of the drive
    tools — ``drive.file`` scope limits visibility to files this app
    created. Files created by other apps return HTTP 403
    ``appNotAuthorizedToFile``; the file's owner must share / inspect
    via the Drive UI instead.
    """
    return _list_permissions(creds, drive_file_id)


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_list_permissions",
    readonly=True, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA,
)
def gdocs_list_permissions(creds, drive_file_id: str) -> dict:
    """DEPRECATED — use ``gdrive_list_permissions`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_list_permissions", "gdrive_list_permissions")
    return _list_permissions(creds, drive_file_id)


# ---------------------------------------------------------------------
# 7. gdocs_create_folder — create a Drive folder (files.create, folder mime)
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Create a Google Drive folder",
    # Creates new Drive state. Not a read; not destructive (nothing is
    # removed). NOT idempotent — files.create makes a fresh folder each
    # call (Drive permits duplicate names), matching
    # gslides_create_presentation / gsheets_create_spreadsheet.
    readonly=False,
    destructive=False,
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GDOCS_CREATE_FOLDER_OUTPUT_SCHEMA,
)
def gdrive_create_folder(
    creds,
    name: str,
    parent_folder_id: str | None = None,
) -> dict:
    """Create a Google Drive folder — a destination to file documents into.

    USE WHEN: you want to organize output before (or after) creating
    docs — e.g. make a "Q3 Onboarding" folder, then file new docs into
    it with ``gdocs_move_to_folder``. ``gdocs_move_to_folder`` only
    moves into an EXISTING folder; this is how you make a new one.

    Uses Drive's ``files.create`` with
    ``mimeType="application/vnd.google-apps.folder"`` — a Drive folder
    is just a file whose mimeType is the folder type. The folder is
    owned by the OAuth user and, because THIS app created it, is fully
    writable under the ``drive.file`` scope — so docs this app creates
    can be filed into it without any broader Drive permission.

    Args:
        name: The folder's display name. Empty / whitespace is rejected
            (Drive would otherwise create a folder literally titled
            "Untitled folder").
        parent_folder_id: Optional parent folder Drive ID. When given,
            the new folder is nested INSIDE it. When omitted (default),
            the folder is created in Drive root (My Drive).

            NOTE: the parent must itself be a folder THIS app can write
            to (one it created). Nesting under a folder the app didn't
            create returns HTTP 403 ``appNotAuthorizedToFile`` — same
            ``drive.file`` constraint as the rest of the drive tools.

    Returns:
        ``{folder_id, name, url, parent_folder_id}``. ``folder_id``
        feeds straight into ``gdocs_move_to_folder`` (as ``folder_id``)
        to file documents into the new folder; ``url`` deep-links to
        the folder in the Drive UI; ``parent_folder_id`` echoes the
        parent back (``None`` when created in root).

    Choreography: typically the FIRST step of an organize flow — create
    the folder here, then ``gdocs_move_to_folder`` each doc into the
    returned ``folder_id``. Pairs with ``gdocs_make_tabbed_doc`` /
    ``gdocs_tab_existing_doc`` (which land docs in root by default).

    NOTE: NOT idempotent — calling twice creates two folders with the
    same name (Drive keys folders by ID, not name). Track the returned
    ``folder_id`` rather than re-creating by name.
    """
    return _create_folder(creds, name=name, parent_folder_id=parent_folder_id)


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_create_folder",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDOCS_CREATE_FOLDER_OUTPUT_SCHEMA,
)
def gdocs_create_folder(
    creds,
    name: str,
    parent_folder_id: str | None = None,
) -> dict:
    """DEPRECATED — use ``gdrive_create_folder`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_create_folder", "gdrive_create_folder")
    return _create_folder(creds, name=name, parent_folder_id=parent_folder_id)


# ---------------------------------------------------------------------
# 8. gdocs_revoke_permission — revoke a share (permissions.delete)
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Revoke a user's access to a Google Drive file",
    # Removes access — destructive (the inverse of gdocs_share_file).
    # Idempotent: revoking an already-gone permission is the desired
    # end state, so the api layer returns a 404 as soft success
    # (was_already_absent=True) — re-running a teardown is safe.
    readonly=False,
    destructive=True,
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GDOCS_REVOKE_PERMISSION_OUTPUT_SCHEMA,
)
def gdrive_revoke_permission(
    creds,
    drive_file_id: str,
    permission_id: str,
) -> dict:
    """Revoke a user's access to a Google Drive file — the inverse of sharing.

    USE WHEN: tearing down a share you (or the agent) granted earlier —
    e.g. a reviewer is done, a temporary collaborator should lose
    access, or a teardown flow is cleaning up. Strengthens the "the
    user controls who has access" story: every grant via
    ``gdocs_share_file`` is reversible here.

    Uses Drive's ``permissions.delete`` REST endpoint. The
    ``permission_id`` is the handle returned by ``gdocs_share_file``
    (its ``permission_id``) or surfaced per-entry by
    ``gdocs_list_permissions`` (each entry's ``id``).

    Idempotent by design: revoking a permission that's already gone is
    the desired end state, so a 404 is returned as a soft SUCCESS
    (``revoked: True``, ``was_already_absent: True``) — a teardown loop
    can re-run safely.

    Args:
        drive_file_id: The file to revoke access on.
        permission_id: The permission to remove. From a prior
            ``gdocs_share_file`` (its ``permission_id``) or any entry's
            ``id`` from ``gdocs_list_permissions``. Empty / blank is
            rejected before the Drive round-trip.

    Returns:
        Success: ``{file_id, permission_id, revoked: True,
        was_already_absent: bool}`` — ``was_already_absent=True`` is the
        idempotent no-op case (the permission was already gone).
        Soft-failure (returned as data, not raised): ``{file_id,
        permission_id, revoked: False, reason, message}`` where
        ``reason`` is ``"app_not_authorized"`` (the file wasn't created
        by this app, so ``drive.file`` can't modify its ACL) or
        ``"cannot_revoke"`` (Drive refused — most commonly an attempt to
        remove the file's sole owner, which Drive forbids).

    Choreography: ``permission_id`` typically comes from a recent
    ``gdocs_share_file`` call or from ``gdocs_list_permissions`` (call
    it first to enumerate revoke targets). Call ``gdocs_list_permissions``
    afterward to confirm the entry is gone.

    NOTE: same app-ownership constraint as ``gdocs_share_file`` —
    ``drive.file`` scope only permits ACL changes on files this app
    created. Revoking on a file the app didn't create returns
    ``reason: "app_not_authorized"``; the file's owner must revoke via
    the Drive UI instead.
    """
    return _revoke_permission(
        creds,
        drive_file_id=drive_file_id,
        permission_id=permission_id,
    )


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_revoke_permission",
    readonly=False, destructive=True, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_REVOKE_PERMISSION_OUTPUT_SCHEMA,
)
def gdocs_revoke_permission(
    creds,
    drive_file_id: str,
    permission_id: str,
) -> dict:
    """DEPRECATED — use ``gdrive_revoke_permission`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on Drive,
    not Docs). Behavior is identical; the old name stays registered as
    an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_revoke_permission", "gdrive_revoke_permission")
    return _revoke_permission(
        creds,
        drive_file_id=drive_file_id,
        permission_id=permission_id,
    )


# ---------------------------------------------------------------------
# 9. gdocs_export_doc — export a Google-native file (files.export)
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Export a Google Doc/Sheet/Slides to PDF, Office, or other formats",
    # Creates a NEW Drive file (the exported artifact) — so not a pure
    # read; not destructive (the source is untouched, nothing removed).
    # NOT idempotent — each call uploads a fresh exported file (Drive
    # permits duplicate names), matching gdocs_create_folder.
    readonly=False,
    destructive=False,
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GDOCS_EXPORT_DOC_OUTPUT_SCHEMA,
)
def gdrive_export_file(
    creds,
    drive_file_id: str,
    export_format: str,
    output_name: str | None = None,
) -> dict:
    """Export a Google Doc / Sheet / Slides / Drawing to PDF, Office, etc.

    USE WHEN: the user wants a downloadable / portable copy of a
    Google-native file — "give me this doc as a PDF", "export the sheet
    to xlsx", "I need a .pptx of these slides". The symmetric inverse of
    the import tools (which bring a ``.docx`` IN and convert to a Doc);
    this renders a Google-native file OUT.

    Uses Drive's ``files.export``. The exported bytes are uploaded back
    to Drive as a NEW standalone file (a real ``.pdf`` / ``.docx`` /
    ``.xlsx`` — NOT a Google-native editor doc), and this returns its ID
    + a ``download_url`` (Drive's direct-download ``webContentLink``).
    Why store-in-Drive rather than return raw bytes: an MCP tool returns
    a JSON envelope, not a binary stream — so the artifact lands in
    Drive (the same pattern ``as_encode_video`` uses for its MP4) where
    the user can download it, and the agent can ``gdocs_share_file`` /
    ``gdocs_move_to_folder`` it. The source file is never modified.

    Args:
        drive_file_id: The Google-native file to export (a Doc / Sheet /
            Slides / Drawing this app created or opened). From a prior
            create call or ``gdocs_find_doc_by_title``. A binary blob
            already on Drive (.pdf, .png, an uploaded .docx) has no
            editor representation and returns ``reason: "not_exportable"``.
        export_format: Target format token (case-insensitive). By source:
            • **Doc** → ``pdf docx odt rtf txt html epub``
            • **Sheet** → ``pdf xlsx ods csv tsv html``
            • **Slides** → ``pdf pptx odp txt``
            • **Drawing** → ``pdf png svg``
            An unrecognized token, or a valid token that's wrong for the
            source type (e.g. ``xlsx`` on a Doc), is rejected before the
            Drive round-trip.
        output_name: Optional name for the exported Drive file. Omitted →
            the source's name with the format extension appended (e.g.
            ``"Q3 Plan"`` → ``"Q3 Plan.pdf"``). The right extension is
            added if you don't include it.

    Returns:
        Success: ``{source_file_id, source_mime_type, export_format,
        export_mime_type, exported_file_id, name, url, download_url,
        size_bytes}``. ``url`` opens the new file in Drive;
        ``download_url`` is the direct-download link (may be ``null`` if
        Drive omits it); ``size_bytes`` is the exported size (``null`` if
        unreported).
        Soft-failure (returned as data, not raised): ``{source_file_id,
        reason, message}`` — ``reason`` is ``"not_found"``,
        ``"app_not_authorized"`` (source not accessible under
        ``drive.file``), or ``"not_exportable"`` (not a Google-native
        editor file).

    Choreography: ``drive_file_id`` from a recent create
    (``gdocs_make_tabbed_doc`` / ``gsheets_create_spreadsheet`` /
    ``gslides_create_presentation``) or ``gdocs_find_doc_by_title``.
    Pair the returned ``exported_file_id`` with ``gdocs_share_file`` to
    send the PDF to someone, or ``gdocs_move_to_folder`` to file it.

    NOTE: ``files.export`` is capped by Drive at ~10 MB of exported
    content. Same ``drive.file`` ownership model as the rest of the
    drive tools — only files THIS app can access are exportable; a file
    the app didn't create/open returns ``reason: "app_not_authorized"``
    (the owner can export via the Drive UI: File → Download).

    AUTH LIMITATION (S2.4): ``download_url`` is Drive's
    ``webContentLink`` - it only serves the bytes to a browser SIGNED
    IN to a Google account that can read the file. An unauthenticated
    client (curl, a sandbox, any server-side fetch without the user's
    cookies) receives a Google sign-in HTML page instead of the file.
    Treat ``download_url`` as a link to hand to the USER, never as a
    machine-fetchable URL. For programmatic access to the CONTENT:
    use ``gdocs_read_doc`` to read a Doc's text directly, or
    ``gdrive_share_file`` (role "reader") so a signed-in account can
    fetch it.
    """
    return _export_doc(
        creds,
        drive_file_id=drive_file_id,
        export_format=export_format,
        output_name=output_name,
    )


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_export_file",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDOCS_EXPORT_DOC_OUTPUT_SCHEMA,
)
def gdocs_export_doc(
    creds,
    drive_file_id: str,
    export_format: str,
    output_name: str | None = None,
) -> dict:
    """DEPRECATED — use ``gdrive_export_file`` instead.

    Renamed off the historical ``gdocs_`` prefix (this acts on any Drive
    file, not just Docs) and given a clearer name. Behavior is identical;
    the old name stays registered as an alias and is slated for removal
    in v3.0.
    """
    warn_deprecated_alias("gdocs_export_doc", "gdrive_export_file")
    return _export_doc(
        creds,
        drive_file_id=drive_file_id,
        export_format=export_format,
        output_name=output_name,
    )


# ---------------------------------------------------------------------
# 10. gdocs_find_file — generalized search over app-accessible files
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="Find Drive files of any type (name / content / type / folder filters)",
    # Pure read by default — files.list. The optional verify_writable
    # probe is opt-in (default False) so the readonly contract holds for
    # the default call, matching gdocs_find_doc_by_title (CQRS / R33 #3).
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GDOCS_FIND_FILE_OUTPUT_SCHEMA,
)
def gdrive_find_file(
    creds,
    query: str = "",
    mime_type: str | None = None,
    full_text: str | None = None,
    parent_folder_id: str | None = None,
    exact: bool = False,
    include_trashed: bool = False,
    verify_writable: bool = False,
) -> dict:
    """Find Drive files of ANY type — Sheets, Slides, PDFs, folders, Docs.

    USE WHEN: you need a file_id and ``gdocs_find_doc_by_title`` is too
    narrow — it only finds Google Docs / .docx. This finds files of any
    type with optional filters: by ``mime_type`` ("my Sheets"), by
    ``full_text`` content ("the doc mentioning 'Q3 revenue'"), by
    ``parent_folder_id`` ("what's in this folder"), and/or by ``query``
    name. With no filters it lists your most-recent app-accessible files.

    **IMPORTANT — searches files this app has CREATED or OPENED, NOT the
    whole Drive.** Under the ``drive.file`` scope, ``files.list`` only
    sees the per-file set this app was granted access to (files it
    created, or that the user explicitly opened with it). Arbitrary files
    elsewhere in the user's Drive that this app never touched will NOT
    appear, regardless of filters — this is a per-file-scope search, not
    a Drive-wide one. (Whole-Drive discovery needs a broader, restricted
    Drive scope this app intentionally does not request.) For re-finding
    files the app itself produced, that's exactly the right surface.

    Args:
        query: Optional name text. Empty (default) = no name filter —
            use ``mime_type`` and/or ``parent_folder_id`` alone to browse.
        mime_type: Optional EXACT Drive mimeType. Common values:
            • Sheets — ``application/vnd.google-apps.spreadsheet``
            • Slides — ``application/vnd.google-apps.presentation``
            • Docs   — ``application/vnd.google-apps.document``
            • Folders— ``application/vnd.google-apps.folder``
            • PDF    — ``application/pdf``
            Omit to match all types. No wildcards (Drive matches exactly).
        full_text: Optional content-contains filter (Drive's ``fullText``
            index — searches inside the file, not just its name).
        parent_folder_id: Optional folder ID to scope results to (only
            files inside that folder). Combine with ``mime_type`` for
            e.g. "Sheets in this folder". Folder IDs come from
            ``gdocs_create_folder`` or a prior ``gdocs_find_file`` with
            ``mime_type`` = the folder type.
        exact: With a ``query``, True = exact name match, False (default)
            = substring. Ignored when ``query`` is empty.
        include_trashed: False (default) excludes trashed files.
        verify_writable: False (default) — pure read; ``owned_by_app`` is
            ``None``. True opts into a per-match no-op-write PROBE that
            reports whether this app can actually WRITE each file (the
            same check ``gdocs_trash_file`` / ``gdocs_move_to_folder``
            run). Cost: one batched HTTP request + a Drive audit-log
            entry per match. Default is False so this readonly tool stays
            a pure read.

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time, trashed,
        owned_by_app}, ...], "count": int}`` — newest-first by
        modified_time. SAME shape as ``gdocs_find_doc_by_title``, so the
        two are interchangeable downstream. ``owned_by_app`` is
        ``True``/``False`` if probed, else ``None``.

    Choreography: the returned ``file_id`` feeds ``gdocs_export_doc``,
    ``gdocs_share_file``, ``gdocs_move_to_folder``, ``gdocs_trash_file``,
    and the Sheets/Slides tools. Use ``gdocs_find_doc_by_title`` instead
    when you specifically want Docs/.docx; use this for everything else.

    NOTE: same ``drive.file`` corpus limit as the rest of the drive
    tools — results are confined to app-accessible files.
    """
    return _find_file(
        creds,
        query,
        mime_type=mime_type,
        full_text=full_text,
        parent_folder_id=parent_folder_id,
        exact=exact,
        include_trashed=include_trashed,
        verify_writable=verify_writable,
    )


# ---------------------------------------------------------------------
# Deprecated alias — gdocs_find_file → gdrive_find_file
# ---------------------------------------------------------------------


@workspace_tool(
    service="drive",
    title="DEPRECATED alias of gdrive_find_file",
    readonly=True, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_FIND_FILE_OUTPUT_SCHEMA,
)
def gdocs_find_file(
    creds,
    query: str = "",
    mime_type: str | None = None,
    full_text: str | None = None,
    parent_folder_id: str | None = None,
    exact: bool = False,
    include_trashed: bool = False,
    verify_writable: bool = False,
) -> dict:
    """DEPRECATED — use ``gdrive_find_file`` instead.

    Renamed off the historical ``gdocs_`` prefix (this finds files of any
    type, not just Docs). Behavior is identical; the old name stays
    registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_find_file", "gdrive_find_file")
    return _find_file(
        creds,
        query,
        mime_type=mime_type,
        full_text=full_text,
        parent_folder_id=parent_folder_id,
        exact=exact,
        include_trashed=include_trashed,
        verify_writable=verify_writable,
    )
