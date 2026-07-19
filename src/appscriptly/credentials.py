"""Per-request Google Credentials for v1.1+ multi-tenant cloud MCP.

Bridges ``user_store`` (per-user persistent state) and the
google-api-python-client ``Credentials`` object that tool code passes
to ``build("docs", "v1", credentials=...)``.

**Responsibilities:**

1. **Resolve creds from a user_id.** Look up the cached tokens,
   reconstruct a ``Credentials`` object, inject the operator's
   ``client_id``/``client_secret`` from runtime config (never stored
   per-user — see ``user_store.save_credentials_json``).

2. **Refresh on demand, atomically per user.** Google rotates the
   refresh_token on every refresh; two parallel tool calls for the
   same user can both call ``creds.refresh()`` and one of them ends
   up with an invalidated refresh_token. A per-user lock serializes
   refresh so only the first wins; subsequent callers re-read the
   fresh creds from user_store.

3. **Translate revocation into a clean re-auth signal.** When Google
   returns ``invalid_grant`` (user revoked, refresh_token rotated by
   another flow, etc.), clear the stored creds and raise
   ``NeedsReauthError`` carrying a fresh authorization URL the tool
   should emit to the user.

**What this module does NOT do.** It doesn't know how to find the
calling user from FastMCP request context — that's the tool's job
(via ``get_access_token().claims["sub"]``). Keeping the user_id as
an explicit parameter makes the module pure-function testable.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from . import user_store
from .oauth_google import GOOGLE_API_SCOPES, build_authorization_url

log = logging.getLogger("appscriptly.credentials")

# PR-Δ5: dedicated audit logger for multi-tenant credential dispatch.
# Separate from ``appscriptly.credentials`` so operators can
# selectively elevate audit-trail verbosity (e.g. for SOC 2 prep:
# pipe ``appscriptly.audit.tenant`` to a separate sink that's
# retained for the compliance window). The request-ID injected by
# ``RequestIdMiddleware`` (PR-Δ4) automatically appears in every
# audit line thanks to ``RequestIdLogFilter`` on the root logger.
_audit_log = logging.getLogger("appscriptly.audit.tenant")


# PR-Δ5: attribute name used to stamp the resolved user_id onto a
# ``Credentials`` object so downstream call sites can defensively
# verify the binding via ``assert_tenant_match``. Centralized as a
# constant so the spelling never drifts between the writer here and
# the reader in ``_tool_helpers``.
_TENANT_ATTR = "_google_docs_mcp_user_id"


def _stamp_tenant(creds: Credentials, user_id: str) -> Credentials:
    """Attach ``user_id`` to ``creds`` so tenant-match checks can fire.

    Google's ``Credentials`` class does not carry the originating
    user_id natively (the binding lives at the storage layer:
    ``user_store.get_state(user_id) → google_creds_json``). For
    defensive multi-tenant safety we stamp the user_id onto the
    instance via ``setattr`` — Credentials inherits from a flexible
    base, accepts extra attributes without complaint, and the stamp
    is process-local (never serialized back to user_store).

    The stamp is what ``_tool_helpers.assert_tenant_match`` reads to
    confirm "these creds are FOR the user the tool thinks they are."
    Today the assertion is belt-and-suspenders — the storage layer
    is the source of truth, and the storage layer is correct. The
    stamp catches a future caching bug, a future SQL injection that
    swaps the WHERE clause, a future race condition that returns
    the wrong row. Without the stamp those bugs would surface as
    silent cross-tenant data access; with it they surface as a
    loud AssertionError before any user data is touched.
    """
    setattr(creds, _TENANT_ATTR, user_id)
    return creds


def _emit_tenant_audit_log(
    user_id: str,
    *,
    required_scopes: list[str] | None,
    granted_scopes: list[str] | None,
    outcome: str,
) -> None:
    """Emit a structured audit-log record for a credential-dispatch event.

    Logger: ``appscriptly.audit.tenant`` — operators can route
    this to a separate retention sink for SOC 2 / compliance audit
    trails. The ``extra`` dict carries structured fields that get
    rendered by any JSON-formatter configured downstream; the
    formatted message is human-readable for default text logs.

    The request-ID from PR-Δ4 is auto-injected by
    ``RequestIdLogFilter`` (installed on the root logger) — no
    explicit threading needed here. Outside HTTP context the
    placeholder ``-`` appears, which is fine: stdio-mode credential
    dispatch is single-tenant and the audit trail is the operator's
    own activity.

    Args:
        user_id: The tenant identifier credentials were dispatched
            for. Truncated to first 8 chars in the human message
            (full value in the structured field) so log lines don't
            leak the entire ``sub`` claim into shoulder-surfable
            terminal output.
        required_scopes: The scope list the call site asked for
            (None = no scope check). Helps a compliance reviewer
            tell "tool X demanded scope Y for user Z" from "tool X
            ran without a scope check."
        granted_scopes: The scope list the returned creds actually
            carry (None = unknown). The intersection of required vs
            granted is the compliance-relevant surface.
        outcome: One of ``"dispatched"`` (creds returned),
            ``"needs_reauth"`` (NeedsReauthError raised),
            ``"revoked"`` (invalid_grant caught and state cleared).
    """
    _audit_log.info(
        "tenant_dispatch user_id=%s... outcome=%s required_scopes=%d granted_scopes=%d",
        user_id[:8] if user_id else "-",
        outcome,
        len(required_scopes) if required_scopes else 0,
        len(granted_scopes) if granted_scopes else 0,
        extra={
            "audit_event": "tenant_dispatch",
            "audit_user_id": user_id,
            "audit_outcome": outcome,
            "audit_required_scopes": required_scopes or [],
            "audit_granted_scopes": granted_scopes or [],
        },
    )


def current_user_id_or_none() -> str | None:
    """Return Google ``sub`` of calling user, or None outside auth context.

    HTTP mode (FastMCP has an auth provider set): returns ``sub`` from
    the FastMCP-issued JWT's claims. Stdio mode (no auth provider) or
    REST endpoint (bearer-token-authed, not MCP): returns None.

    Used as the mode discriminator across the codebase — caller branches
    into per-user (HTTP) vs operator-cached (stdio) credential and
    Apps-Script-URL lookup paths.

    Defensive about fastmcp version drift — the dependency surface for
    ``get_access_token`` has moved across 2.x; if import fails or the
    call raises, treat it as "no auth context" rather than crashing.
    """
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:
        return None
    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001 — defensive against version drift
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    return claims.get("sub")


class NeedsReauthError(Exception):
    """Raised when a user must (re-)authorize before we can call Google.

    The MCP tool catches this and returns a Markdown response with
    ``auth_url`` so Claude renders a clickable link. Three causes:

    - No creds cached yet (first-time user)
    - ``invalid_grant`` on refresh (revoked or rotated out-of-band)
    - Required scope not in granted scopes (incremental authorization)
    """

    def __init__(self, user_id: str, *, auth_url: str, reason: str) -> None:
        super().__init__(reason)
        self.user_id = user_id
        self.auth_url = auth_url
        self.reason = reason


# Per-user lock registry. Bounded by the number of distinct users
# the server has handled since start (small in practice). Using
# threading.Lock so this works for both sync and async tools — async
# tools call sync Google APIs anyway (no asyncio.Lock benefit).
_per_user_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _user_lock(user_id: str) -> threading.Lock:
    with _registry_lock:
        lock = _per_user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _per_user_locks[user_id] = lock
        return lock


def get_credentials_for_user(
    user_id: str,
    *,
    client_config: dict,
    signing_key: bytes,
    base_url: str,
    required_scopes: list[str] | None = None,
    enc_key: bytes | None = None,
) -> Credentials:
    """Return valid Google API ``Credentials`` for ``user_id``.

    ``enc_key`` (AES-GCM key for the encrypted PKCE verifier) is threaded
    into the auth-URL builder for the (re-)authorization paths. In
    production it always arrives via ``**resolve_runtime_oauth_config()``;
    it is Optional only so direct test callers that never mint an auth
    URL need not supply it.

    Raises ``NeedsReauthError`` if the user needs to (re-)authorize.
    The caller (tool function) should catch it and surface
    ``e.auth_url`` to the user as a clickable Markdown link.

    Refreshes expired tokens transparently and persists the new
    token back to ``user_store``. Concurrent calls for the same user
    are serialized — one refresh, all callers see the new token.
    """
    state = user_store.get_state(user_id)

    if "google_creds_json" not in state:
        _emit_tenant_audit_log(
            user_id,
            required_scopes=required_scopes,
            granted_scopes=None,
            outcome="needs_reauth",
        )
        raise NeedsReauthError(
            user_id,
            auth_url=_auth_url(
                user_id, client_config, signing_key, base_url, enc_key=enc_key,
            ),
            reason="Google API credentials not yet authorized",
        )

    creds = _credentials_from_state(state, client_config)

    if creds.valid:
        checked = _check_scopes_or_raise(
            creds, user_id, required_scopes, client_config, signing_key, base_url,
            enc_key=enc_key,
        )
        _emit_tenant_audit_log(
            user_id,
            required_scopes=required_scopes,
            granted_scopes=list(checked.scopes or []),
            outcome="dispatched",
        )
        return _stamp_tenant(checked, user_id)

    if not creds.refresh_token:
        # Expired AND no refresh_token — can't recover silently.
        _emit_tenant_audit_log(
            user_id,
            required_scopes=required_scopes,
            granted_scopes=None,
            outcome="needs_reauth",
        )
        raise NeedsReauthError(
            user_id,
            auth_url=_auth_url(
                user_id, client_config, signing_key, base_url, enc_key=enc_key,
            ),
            reason="Credentials expired and no refresh token available",
        )

    # Refresh path — serialize per user.
    lock = _user_lock(user_id)
    with lock:
        # Re-read inside the lock. Another caller may have just refreshed
        # and persisted; no point calling Google again.
        state = user_store.get_state(user_id)
        creds = _credentials_from_state(state, client_config)
        if creds.valid:
            checked = _check_scopes_or_raise(
                creds, user_id, required_scopes, client_config,
                signing_key, base_url, enc_key=enc_key,
            )
            _emit_tenant_audit_log(
                user_id,
                required_scopes=required_scopes,
                granted_scopes=list(checked.scopes or []),
                outcome="dispatched",
            )
            return _stamp_tenant(checked, user_id)

        try:
            creds.refresh(Request())
        except RefreshError as e:
            if _is_invalid_grant(e):
                log.info(
                    "credentials: clearing revoked creds for user %s",
                    user_id,
                )
                user_store.clear_state(user_id)
                _emit_tenant_audit_log(
                    user_id,
                    required_scopes=required_scopes,
                    granted_scopes=None,
                    outcome="revoked",
                )
                raise NeedsReauthError(
                    user_id,
                    auth_url=_auth_url(
                        user_id, client_config, signing_key, base_url,
                        enc_key=enc_key,
                    ),
                    reason=(
                        "Google credentials were revoked or rotated. "
                        "Re-authorize to continue."
                    ),
                ) from e
            # Unknown refresh failure — propagate. Network blip, Google
            # 5xx, etc. The tool should fail loudly, not swallow.
            raise

        user_store.save_credentials_json(user_id, creds.to_json())

    checked = _check_scopes_or_raise(
        creds, user_id, required_scopes, client_config, signing_key, base_url,
        enc_key=enc_key,
    )
    _emit_tenant_audit_log(
        user_id,
        required_scopes=required_scopes,
        granted_scopes=list(checked.scopes or []),
        outcome="dispatched",
    )
    return _stamp_tenant(checked, user_id)


def _credentials_from_state(
    state: user_store.UserState, client_config: dict
) -> Credentials:
    """Reconstruct ``Credentials`` from stored JSON + runtime client_config.

    ``client_id`` and ``client_secret`` are NEVER read from the stored
    JSON — they're operator-level secrets injected from runtime config.
    See ``user_store.save_credentials_json`` for the stripping side.

    ``expiry`` is stored as ISO 8601 string (the format
    ``Credentials.to_json()`` produces) and reconstituted to a naive
    UTC datetime here — that's the shape ``Credentials.expired``
    compares against.

    Precondition: the caller MUST have verified
    ``"google_creds_json" in state`` before reaching this function.
    Every call site does (see line ~133 in
    ``get_credentials_for_user``); the asserts here make the
    invariant load-bearing for type-checkers.
    """
    creds_json = state.get("google_creds_json")
    assert creds_json is not None, (
        "_credentials_from_state called without google_creds_json — "
        "caller must check `'google_creds_json' in state` first"
    )
    raw = json.loads(creds_json)
    client_block = client_config.get("web") or client_config.get("installed") or {}

    expiry = raw.get("expiry")
    if isinstance(expiry, str):
        # ``Credentials.to_json()`` emits ISO format. The class
        # internally expects a naive UTC datetime — strip any tzinfo
        # after parse to match.
        parsed = datetime.fromisoformat(expiry)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        expiry = parsed

    return Credentials(
        token=raw.get("token"),
        refresh_token=raw.get("refresh_token"),
        token_uri=raw.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=raw.get("scopes"),
        expiry=expiry,
        client_id=client_block.get("client_id"),
        client_secret=client_block.get("client_secret"),
    )


def _check_scopes_or_raise(
    creds: Credentials,
    user_id: str,
    required_scopes: list[str] | None,
    client_config: dict,
    signing_key: bytes,
    base_url: str,
    enc_key: bytes | None = None,
) -> Credentials:
    """If any required scope is missing, raise NeedsReauthError.

    Incremental authorization (Google's
    ``include_granted_scopes=true``) is what the re-auth URL uses, so
    consenting again only adds the missing scope — doesn't reset the
    others. Documented in build_authorization_url.
    """
    if not required_scopes:
        return creds
    granted = set(creds.scopes or [])
    missing = [s for s in required_scopes if s not in granted]
    if missing:
        raise NeedsReauthError(
            user_id,
            auth_url=_auth_url(
                user_id, client_config, signing_key, base_url,
                scopes=list(set(GOOGLE_API_SCOPES) | set(required_scopes)),
                enc_key=enc_key,
            ),
            reason=f"Missing required scopes: {missing}",
        )
    return creds


def _is_invalid_grant(e: RefreshError) -> bool:
    """Pattern-match Google's many ways of saying 'token revoked'."""
    msg = str(e).lower()
    return any(
        s in msg for s in (
            "invalid_grant",
            "expired or revoked",
            "token has been expired",
            "bad request",  # Google sometimes returns this for revoked creds
        )
    )


def _auth_url(
    user_id: str,
    client_config: dict,
    signing_key: bytes,
    base_url: str,
    *,
    scopes: list[str] | None = None,
    enc_key: bytes | None = None,
) -> str:
    return build_authorization_url(
        user_id,
        base_url=base_url,
        client_config=client_config,
        signing_key=signing_key,
        enc_key=enc_key,
        scopes=scopes,
    )
