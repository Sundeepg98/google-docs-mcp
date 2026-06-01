"""Google Drive permissions / sharing operations.

Separate from ``api.py`` (file CRUD) per multi-service feasibility audit
(R33 agent ``a2d2492bbebb200a6``):

    "Drive permissions deserve a sub-module
    (``services/drive/sharing.py``) separate from file CRUD — the
    sharing model is a different mental domain."

Same service folder for consumer convenience (one ``services/drive/``
package); same shared dependencies (``google_clients.get_service`` —
the M2 chokepoint). The split is between distinct CONCEPTS (file
identity / lifecycle vs. permissioning), not distinct dependency
graphs — matching the ARCHITECTURE.md §5 "sub-module split when
single file would cross ~400 LOC or fold two distinct concepts" rule.

**Scope note.** The ``drive.file`` scope (already granted by
``auth.SCOPES``) permits creating and listing permissions on files
this app created. Sharing or inspecting a file the app did NOT
create returns HTTP 403 ``appNotAuthorizedToFile`` — identical to
trash / move / untrash's behavior. ``grant_permission`` and
``list_permissions`` let that 403 propagate as ``HttpError``; the
tool-layer wrappers in ``services/drive/tools.py`` translate it via
``_format_http_error`` (the standard envelope). No additional OAuth
grant is needed for v2.3.0 sharing.

**Soft-failure note.** Unlike ``trash_drive_file`` /
``untrash_drive_file`` / ``move_to_folder`` (which catch 403 +
``appNotAuthorizedToFile`` to support batch skip-and-continue), the
sharing functions deliberately do NOT do soft-failure handling here.
Sharing is a single-target operation (one file, one recipient per
call); there is no batch loop to protect. A 403 is genuinely a
caller error worth surfacing as an exception so the tool-layer
``_format_http_error`` can produce a structured Markdown response
through the standard envelope. If a future batch-sharing tool ever
ships, soft-failure shaping can be added at that point (third-consumer
rule).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from googleapiclient.errors import HttpError

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Drive's permissions API accepts these role literals on the request
# body. Pinned here so the tool-layer can validate caller input before
# the Drive round-trip (cheap rejection of typos like "editor").
_VALID_ROLES: frozenset[str] = frozenset({"reader", "writer", "commenter"})


def grant_permission(
    creds: Credentials,
    drive_file_id: str,
    email: str,
    role: str = "writer",
    notify: bool = True,
    message: str = "",
) -> dict:
    """Grant a user permission on a Drive file via ``permissions.create``.

    Args:
        creds: OAuth credentials carrying the drive.file scope.
        drive_file_id: The Drive file to share.
        email: Recipient's email address.
        role: One of ``"reader"`` / ``"writer"`` / ``"commenter"``.
            Defaults to ``"writer"`` (full edit access).
        notify: When True (default), Drive sends a notification email
            to the recipient. False suppresses the email — useful for
            programmatic shares where the caller surfaces the URL
            through some other channel.
        message: Optional custom message included in the notification
            email. Ignored when ``notify=False``. The Drive API
            requires this to be omitted (not empty string) when blank,
            so we map ``""`` to ``None`` before the call.

    Returns:
        ``{permission_id, role, granted_to, file_id}`` — a flat shape
        the agent can act on (e.g. record the permission_id for a
        later revoke). ``granted_to`` echoes the recipient email back
        for confirmation (Drive returns this in the response body).

    Raises:
        ValueError: ``role`` not in the allowed set, or ``email``
            empty/blank. Cheap rejection before the Drive round-trip.
        HttpError: any non-2xx from Drive (e.g. 403
            ``appNotAuthorizedToFile`` when the file wasn't created
            by this app, 404 file not found, 400 invalid email). The
            tool-layer envelope (``_format_http_error``) renders this
            as a structured response.
    """
    if role not in _VALID_ROLES:
        raise ValueError(
            f"role must be one of {sorted(_VALID_ROLES)}, got {role!r}. "
            "Drive's permissions API accepts only reader / writer / "
            "commenter for user-type permissions."
        )
    if not email or not email.strip():
        raise ValueError(
            "email cannot be empty — Drive requires a recipient "
            "address to grant a permission to."
        )

    drive = get_service("drive", "v3", credentials=creds)
    body = {
        "type": "user",
        "role": role,
        "emailAddress": email.strip(),
    }
    # ``emailMessage`` MUST be omitted (not empty) when blank — Drive
    # otherwise sends a notification with a literal empty body, which
    # surfaces as "" in the recipient's inbox.
    resp = drive.permissions().create(
        fileId=drive_file_id,
        body=body,
        sendNotificationEmail=notify,
        emailMessage=message or None,
        fields="id,emailAddress,role,type",
    ).execute()
    return {
        "permission_id": resp["id"],
        "role": resp["role"],
        "granted_to": resp.get("emailAddress", ""),
        "file_id": drive_file_id,
    }


def list_permissions(
    creds: Credentials,
    drive_file_id: str,
) -> dict:
    """List all permissions on a Drive file via ``permissions.list``.

    Args:
        creds: OAuth credentials carrying the drive.file scope.
        drive_file_id: The Drive file whose permissions to enumerate.

    Returns:
        ``{file_id, permissions: [{id, emailAddress, role, type}, ...]}``.
        Each permission is the raw Drive-API shape with the four most
        useful fields surfaced via the ``fields`` mask. Domain / group
        / anyone permissions may have ``emailAddress`` missing — the
        consumer should branch on ``type``.

    Raises:
        HttpError: any non-2xx from Drive (e.g. 403 when this app
            doesn't have read access to the file, 404 file not found).
            Tool-layer envelope renders this as a structured response.

    Note:
        ``drive.file`` scope limits the visible permission list to
        files this app created — Drive returns 403 when called against
        externally-owned files. This is intentional: the per-file
        scope is the entire point of drive.file vs. drive.full.
    """
    drive = get_service("drive", "v3", credentials=creds)
    # PR-Δ3.5: gdocs_list_permissions is readonly=True, idempotent=True.
    resp = execute_with_retry(
        lambda: drive.permissions().list(
            fileId=drive_file_id,
            fields="permissions(id,emailAddress,role,type)",
        ).execute(),
        idempotent=True,
        op_name="drive.permissions.list",
    )
    return {
        "file_id": drive_file_id,
        "permissions": resp.get("permissions", []),
    }


def revoke_permission(
    creds: Credentials,
    drive_file_id: str,
    permission_id: str,
) -> dict:
    """Revoke a permission on a Drive file via ``permissions.delete``.

    The inverse of ``grant_permission``. Removes a single permission
    (identified by ``permission_id`` — the ``id`` returned by
    ``grant_permission`` or surfaced per-entry by ``list_permissions``)
    from the file's access list. Drive's ``permissions.delete`` returns
    an empty body on success; this wraps that into a structured
    confirmation envelope.

    **Idempotent by design.** Revoking a permission that's already gone
    is the desired end state ("this grant no longer exists"), so a 404
    on the permission is returned as a soft *success* (``revoked: True``,
    ``was_already_absent: True``) rather than an error — a teardown loop
    can re-run safely. The matching tool wrapper is annotated
    ``idempotent=True``.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope.
        drive_file_id: The Drive file to revoke access on.
        permission_id: The permission to remove. From a prior
            ``grant_permission`` (its ``permission_id``) or any entry's
            ``id`` in ``list_permissions``.

    Returns:
        Success: ``{file_id, permission_id, revoked: True,
        was_already_absent: bool}``. ``was_already_absent`` is True when
        the permission (or its file) was already gone — the idempotent
        no-op case.
        Soft-failure: ``{file_id, permission_id, revoked: False, reason,
        message}`` where ``reason`` is ``"app_not_authorized"`` (403
        ``appNotAuthorizedToFile`` — the file wasn't created by this app,
        so ``drive.file`` can't modify its ACL) or ``"cannot_revoke"``
        (e.g. attempting to remove the sole owner, which Drive forbids
        with 403 and a non-``appNotAuthorizedToFile`` reason).

    Raises:
        ValueError: ``permission_id`` empty / blank. Cheap rejection
            before the Drive round-trip.
        HttpError: any non-2xx Drive does NOT classify above (e.g. a
            500) propagates so genuine bugs surface; the tool-layer
            ``_format_http_error`` renders it through the standard
            envelope.

    Note:
        Like the rest of the drive tools, ``drive.file`` scope limits
        this to files this app created. A 403 ``appNotAuthorizedToFile``
        is returned as soft-failure data (not raised) so a batch
        teardown can skip-and-continue, matching the trash / move
        contract.
    """
    if not permission_id or not permission_id.strip():
        raise ValueError(
            "permission_id cannot be empty — pass the id returned by "
            "gdocs_share_file or an entry's id from gdocs_list_permissions."
        )

    drive = get_service("drive", "v3", credentials=creds)
    try:
        # Idempotent=True: deleting an already-deleted permission is a
        # true no-op (Drive 404s, which we treat as soft success below).
        # Retrying a transient 429/5xx is therefore safe.
        execute_with_retry(
            lambda: drive.permissions().delete(
                fileId=drive_file_id,
                permissionId=permission_id.strip(),
            ).execute(),
            idempotent=True,
            op_name="drive.permissions.delete",
        )
    except HttpError as e:
        if e.status_code == 404:
            # The permission (or the file) is already gone. For a revoke,
            # that's the desired end state — report idempotent success.
            return {
                "file_id": drive_file_id,
                "permission_id": permission_id.strip(),
                "revoked": True,
                "was_already_absent": True,
            }
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "permission_id": permission_id.strip(),
                    "revoked": False,
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks access to modify this file's "
                        "permissions — it wasn't created by this app. "
                        "drive.file scope only permits ACL changes on "
                        "app-created files. The file's owner must revoke "
                        "via the Drive UI."
                    ),
                }
            # Other 403s: Drive refuses the deletion for a reason that
            # isn't app-authorization — most commonly trying to remove
            # the file's sole owner. Surface as a distinct soft-failure
            # so callers don't conflate it with the scope case.
            return {
                "file_id": drive_file_id,
                "permission_id": permission_id.strip(),
                "revoked": False,
                "reason": "cannot_revoke",
                "message": (
                    "Drive refused to delete this permission (HTTP 403). "
                    "The most common cause is attempting to remove the "
                    "file's owner — ownership can't be revoked this way. "
                    f"Drive reason(s): {reasons or 'unspecified'}."
                ),
            }
        raise

    return {
        "file_id": drive_file_id,
        "permission_id": permission_id.strip(),
        "revoked": True,
        "was_already_absent": False,
    }
