"""Per-user state for multi-tenant cloud MCP (v1.1+).

When the MCP server runs over HTTP (Fly.io) with proper OAuth, each
calling user has their own Google credentials and their own Apps
Script Web App deployment. This module is the storage layer for that
per-user state, keyed by the user's identity (Google ``sub`` claim
from the OAuth id_token, fall back to email).

SQLite was picked over JSON-per-user because:
- Atomic merge-updates (no read-modify-write races between concurrent
  tool calls touching the same user's row).
- Built-in to stdlib, no extra dep.
- One DB file is easier to back up / inspect than N JSON files.
- WAL mode lets MCP tools read while the OAuth callback writes.

Stays separate from ``config.py`` (which is single-tenant local state
for the stdio MCP — token cache, operator's webapp URL) on purpose:
different consumers, different lifecycles, different security model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from typing_extensions import TypedDict

from .auth import default_data_dir

_log = logging.getLogger("google_docs_mcp.user_store")

# Apps Script Web App URL: https://script.google.com/macros/s/<deploymentId>/(exec|dev)
_GAS_URL_PATH_RE = re.compile(r"^/macros/s/[A-Za-z0-9_-]+/(exec|dev)$")


def _valid_gas_url(value: object) -> bool:
    """True if ``value`` is a valid Apps Script Web App URL string.

    Accepts only ``https://script.google.com/macros/s/<deploymentId>/(exec|dev)``.
    Used by ``_FIELD_VALIDATORS`` to gate writes to ``apps_script_url`` and to
    drop tampered/legacy values on read. Defense-in-depth — the setup tool
    builds this URL itself, so an invalid value here signals either a
    pre-validator install, manual DB tampering, or a bug in the setup path.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
    except (ValueError, TypeError):
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host != "script.google.com":
        return False
    return bool(_GAS_URL_PATH_RE.match(parsed.path or ""))


def _valid_apps_script_hmac_key(value: object) -> bool:
    """True if ``value`` is a 64-char lowercase hex string (HMAC-SHA256 key).

    The per-user HMAC key signs requests from the MCP server to the user's
    Apps Script Web App (v2.0+). 64 hex chars = 256 bits — exactly the
    HMAC-SHA256 key size. Lowercase-only avoids ambiguity between casings
    when keys round-trip through logs or copy-paste.

    Used by ``_FIELD_VALIDATORS`` to gate writes (setup tool + migration
    script produce keys via ``secrets.token_hex(32)``) and to drop tampered
    values on read. An invalid persisted value signals either a
    pre-validator install, manual DB tampering, or a regressed key-gen path.
    """
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


# Per-field validators run in save_state (raise) and get_state (drop+log).
# Add a new field by writing a ``_valid_<field>`` helper and registering it
# here; keep the field listed in ``_PERSISTENT_FIELDS`` too.
_FIELD_VALIDATORS: dict[str, Callable[[object], bool]] = {
    "apps_script_url": _valid_gas_url,
    "apps_script_hmac_key": _valid_apps_script_hmac_key,
}

# Per-path init guard. ``PRAGMA journal_mode=WAL`` requires an exclusive
# lock to transition into WAL mode — and ``busy_timeout`` doesn't help
# here, the call fails fast under contention. We serialize first-time
# init under a Python-level lock; once a path is in ``_initialized_paths``,
# subsequent connections skip the init and just open normally (WAL mode
# is persistent in the DB file).
_initialized_paths: set[Path] = set()
_init_lock = threading.Lock()


class UserState(TypedDict, total=False):
    """Schema documentation for the ``user_state`` table.

    All fields except ``user_id`` are optional — a row gets created
    by the OAuth callback (sets ``google_creds_json``) and later
    enriched by ``gdocs_setup_apps_script`` (sets ``apps_script_*``).
    """
    user_id: str
    google_creds_json: str          # creds.to_json() output, parse before use
    apps_script_url: str            # the /exec endpoint of the deployed Web App
    apps_script_script_id: str      # GAS project id; lets us update vs re-create
    apps_script_deployment_id: str  # GAS deployment id (versioned)
    apps_script_version_number: int
    apps_script_content_hash: str   # for setup idempotency (matches setup_state.py)
    apps_script_hmac_key: str       # 64-char lowercase hex; HMAC-SHA256 key per user (v2.0+)
    created_at: int                 # unix epoch seconds, first write
    updated_at: int                 # unix epoch seconds, last write


_PERSISTENT_FIELDS = {
    "google_creds_json",
    "apps_script_url",
    "apps_script_script_id",
    "apps_script_deployment_id",
    "apps_script_version_number",
    "apps_script_content_hash",
    "apps_script_hmac_key",
}


def db_path() -> Path:
    """Resolve the SQLite file path.

    Override with ``GOOGLE_DOCS_USER_STORE_PATH`` (useful for tests
    and for pointing Fly's volume mount at a known location).
    """
    override = os.environ.get("GOOGLE_DOCS_USER_STORE_PATH")
    if override:
        return Path(override)
    return default_data_dir() / "user_state.db"


def _ensure_initialized(path: Path) -> None:
    """First-time init for ``path``: set WAL mode + create schema.

    Serialized under ``_init_lock`` so concurrent callers don't race
    on the PRAGMA. Idempotent — subsequent calls fast-path through
    the set-membership check without grabbing the lock.
    """
    if path in _initialized_paths:
        return
    with _init_lock:
        if path in _initialized_paths:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_state (
                    user_id                      TEXT PRIMARY KEY,
                    google_creds_json            TEXT,
                    apps_script_url              TEXT,
                    apps_script_script_id        TEXT,
                    apps_script_deployment_id    TEXT,
                    apps_script_version_number   INTEGER,
                    apps_script_content_hash     TEXT,
                    apps_script_hmac_key         TEXT,
                    created_at                   INTEGER NOT NULL,
                    updated_at                   INTEGER NOT NULL
                )
                """
            )
            # Existing v1.x deployments hit CREATE TABLE IF NOT EXISTS
            # without the new column. Add it idempotently. ALTER TABLE
            # ADD COLUMN with no DEFAULT is fast (no row rewrite) and
            # leaves existing rows with NULL — which migrate_existing_users
            # then backfills.
            #
            # Use PRAGMA table_info to decide whether to ALTER rather
            # than catching OperationalError on the ALTER itself —
            # SQLite's "duplicate column name" error is reported as
            # generic SQLITE_ERROR (no specific extended code), so
            # string-matching the message is fragile across versions
            # and locales. table_info is the official introspection
            # surface and gives an unambiguous answer.
            existing_cols = {
                row[1]  # row = (cid, name, type, notnull, dflt, pk)
                for row in conn.execute("PRAGMA table_info(user_state)")
            }
            if "apps_script_hmac_key" not in existing_cols:
                conn.execute(
                    "ALTER TABLE user_state ADD COLUMN apps_script_hmac_key TEXT"
                )
        finally:
            conn.close()
        _initialized_paths.add(path)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection to the (already-initialized) DB.

    WAL mode is persistent in the DB file — once
    ``_ensure_initialized`` has set it, every subsequent open uses
    WAL automatically. ``busy_timeout=5000ms`` lets concurrent
    writers WAIT for the row-level lock rather than failing fast.
    """
    path = db_path()
    _ensure_initialized(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def get_state(user_id: str) -> UserState:
    """Fetch a user's full state. Returns ``{}`` for unknown users.

    Callers should treat the absence of a field the same as "not yet
    set" — e.g. a tool that needs ``apps_script_url`` and finds it
    missing should raise with a clear "run gdocs_setup_apps_script
    first" message.
    """
    if not user_id:
        raise ValueError("user_id is required")

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_state WHERE user_id = ?", (user_id,)
        ).fetchone()

    if row is None:
        return {}
    result: dict[str, Any] = {k: row[k] for k in row.keys() if row[k] is not None}

    # Drop+log any persisted field that fails validation. Likely causes:
    # row written before validators existed, manual SQL tampering, or a
    # regressed setup path. Re-running setup repopulates the field.
    for col, validator in _FIELD_VALIDATORS.items():
        if col in result and not validator(result[col]):
            _log.warning(
                "user_store: dropping invalid persisted %s=%r for user "
                "%s — likely from a pre-validator install or external "
                "tampering; re-run setup to repopulate",
                col, str(result[col])[:60], user_id[:8],
            )
            del result[col]

    return result  # type: ignore[return-value]


def save_state(user_id: str, updates: dict[str, Any]) -> UserState:
    """Merge ``updates`` into the user's row and return the full new state.

    Semantics match ``config.py::save()``: existing fields not in
    ``updates`` are preserved; ``updates`` overrides on conflict.
    Unknown keys in ``updates`` are rejected loudly — typos here are
    the kind of bug that silently sets the wrong row forever.

    ``created_at`` is set on first write and never changed.
    ``updated_at`` is bumped on every call.
    """
    if not user_id:
        raise ValueError("user_id is required")

    unknown = set(updates) - _PERSISTENT_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown user_state fields: {sorted(unknown)}. "
            f"Allowed: {sorted(_PERSISTENT_FIELDS)}"
        )

    # Field-level validation. ``None`` means "explicitly clear" — allowed
    # past the validator on purpose so a caller can blank a field that
    # was previously set. Invalid non-None values raise; the SQL write
    # below never happens.
    for col, value in updates.items():
        validator = _FIELD_VALIDATORS.get(col)
        if validator is not None and value is not None and not validator(value):
            raise ValueError(
                f"user_state field {col!r} failed validation: "
                f"{value!r} is not a valid value. See "
                f"user_store._FIELD_VALIDATORS."
            )

    now = int(time.time())
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM user_state WHERE user_id = ?", (user_id,)
        ).fetchone()

        if existing is None:
            cols = ["user_id", "created_at", "updated_at", *updates.keys()]
            vals = [user_id, now, now, *updates.values()]
            placeholders = ", ".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO user_state ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        else:
            cols = [*updates.keys(), "updated_at"]
            vals = [*updates.values(), now, user_id]
            set_clause = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE user_state SET {set_clause} WHERE user_id = ?", vals,
            )

    return get_state(user_id)


def clear_state(user_id: str) -> None:
    """Delete a user's row. Idempotent — no error if the row is absent.

    Use cases: user revokes consent in claude.ai's connector UI and
    we want to clean up; user manually invokes a reset tool;
    integration tests cleaning up between runs.
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _connect() as conn:
        conn.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))


def google_creds_dict(state: UserState) -> dict | None:
    """Parse ``google_creds_json`` if present, else None.

    Convenience helper — every consumer would otherwise duplicate
    ``json.loads(state["google_creds_json"]) if "google_creds_json"
    in state else None``.
    """
    raw = state.get("google_creds_json")
    if raw is None:
        return None
    return json.loads(raw)


def save_credentials_json(user_id: str, creds_to_json_output: str) -> UserState:
    """Persist Google Credentials JSON safely (strips operator secrets).

    Defense in depth: ``Credentials.to_json()`` includes ``client_id``
    and ``client_secret`` — but those are operator-level OAuth app
    secrets, not user-specific. Storing them in every user row means
    a user_state.db leak hands an attacker the credentials to
    impersonate the entire OAuth app to Google. Strip before persist;
    re-inject from runtime config at load time
    (see ``credentials._credentials_from_state``).

    Use this instead of ``save_state(uid, {"google_creds_json": ...})``
    when the JSON came from ``Credentials.to_json()``. Pure pass-through
    save_state is fine for tests / fixtures that don't carry secrets.
    """
    raw = json.loads(creds_to_json_output)
    raw.pop("client_id", None)
    raw.pop("client_secret", None)
    return save_state(user_id, {"google_creds_json": json.dumps(raw)})
