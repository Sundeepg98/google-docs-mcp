"""Co-located tests for services/drive/sharing.py (v2.3.0).

Mirrors the canonical pattern from ``tests/unit/services/drive/test_api.py``:
exercise the module via ``with_google_api_client(InMemoryGoogleAPIClient)``
so the real ``get_service`` chokepoint runs but Drive's HTTP boundary
is stubbed. No real OAuth, no real Drive round-trip.

Tests split across two surfaces:

1. **Pre-API validation** (pure-function branches that raise BEFORE
   the Drive round-trip) — ``role`` allowlist, empty ``email``.
   Cheap to test, catches typo regressions instantly.
2. **Drive call shape** — ``permissions.create`` receives the right
   body / fields / sendNotificationEmail / emailMessage; the
   ``""`` → ``None`` mapping for blank messages is preserved (Drive
   would otherwise mail an empty-body notification).
3. **Response shape** — the flat ``{permission_id, role, granted_to,
   file_id}`` envelope the tool layer surfaces is built correctly
   from the raw Drive response.
4. **List shape** — empty response (private file) returns
   ``{file_id, permissions: []}`` rather than missing the key.

The empirical-validation framing of v2.3.0: this file is the proof
that the M2 chokepoint + per-service folder pattern delivers a clean
"bolt-on a new module + its tests in one PR" outcome. If anything
here required architectural rework, the foundation cost wasn't
recovered. Spoiler: it didn't.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from googleapiclient.errors import HttpError

from appscriptly.services.drive.sharing import (
    _VALID_ROLES,
    grant_permission,
    list_permissions,
    revoke_permission,
)


def _mock_http_error(status_code: int, reason_code: str = "") -> HttpError:
    """Build a fake HttpError with the structure googleapiclient produces.

    Mirror of the helper in ``tests/unit/test_soft_failure_contracts.py``
    — kept local so the sharing tests don't reach across into another
    test module's private helper. ``error_details`` is populated directly
    because that's the attribute ``revoke_permission`` inspects to
    classify ``appNotAuthorizedToFile`` vs other 403s.
    """
    resp = MagicMock()
    resp.status = status_code
    resp.reason = "Forbidden" if status_code == 403 else "Not Found"
    content = (
        f'{{"error":{{"code":{status_code},"errors":'
        f'[{{"reason":"{reason_code}","message":"mocked"}}]}}}}'
    ).encode("utf-8")
    err = HttpError(resp, content)
    err.error_details = [{"reason": reason_code, "message": "mocked"}]
    return err


# ---------------------------------------------------------------------
# Module-level pinning — public surface canaries
# ---------------------------------------------------------------------


def test_valid_roles_is_the_drive_documented_three():
    """Drive's permissions API accepts only ``reader`` / ``writer`` /
    ``commenter`` for ``type=user`` permissions. Pinning the set here
    catches a stray edit (e.g. adding ``"editor"`` — a UI label that
    is NOT a valid role literal at the API)."""
    assert _VALID_ROLES == frozenset({"reader", "writer", "commenter"})


# ---------------------------------------------------------------------
# grant_permission — pre-API validation (no Drive call required)
# ---------------------------------------------------------------------


def test_grant_permission_rejects_invalid_role():
    """Garbage role rejected client-side BEFORE the Drive round-trip —
    spares the user a 400 from the Drive API and keeps the error
    surface honest about what's allowed."""
    with pytest.raises(ValueError, match="role must be one of"):
        grant_permission(MagicMock(), "FILE1", "user@example.com", role="editor")


def test_grant_permission_rejects_blank_email():
    """An empty / whitespace email is a caller bug, not a Drive
    rejection — surface as ValueError with explanation."""
    with pytest.raises(ValueError, match="email cannot be empty"):
        grant_permission(MagicMock(), "FILE1", "   ", role="writer")
    with pytest.raises(ValueError, match="email cannot be empty"):
        grant_permission(MagicMock(), "FILE1", "", role="writer")


def test_grant_permission_accepts_all_three_documented_roles():
    """All three role literals (reader / writer / commenter) must be
    accepted without raising. The Drive call is stubbed so we only
    exercise the pre-API validation pass."""
    drive = MagicMock(name="drive-stub")
    drive.permissions().create().execute.return_value = {
        "id": "perm-1", "emailAddress": "u@e.com",
        "role": "reader", "type": "user",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        for role in ("reader", "writer", "commenter"):
            drive.permissions().create().execute.return_value["role"] = role
            result = grant_permission(MagicMock(), "FILE1", "u@e.com", role=role)
            assert result["role"] == role


# ---------------------------------------------------------------------
# grant_permission — Drive call shape
# ---------------------------------------------------------------------


@pytest.fixture
def stub_drive_for_grant():
    """A Drive Resource stub whose permissions().create().execute()
    returns a plausible Drive response. Enough to let grant_permission
    complete and let us inspect the call args it passed."""
    drive = MagicMock(name="drive-v3-stub-grant")
    drive.permissions().create().execute.return_value = {
        "id": "PERM-XYZ",
        "emailAddress": "recipient@example.com",
        "role": "writer",
        "type": "user",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def _last_create_kwargs(drive: MagicMock) -> dict:
    """The kwargs of the most recent permissions().create(...) call
    that actually carried a ``fileId`` (filters out the bare ``()`` lookup
    MagicMock uses to build the chain). Mirrors the helper in
    test_api.py's ``_last_q_passed_to_list``."""
    for call in reversed(drive.permissions().create.call_args_list):
        if "fileId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no permissions().create() call captured fileId")


def test_grant_permission_passes_fileId_to_drive(stub_drive_for_grant):
    """The Drive call must target the file_id the caller passed."""
    grant_permission(
        MagicMock(), "FILE-ABC", "u@e.com", role="writer",
    )
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["fileId"] == "FILE-ABC"


def test_grant_permission_builds_user_type_body_with_role_and_email(
    stub_drive_for_grant,
):
    """The request body must be ``{type: user, role, emailAddress}``.
    ``type=user`` is implicit in this tool (the docstring promises
    user-grant semantics); groups / domains / anyone would need
    separate tools."""
    grant_permission(
        MagicMock(), "FILE1", "alice@example.com", role="commenter",
    )
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["body"] == {
        "type": "user",
        "role": "commenter",
        "emailAddress": "alice@example.com",
    }


def test_grant_permission_strips_whitespace_from_email(stub_drive_for_grant):
    """Leading / trailing whitespace on email gets stripped before the
    Drive call — Drive validates the literal exactly and rejects
    ``" alice@example.com "`` as malformed."""
    grant_permission(
        MagicMock(), "FILE1", "  alice@example.com  ", role="writer",
    )
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["body"]["emailAddress"] == "alice@example.com"


def test_grant_permission_default_notify_true_sends_email(stub_drive_for_grant):
    """Default ``notify=True`` → ``sendNotificationEmail=True``. Drive's
    default is also True; passing it explicitly preserves intent across
    SDK upgrades."""
    grant_permission(MagicMock(), "FILE1", "u@e.com")
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["sendNotificationEmail"] is True


def test_grant_permission_notify_false_suppresses_email(stub_drive_for_grant):
    """``notify=False`` → ``sendNotificationEmail=False``. Use case:
    programmatic shares where the URL is surfaced through another
    channel (Slack DM, in-app notification, etc.)."""
    grant_permission(
        MagicMock(), "FILE1", "u@e.com", notify=False,
    )
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["sendNotificationEmail"] is False


def test_grant_permission_blank_message_maps_to_None_not_empty_string(
    stub_drive_for_grant,
):
    """SUBTLE BUG GUARD: Drive's ``emailMessage`` must be omitted (or
    None) when blank — passing ``""`` makes Drive send a notification
    with a literal empty body, which surfaces as a blank message in
    the recipient's inbox. The implementation maps ``""`` → ``None``."""
    grant_permission(
        MagicMock(), "FILE1", "u@e.com", message="",
    )
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["emailMessage"] is None


def test_grant_permission_passes_custom_message_through(stub_drive_for_grant):
    """A non-empty ``message`` reaches Drive verbatim, included in the
    notification email body."""
    grant_permission(
        MagicMock(), "FILE1", "u@e.com",
        message="Hey, here's the project doc — let me know if you need access.",
    )
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["emailMessage"].startswith("Hey, here's the project doc")


def test_grant_permission_requests_minimal_fields_mask(stub_drive_for_grant):
    """The ``fields`` mask limits the Drive response to what the
    consumer needs (``id``, ``emailAddress``, ``role``, ``type``).
    Drive returns a much larger object by default; the mask cuts
    payload size and avoids leaking unused fields to MCP callers."""
    grant_permission(MagicMock(), "FILE1", "u@e.com")
    kw = _last_create_kwargs(stub_drive_for_grant)
    assert kw["fields"] == "id,emailAddress,role,type"


# ---------------------------------------------------------------------
# grant_permission — response envelope shape
# ---------------------------------------------------------------------


def test_grant_permission_returns_flat_envelope(stub_drive_for_grant):
    """The returned dict is the flat ``{permission_id, role,
    granted_to, file_id}`` envelope the tool layer surfaces. Maps
    Drive's ``id`` → ``permission_id`` (so the agent doesn't have to
    learn Drive's vocabulary) and echoes ``file_id`` back for
    self-consistency."""
    stub_drive_for_grant.permissions().create().execute.return_value = {
        "id": "PERM-001",
        "emailAddress": "bob@example.com",
        "role": "reader",
        "type": "user",
    }
    result = grant_permission(
        MagicMock(), "FILE-ABC", "bob@example.com", role="reader",
    )
    assert result == {
        "permission_id": "PERM-001",
        "role": "reader",
        "granted_to": "bob@example.com",
        "file_id": "FILE-ABC",
    }


def test_grant_permission_handles_drive_response_without_emailAddress(
    stub_drive_for_grant,
):
    """Defensive: if Drive ever omits ``emailAddress`` from the
    response (shouldn't for user-type perms, but the SDK contract
    permits it), ``granted_to`` falls back to empty string rather
    than KeyError."""
    stub_drive_for_grant.permissions().create().execute.return_value = {
        "id": "PERM-002",
        "role": "writer",
        "type": "user",
    }
    result = grant_permission(
        MagicMock(), "FILE-X", "u@e.com",
    )
    assert result["granted_to"] == ""


# ---------------------------------------------------------------------
# list_permissions — Drive call shape + response envelope
# ---------------------------------------------------------------------


@pytest.fixture
def stub_drive_for_list():
    """Drive stub for the list path. Default returns an empty
    permission set (private file scenario)."""
    drive = MagicMock(name="drive-v3-stub-list")
    drive.permissions().list().execute.return_value = {"permissions": []}
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def _last_list_kwargs(drive: MagicMock) -> dict:
    for call in reversed(drive.permissions().list.call_args_list):
        if "fileId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no permissions().list() call captured fileId")


def test_list_permissions_passes_fileId(stub_drive_for_list):
    list_permissions(MagicMock(), "FILE-ABC")
    kw = _last_list_kwargs(stub_drive_for_list)
    assert kw["fileId"] == "FILE-ABC"


def test_list_permissions_requests_minimal_fields_mask(stub_drive_for_list):
    """The ``fields`` mask uses Drive's nested-array syntax
    (``permissions(...)``) to limit per-entry fields. Catches a
    regression where someone drops the mask and Drive returns the
    full ACL surface (display names, photo links, etc.)."""
    list_permissions(MagicMock(), "FILE1")
    kw = _last_list_kwargs(stub_drive_for_list)
    assert kw["fields"] == "permissions(id,emailAddress,role,type)"


def test_list_permissions_returns_envelope_with_empty_permissions_for_private_file(
    stub_drive_for_list,
):
    """Empty response (private file — only owner can see it) returns
    ``{file_id, permissions: []}`` rather than missing the key.
    Consumers branch on ``len(permissions)`` rather than truthiness."""
    result = list_permissions(MagicMock(), "FILE-PRIVATE")
    assert result == {"file_id": "FILE-PRIVATE", "permissions": []}


def test_list_permissions_passes_drive_response_through_unchanged(
    stub_drive_for_list,
):
    """When Drive returns permissions, they appear verbatim in the
    response — no per-entry transformation. The Drive shape IS the
    public shape for the consumer."""
    stub_drive_for_list.permissions().list().execute.return_value = {
        "permissions": [
            {"id": "perm-1", "emailAddress": "owner@e.com",
             "role": "owner", "type": "user"},
            {"id": "perm-2", "emailAddress": "alice@e.com",
             "role": "writer", "type": "user"},
            {"id": "perm-3", "role": "reader", "type": "anyone"},
        ],
    }
    result = list_permissions(MagicMock(), "FILE-SHARED")
    assert result["file_id"] == "FILE-SHARED"
    assert len(result["permissions"]) == 3
    # Anyone-link entries don't carry emailAddress — pass through
    # without invention.
    assert "emailAddress" not in result["permissions"][2]


def test_list_permissions_handles_missing_permissions_key_in_response(
    stub_drive_for_list,
):
    """Defensive: if Drive ever omits the ``permissions`` key entirely
    (shouldn't, but the SDK contract permits it), the envelope
    surfaces ``permissions: []`` rather than KeyError."""
    stub_drive_for_list.permissions().list().execute.return_value = {}
    result = list_permissions(MagicMock(), "FILE-Y")
    assert result == {"file_id": "FILE-Y", "permissions": []}


# ---------------------------------------------------------------------
# revoke_permission — pre-API validation
# ---------------------------------------------------------------------


def test_revoke_permission_rejects_blank_permission_id():
    """An empty / whitespace permission_id is a caller bug — surface as
    ValueError BEFORE the Drive round-trip (no get_service needed)."""
    with pytest.raises(ValueError, match="permission_id cannot be empty"):
        revoke_permission(MagicMock(), "FILE1", "   ")
    with pytest.raises(ValueError, match="permission_id cannot be empty"):
        revoke_permission(MagicMock(), "FILE1", "")


# ---------------------------------------------------------------------
# revoke_permission — Drive call shape
# ---------------------------------------------------------------------


@pytest.fixture
def stub_drive_for_revoke():
    """A Drive stub whose permissions().delete().execute() succeeds
    (Drive returns an empty body on a successful delete)."""
    drive = MagicMock(name="drive-v3-stub-revoke")
    drive.permissions().delete().execute.return_value = ""
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def _last_delete_kwargs(drive: MagicMock) -> dict:
    for call in reversed(drive.permissions().delete.call_args_list):
        if "fileId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no permissions().delete() call captured fileId")


def test_revoke_permission_passes_file_and_permission_ids(stub_drive_for_revoke):
    """The Drive call must target both the file_id and permission_id the
    caller passed — permissions.delete is keyed by (fileId, permissionId)."""
    revoke_permission(MagicMock(), "FILE-ABC", "PERM-1")
    kw = _last_delete_kwargs(stub_drive_for_revoke)
    assert kw["fileId"] == "FILE-ABC"
    assert kw["permissionId"] == "PERM-1"


def test_revoke_permission_strips_whitespace_from_permission_id(
    stub_drive_for_revoke,
):
    """Leading / trailing whitespace on permission_id is stripped before
    the Drive call (consistent with grant_permission's email handling)."""
    revoke_permission(MagicMock(), "FILE1", "  PERM-PADDED  ")
    kw = _last_delete_kwargs(stub_drive_for_revoke)
    assert kw["permissionId"] == "PERM-PADDED"


# ---------------------------------------------------------------------
# revoke_permission — response envelope (success)
# ---------------------------------------------------------------------


def test_revoke_permission_returns_revoked_true_on_success(stub_drive_for_revoke):
    """A clean delete returns ``revoked: True`` with
    ``was_already_absent: False`` (the permission existed and is now
    gone)."""
    result = revoke_permission(MagicMock(), "FILE-ABC", "PERM-1")
    assert result == {
        "file_id": "FILE-ABC",
        "permission_id": "PERM-1",
        "revoked": True,
        "was_already_absent": False,
    }


# ---------------------------------------------------------------------
# revoke_permission — idempotent 404 (soft success)
# ---------------------------------------------------------------------


def test_revoke_permission_treats_404_as_idempotent_success():
    """SOFT-SUCCESS GUARD: revoking a permission that's already gone is
    the desired end state for a teardown, so a 404 is returned as
    ``revoked: True, was_already_absent: True`` — NOT raised. This lets
    a teardown loop re-run safely."""
    drive = MagicMock(name="drive-revoke-404")
    drive.permissions().delete().execute.side_effect = _mock_http_error(404)
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = revoke_permission(MagicMock(), "FILE-GONE", "PERM-GONE")
    assert result == {
        "file_id": "FILE-GONE",
        "permission_id": "PERM-GONE",
        "revoked": True,
        "was_already_absent": True,
    }


# ---------------------------------------------------------------------
# revoke_permission — 403 classification (soft failure, returned as data)
# ---------------------------------------------------------------------


def test_revoke_permission_403_app_not_authorized_returns_soft_failure():
    """403 ``appNotAuthorizedToFile`` (the file wasn't created by this
    app) is returned as data — ``revoked: False, reason:
    "app_not_authorized"`` — matching the trash / move contract so a
    batch teardown can skip-and-continue."""
    drive = MagicMock(name="drive-revoke-403-app")
    drive.permissions().delete().execute.side_effect = _mock_http_error(
        403, "appNotAuthorizedToFile",
    )
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = revoke_permission(MagicMock(), "FOREIGN-FILE", "PERM-X")
    assert result["revoked"] is False
    assert result["reason"] == "app_not_authorized"
    assert result["file_id"] == "FOREIGN-FILE"
    assert result["permission_id"] == "PERM-X"


def test_revoke_permission_other_403_returns_cannot_revoke():
    """A 403 whose reason is NOT ``appNotAuthorizedToFile`` (most
    commonly an attempt to remove the file's sole owner) is surfaced as
    a DISTINCT soft-failure ``reason: "cannot_revoke"`` so callers don't
    conflate it with the scope / app-authorization case."""
    drive = MagicMock(name="drive-revoke-403-owner")
    drive.permissions().delete().execute.side_effect = _mock_http_error(
        403, "cannotModifyInheritedPermission",
    )
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = revoke_permission(MagicMock(), "FILE1", "PERM-OWNER")
    assert result["revoked"] is False
    assert result["reason"] == "cannot_revoke"


# ---------------------------------------------------------------------
# revoke_permission — genuine errors still raise
# ---------------------------------------------------------------------


def test_revoke_permission_reraises_unclassified_http_error():
    """A status Drive doesn't classify above (e.g. 500) must PROPAGATE
    as HttpError so genuine bugs surface — the soft-failure handling is
    deliberately scoped to 404 / 403 only."""
    drive = MagicMock(name="drive-revoke-500")
    drive.permissions().delete().execute.side_effect = _mock_http_error(500)
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(HttpError):
            revoke_permission(MagicMock(), "FILE1", "PERM-1")
