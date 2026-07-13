"""Per-user ledger of the Apps Script automations appscriptly minted.

Why this exists: the ``as_*`` installers each mint a bound (or standalone)
Apps Script project in the user's Google account, but a minted project is
INVISIBLE to the connector afterward. Stream-0 dogfood finding S0-1 proved
it live: ``drive.files.list(mimeType=...google-apps.script)`` returns zero
immediately after minting several projects, because Apps Script projects
are created via the Apps Script API (``projects.create``), not the Drive
API, so they never enter the connector's ``drive.file`` per-file grant
set. There is therefore NO connector-side way to enumerate or reap an
already-minted automation.

Consequence (S0-1 + S0-4): this ledger is the ONLY discovery surface for
what a user has installed, and it is **FORWARD-ONLY** by proof — nothing
can backfill it from Drive, so a mint that does not write its row here is
permanently undiscoverable. Treat the row-write as part of the mint: the
lifecycle helper writes the row in the SAME flow as every deploy, and a
mint without a ledger row is a bug (test-pinned in
``tests/unit/test_automation_ledger.py`` +
``tests/unit/services/apps_script/test_lifecycle.py``).

Storage mirrors ``job_store``'s (and thus ``user_store``'s) SQLite
patterns — atomic writes, stdlib only, WAL for concurrent read/write, one
file on the Fly ``/data`` volume — but in a SEPARATE DB file. The ledger
is DURABLE per-user configuration (it must survive deploys, exactly like
``user_state.db``), unlike ``convert_jobs.db``'s operational transients;
it lives in its own file so its schema migrations never couple to
``user_state.db``'s (the same rationale ``job_store`` documents).

Unlike ``user_store``'s single-row-per-user ``user_state`` table, this is
a MANY-rows-per-user table (one row per minted automation), so it does
not ride the ``StorageBackend`` abstraction (which is a per-user
key/value shape). It is a plain module-level CRUD surface, exactly like
``job_store``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .auth import default_data_dir

_log = logging.getLogger("appscriptly.automation_ledger")

# Same per-path init-guard pattern as user_store / job_store: the WAL
# transition needs an exclusive lock, so first-time init is serialized;
# later connects skip it (WAL mode persists in the DB file).
_initialized_paths: set[Path] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    """Resolve the SQLite file path for the automation ledger.

    Override with ``GOOGLE_DOCS_AUTOMATION_LEDGER_PATH`` (tests; exotic
    mounts). Default sits next to ``user_state.db`` on the data dir, which
    on Fly is the persistent ``/data`` volume — the ledger must survive
    deploys because it is the only record of what the user installed.
    """
    override = os.environ.get("GOOGLE_DOCS_AUTOMATION_LEDGER_PATH")
    if override:
        return Path(override)
    return default_data_dir() / "automation_ledger.db"


def _ensure_initialized(path: Path) -> None:
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
                CREATE TABLE IF NOT EXISTS automation_ledger (
                    script_id         TEXT PRIMARY KEY,
                    user_id           TEXT NOT NULL,
                    tool              TEXT NOT NULL,
                    container_id      TEXT,
                    container_kind    TEXT,
                    deployment_id     TEXT,
                    project_url       TEXT,
                    exec_url          TEXT,
                    content_hash      TEXT,
                    handler_functions TEXT NOT NULL DEFAULT '[]',
                    created_at        INTEGER NOT NULL,
                    updated_at        INTEGER NOT NULL
                )
                """
            )
            # List = "this user's automations, newest first"; on_conflict
            # lookup = "this user's automations for (tool, container)". Two
            # covering indexes so neither path scans the table.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_ledger_owner "
                "ON automation_ledger (user_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_ledger_target "
                "ON automation_ledger (user_id, tool, container_id)"
            )
        finally:
            conn.close()
        _initialized_paths.add(path)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection to the (initialized) DB and close it.

    Opened per operation in whatever thread calls it; no connection is
    ever shared across threads, so the default ``check_same_thread``
    stands. ``busy_timeout`` lets concurrent writers wait for the WAL lock
    instead of failing fast, exactly like ``user_store`` / ``job_store``.
    """
    path = db_path()
    _ensure_initialized(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Materialize a ledger row, parsing ``handler_functions`` JSON back
    to a list so callers never see the raw TEXT column."""
    out = {k: row[k] for k in row.keys()}
    raw = out.get("handler_functions")
    try:
        parsed = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        parsed = []
    out["handler_functions"] = parsed if isinstance(parsed, list) else []
    return out


def record_automation(
    *,
    user_id: str,
    script_id: str,
    tool: str,
    container_id: str | None,
    container_kind: str | None,
    deployment_id: str | None = None,
    project_url: str | None = None,
    exec_url: str | None = None,
    content_hash: str | None = None,
    handler_functions: Sequence[str] = (),
) -> None:
    """Insert (or refresh) the ledger row for a minted automation.

    Written in the SAME flow as the mint — a mint without this call is a
    bug (the automation becomes undiscoverable, per S0-1). Keyed by
    ``script_id`` (the mint's identity), so an idempotent re-install of
    the SAME project UPDATES its row rather than duplicating it;
    ``created_at`` is preserved on update, ``updated_at`` is bumped.

    ``handler_functions`` records the installable-trigger handler names
    (Classes D/E) so uninstall can regenerate a self-disarming body that
    redefines exactly those functions (see ``_lifecycle.uninstall_automation``).
    Empty for classes with no installable trigger.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if not script_id:
        raise ValueError("script_id is required")
    if not tool:
        raise ValueError("tool is required")
    now = int(time.time())
    handlers_json = json.dumps([str(h) for h in handler_functions])
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO automation_ledger (
                script_id, user_id, tool, container_id, container_kind,
                deployment_id, project_url, exec_url, content_hash,
                handler_functions, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(script_id) DO UPDATE SET
                user_id = excluded.user_id,
                tool = excluded.tool,
                container_id = excluded.container_id,
                container_kind = excluded.container_kind,
                deployment_id = excluded.deployment_id,
                project_url = excluded.project_url,
                exec_url = excluded.exec_url,
                content_hash = excluded.content_hash,
                handler_functions = excluded.handler_functions,
                updated_at = excluded.updated_at
            """,
            (
                script_id, user_id, tool, container_id, container_kind,
                deployment_id, project_url, exec_url, content_hash,
                handlers_json, now, now,
            ),
        )


def get_automation(script_id: str) -> dict[str, Any] | None:
    """Return the ledger row for ``script_id``, or None if not recorded.

    NOT scoped by user on purpose — the caller (``as_uninstall_automation``)
    scopes ownership itself by comparing the row's ``user_id`` to the
    caller's, so it can give a precise "belongs to a different account"
    message rather than a bare not-found.
    """
    if not script_id:
        raise ValueError("script_id is required")
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM automation_ledger WHERE script_id = ?",
            (script_id,),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_automations(user_id: str) -> list[dict[str, Any]]:
    """Every automation this user has installed, newest first.

    The forward-only inventory that ``as_list_installed_automations``
    surfaces — the only way to re-find script_ids that scrolled out of
    chat (S0-2), since ``drive.file`` cannot enumerate minted projects
    (S0-1).
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM automation_ledger WHERE user_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_automations(
    user_id: str, tool: str, container_id: str | None
) -> list[dict[str, Any]]:
    """Prior automations from ``tool`` on ``container_id`` for this user.

    The ``on_conflict`` lookup key (S0-3: re-running an installer mints a
    SECOND distinct project on the same container). ``replace`` uninstalls
    these first; ``skip`` returns the newest of these instead of minting.
    Newest first so ``skip`` reuses the most recent install.
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _connect() as conn:
        # ``container_id IS ?`` handles the standalone (NULL container)
        # case correctly — ``= NULL`` never matches in SQL, ``IS NULL`` does.
        rows = conn.execute(
            "SELECT * FROM automation_ledger "
            "WHERE user_id = ? AND tool = ? AND container_id IS ? "
            "ORDER BY created_at DESC, rowid DESC",
            (user_id, tool, container_id),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def forget_automation(script_id: str) -> bool:
    """Drop ``script_id``'s row. Returns True if a row was removed.

    Idempotent — forgetting an already-forgotten (or never-recorded)
    automation is a no-op that returns False. Called by uninstall after
    the deployments are removed + content disarmed, so a forgotten row
    reflects an automation that is genuinely no longer active.
    """
    if not script_id:
        raise ValueError("script_id is required")
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM automation_ledger WHERE script_id = ?",
            (script_id,),
        )
        return cur.rowcount > 0


def forget_all_for_user(user_id: str) -> int:
    """Drop every ledger row for ``user_id``. Returns the count removed.

    For consent revocation / account reset: when a user disconnects the
    connector, their automation inventory should not linger. (The minted
    projects themselves still exist in their Google account — the ledger
    forget only clears appscriptly's record, matching the honest
    "uninstall is partial" reality.)
    """
    if not user_id:
        raise ValueError("user_id is required")
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM automation_ledger WHERE user_id = ?",
            (user_id,),
        )
        return cur.rowcount
