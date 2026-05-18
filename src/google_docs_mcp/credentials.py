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

log = logging.getLogger("google_docs_mcp.credentials")


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
    signing_key: str,
    base_url: str,
    required_scopes: list[str] | None = None,
) -> Credentials:
    """Return valid Google API ``Credentials`` for ``user_id``.

    Raises ``NeedsReauthError`` if the user needs to (re-)authorize.
    The caller (tool function) should catch it and surface
    ``e.auth_url`` to the user as a clickable Markdown link.

    Refreshes expired tokens transparently and persists the new
    token back to ``user_store``. Concurrent calls for the same user
    are serialized — one refresh, all callers see the new token.
    """
    state = user_store.get_state(user_id)

    if "google_creds_json" not in state:
        raise NeedsReauthError(
            user_id,
            auth_url=_auth_url(user_id, client_config, signing_key, base_url),
            reason="Google API credentials not yet authorized",
        )

    creds = _credentials_from_state(state, client_config)

    if creds.valid:
        return _check_scopes_or_raise(
            creds, user_id, required_scopes, client_config, signing_key, base_url,
        )

    if not creds.refresh_token:
        # Expired AND no refresh_token — can't recover silently.
        raise NeedsReauthError(
            user_id,
            auth_url=_auth_url(user_id, client_config, signing_key, base_url),
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
            return _check_scopes_or_raise(
                creds, user_id, required_scopes, client_config,
                signing_key, base_url,
            )

        try:
            creds.refresh(Request())
        except RefreshError as e:
            if _is_invalid_grant(e):
                log.info(
                    "credentials: clearing revoked creds for user %s",
                    user_id,
                )
                user_store.clear_state(user_id)
                raise NeedsReauthError(
                    user_id,
                    auth_url=_auth_url(user_id, client_config, signing_key, base_url),
                    reason=(
                        "Google credentials were revoked or rotated. "
                        "Re-authorize to continue."
                    ),
                ) from e
            # Unknown refresh failure — propagate. Network blip, Google
            # 5xx, etc. The tool should fail loudly, not swallow.
            raise

        user_store.save_credentials_json(user_id, creds.to_json())

    return _check_scopes_or_raise(
        creds, user_id, required_scopes, client_config, signing_key, base_url,
    )


def _credentials_from_state(state: dict, client_config: dict) -> Credentials:
    """Reconstruct ``Credentials`` from stored JSON + runtime client_config.

    ``client_id`` and ``client_secret`` are NEVER read from the stored
    JSON — they're operator-level secrets injected from runtime config.
    See ``user_store.save_credentials_json`` for the stripping side.

    ``expiry`` is stored as ISO 8601 string (the format
    ``Credentials.to_json()`` produces) and reconstituted to a naive
    UTC datetime here — that's the shape ``Credentials.expired``
    compares against.
    """
    raw = json.loads(state["google_creds_json"])
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
    signing_key: str,
    base_url: str,
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
    signing_key: str,
    base_url: str,
    *,
    scopes: list[str] | None = None,
) -> str:
    return build_authorization_url(
        user_id,
        base_url=base_url,
        client_config=client_config,
        signing_key=signing_key,
        scopes=scopes,
    )
