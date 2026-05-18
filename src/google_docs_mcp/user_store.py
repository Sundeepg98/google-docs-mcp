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
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from typing_extensions import TypedDict

from .auth import default_data_dir


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
    created_at: int                 # unix epoch seconds, first write
    updated_at: int                 # unix epoch seconds, last write


_PERSISTENT_FIELDS = {
    "google_creds_json",
    "apps_script_url",
    "apps_script_script_id",
    "apps_script_deployment_id",
    "apps_script_version_number",
    "apps_script_content_hash",
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


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection with the schema ensured and WAL enabled.

    Lazy init: the first call from a fresh deployment creates the
    table; subsequent calls are no-ops. WAL mode lets concurrent
    readers (tool calls) not block the writer (OAuth callback).
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
                created_at                   INTEGER NOT NULL,
                updated_at                   INTEGER NOT NULL
            )
            """
        )
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
    return {k: row[k] for k in row.keys() if row[k] is not None}  # type: ignore[return-value]


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
