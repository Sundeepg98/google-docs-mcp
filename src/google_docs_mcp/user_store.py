"""Per-user state for multi-tenant cloud MCP (v1.1+, v2.1 abstracted).

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

**v2.1 — StorageBackend abstraction.** The persistence calls are now
mediated by a thin ``StorageBackend`` Protocol. The default backend
remains ``SqliteBackend`` with identical on-disk semantics to v2.0a;
``InMemoryBackend`` is provided for test ergonomics (no fsync, no
WAL setup, faster). The public top-level functions
(``get_state`` / ``save_state`` / ``clear_state`` /
``save_credentials_json`` / ``google_creds_dict`` / ``db_path``) are
preserved unchanged — every existing caller works untouched. The
underscore-prefixed module attributes (``_initialized_paths``,
``_ensure_initialized``, ``_FIELD_VALIDATORS``, etc.) are also
preserved because the migration script + chaos harness + a few tests
reach into them; treating those as load-bearing internal API.

The whole point of v2.1 is to make the eventual Postgres migration
swap-the-backend-class instead of rewrite-every-call-site. The
``StorageBackend`` Protocol is what a hypothetical ``PostgresBackend``
must satisfy.
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
from typing import Any, Callable, Iterator, Protocol, runtime_checkable
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
#
# Stays at module level rather than inside SqliteBackend because the
# chaos harness, test fixtures, and migration script all reach in via
# ``user_store._initialized_paths.clear()`` to reset between test
# runs. Treating these as load-bearing internal API.
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


def assert_state_db_writable() -> None:
    """Fail loud at startup if the state DB can't be opened for WRITE.

    The in-process companion to entrypoint.sh's volume-ownership check.
    This is the SQLITE_READONLY incident's belt-and-suspenders guard:
    every per-user tool call opens ``user_state.db`` in WAL mode, and
    WAL init writes both the DB file AND the ``-wal``/``-shm`` sidecars
    in the parent directory. If the volume's files are owned by a
    different uid than the runtime user (the classic root-owned-volume-
    vs-non-root-process mismatch), that open raises
    ``sqlite3.OperationalError: attempt to write a readonly database``
    at REQUEST time — silently taking the whole tool surface offline
    for hours. We'd rather crash at boot, visibly, in the deploy logs.

    Does a REAL write probe (open WAL + a throwaway DDL in a rolled-back
    txn), not an ``os.access`` check — ``os.access`` lies for root and
    on some filesystems, and only a real sqlite open exercises the exact
    WAL-sidecar-creation path that fails in production.

    Raises ``RuntimeError`` (not the raw sqlite error) with an operator-
    actionable message. Call once at HTTP startup, before serving.
    """
    path = db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"State DB directory {path.parent} could not be created/accessed "
            f"({e}). On Fly this is the /data volume; check the mount is "
            "present and writable by the runtime user (uid 10001). "
            "entrypoint.sh normally reconciles this at boot."
        ) from e

    # Open exactly like _connect does (WAL), and force a write so the
    # readonly condition surfaces HERE rather than on the first tool call.
    conn = None
    try:
        conn = sqlite3.connect(path, isolation_level=None, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        # A transaction we immediately roll back: proves write capability
        # without mutating real data or requiring the schema to exist yet.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            f"State DB at {path} is NOT writable by the runtime user "
            f"({e}). This is the SQLITE_READONLY failure mode: the "
            "persistent volume's files are likely owned by a different "
            "uid than the server process (uid 10001). The whole tool "
            "surface would fail at request time, so we refuse to start. "
            "Fix: ensure entrypoint.sh's root-stage chown ran (the "
            "container must start as root and drop to app via setpriv); "
            "or manually 'fly ssh console' + "
            "'chown -R 10001:10001 /data'. See entrypoint.sh."
        ) from e
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------
# StorageBackend Protocol + implementations (v2.1)
# ---------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal storage interface the rest of the codebase depends on.

    A future ``PostgresBackend`` only needs to satisfy these four methods
    to swap in. Signatures match the historical top-level public functions
    exactly so the module-level facade can delegate trivially. Validation
    and merge semantics live in the facade (the validators are a
    cross-cutting concern, not backend-specific) — backend impls are
    pure persistence + per-row merge.
    """

    def init_schema(self) -> None:
        """Lazy first-call init. Idempotent. Must be safe under concurrency."""
        ...

    def get_state(self, user_id: str) -> dict[str, Any]:
        """Return the full row as a dict (no NULL/None values), or {} if absent."""
        ...

    def save_state(self, user_id: str, updates: dict[str, Any]) -> None:
        """Merge ``updates`` into the user's row.

        Inserts a new row when none exists (setting created_at +
        updated_at to ``now``). On update, bumps updated_at; preserves
        created_at. The facade is responsible for validating ``updates``
        and rejecting unknown columns BEFORE calling this method —
        backends trust their inputs.
        """
        ...

    def clear_state(self, user_id: str) -> None:
        """Delete the user's row. Idempotent — no error if absent."""
        ...


class SqliteBackend:
    """The canonical (and currently only production) backend.

    Behaviorally identical to pre-v2.1 ``user_store`` — same WAL setup,
    same per-path init guard, same idempotent ALTER TABLE for the
    v2.0a ``apps_script_hmac_key`` column add, same merge semantics on
    save.

    ``path_resolver`` is a thunk rather than a fixed Path so the
    backend re-reads ``GOOGLE_DOCS_USER_STORE_PATH`` on every call.
    Pre-v2.1 code did this implicitly via top-level ``db_path()`` calls
    in ``_connect``; preserving the behavior matters because test
    fixtures + the chaos harness set the env var AFTER importing the
    module.
    """

    def __init__(self, path_resolver: Callable[[], Path] = db_path) -> None:
        self._path_resolver = path_resolver

    def _path(self) -> Path:
        return self._path_resolver()

    def init_schema(self) -> None:
        """First-time init for the current resolved path: WAL mode + schema.

        Serialized under module-level ``_init_lock`` so concurrent callers
        don't race on the PRAGMA. Idempotent — subsequent calls fast-path
        through the set-membership check without grabbing the lock.

        Stays the inner-loop body of the module-level
        ``_ensure_initialized(path)`` so external code that reaches in
        (e.g. ``scripts/migrate_existing_users.py``) keeps working.
        """
        _ensure_initialized(self._path())

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection to the (already-initialized) DB.

        WAL mode is persistent in the DB file — once init_schema has
        set it, every subsequent open uses WAL automatically.
        ``busy_timeout=5000ms`` lets concurrent writers WAIT for the
        row-level lock rather than failing fast.
        """
        self.init_schema()
        conn = sqlite3.connect(self._path(), isolation_level=None, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def get_state(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_state WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return {}
        return {k: row[k] for k in row.keys() if row[k] is not None}

    def save_state(self, user_id: str, updates: dict[str, Any]) -> None:
        now = int(time.time())
        with self._connect() as conn:
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

    def clear_state(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))


class InMemoryBackend:
    """Process-local dict-backed storage. For tests; no I/O.

    Drop-in replacement for ``SqliteBackend``. Same merge semantics,
    same created_at/updated_at bookkeeping, same return shape from
    ``get_state``. Notable differences from SQLite:

    - No persistence: a fresh instance starts empty.
    - No WAL / locking concerns: a single ``threading.Lock`` guards
      every read-modify-write so concurrent test threads can't
      tear writes.
    - Doesn't enforce ``apps_script_hmac_key`` column existence — the
      facade's validator layer already gates field names via
      ``_PERSISTENT_FIELDS``, so a backend doesn't need a column
      catalogue.

    Use via ``set_backend(InMemoryBackend())`` in test setup;
    ``set_backend(SqliteBackend())`` to revert. The
    ``with_backend(...)`` context manager is the ergonomic version.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        # No-op: the dict is the schema.
        pass

    def get_state(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._rows.get(user_id)
            if row is None:
                return {}
            # Return a copy so callers mutating their dict don't
            # mutate persisted state — matches SqliteBackend's behavior
            # where the dict is built from a sqlite3.Row each call.
            # Also drop None values to match the SQLite "NULL columns
            # not in result" contract.
            return {k: v for k, v in row.items() if v is not None}

    def save_state(self, user_id: str, updates: dict[str, Any]) -> None:
        now = int(time.time())
        with self._lock:
            row = self._rows.get(user_id)
            if row is None:
                self._rows[user_id] = {
                    "user_id": user_id,
                    "created_at": now,
                    "updated_at": now,
                    **updates,
                }
            else:
                row.update(updates)
                row["updated_at"] = now

    def clear_state(self, user_id: str) -> None:
        with self._lock:
            self._rows.pop(user_id, None)


# Module-level default backend. SqliteBackend with the env-aware
# path resolver = exact pre-v2.1 behavior. Preserved intentionally
# so module-import-time test setup (auto-applied fixtures that
# reach into _backend, _initialized_paths.clear(), etc.) keeps
# working bit-for-bit.
#
# PR-Δ6 (Vercel pilot): operator entrypoints that need a different
# backend (the Vercel ``api/index.py`` reads STORAGE_BACKEND=vercel_kv)
# call ``init_default_backend_from_env()`` explicitly at startup,
# AFTER module import. Tests never call it — they rely on
# ``with_backend(InMemoryBackend())`` for explicit per-test
# backend control.
_backend: StorageBackend = SqliteBackend()
_backend_lock = threading.Lock()  # guards set_backend / with_backend


def init_default_backend_from_env() -> StorageBackend:
    """Resolve the operator-configured backend from STORAGE_BACKEND env var.

    Called once at process startup by operator entrypoints (Vercel's
    ``api/index.py``, stdio CLI's ``server.main``) to honor the
    deploy target's backend preference. Returns the resolved backend
    (already activated via ``set_backend``) for caller convenience.

    Tests do NOT call this — they want SqliteBackend by default and
    use ``with_backend(InMemoryBackend())`` for explicit overrides.

    Env-var matrix:
      - unset / ``sqlite``: SqliteBackend (default; no-op).
      - ``vercel_kv``: VercelKvBackend if KV_REST_API_URL +
        KV_REST_API_TOKEN are set; else SqliteBackend with WARNING.
      - unknown value: SqliteBackend with WARNING.

    See ``google_docs_mcp.storage.backend_selector.select_backend``
    for the resolution implementation.
    """
    # Lazy import — keeps the storage package off the import path
    # for the default-SqliteBackend case (which is every test +
    # every Fly deploy).
    from google_docs_mcp.storage.backend_selector import select_backend
    resolved = select_backend()
    set_backend(resolved)
    return resolved


def get_backend() -> StorageBackend:
    """Return the currently active backend. Tests + the with_backend
    context manager use this to introspect or save+restore."""
    return _backend


def set_backend(backend: StorageBackend) -> StorageBackend:
    """Replace the active backend. Returns the previous backend so
    callers can restore it. Thread-safe.

    Tests that want a clean in-memory store should call
    ``set_backend(InMemoryBackend())`` in setup and restore the
    returned previous backend in teardown — or use the ``with_backend``
    context manager which does both automatically.
    """
    global _backend
    with _backend_lock:
        previous = _backend
        _backend = backend
    return previous


@contextmanager
def with_backend(backend: StorageBackend) -> Iterator[StorageBackend]:
    """Temporarily swap the active backend within a ``with`` block.

    Example:

        from google_docs_mcp.user_store import InMemoryBackend, with_backend, save_state

        with with_backend(InMemoryBackend()):
            save_state("user-x", {"google_creds_json": "..."})
            # ... assertions against in-memory state ...
        # Outside the block, the default SqliteBackend is restored.
    """
    previous = set_backend(backend)
    try:
        yield backend
    finally:
        set_backend(previous)


# ---------------------------------------------------------------------
# Schema init (module-level: external callers reach in)
# ---------------------------------------------------------------------


def _ensure_initialized(path: Path) -> None:
    """First-time init for ``path``: set WAL mode + create schema.

    Serialized under ``_init_lock`` so concurrent callers don't race
    on the PRAGMA. Idempotent — subsequent calls fast-path through
    the set-membership check without grabbing the lock.

    Lives at module level (rather than inside SqliteBackend as a
    private method) because ``scripts/migrate_existing_users.py``
    reaches in via ``user_store._ensure_initialized(path)`` to ensure
    the column-add migration runs before the migration script does
    its own raw-SQL writes. Keeping the symbol where it has been
    since v1.1 means that script doesn't need to change.
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


# ---------------------------------------------------------------------
# Public facade — preserved 1:1 from pre-v2.1
# ---------------------------------------------------------------------


def get_state(user_id: str) -> UserState:
    """Fetch a user's full state. Returns ``{}`` for unknown users.

    Callers should treat the absence of a field the same as "not yet
    set" — e.g. a tool that needs ``apps_script_url`` and finds it
    missing should raise with a clear "run gdocs_setup_apps_script
    first" message.

    Delegates to the active backend (default: SqliteBackend). The
    drop-invalid-on-read pass (per ``_FIELD_VALIDATORS``) lives in
    the facade so it runs identically against every backend.
    """
    if not user_id:
        raise ValueError("user_id is required")

    result = _backend.get_state(user_id)
    if not result:
        return {}  # type: ignore[return-value]

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

    Validation (unknown-field check + per-field validators) runs in
    the facade BEFORE delegating to the backend — so an invalid call
    never reaches storage and all backends see clean inputs.
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
    # was previously set. Invalid non-None values raise; the backend
    # write below never happens.
    for col, value in updates.items():
        validator = _FIELD_VALIDATORS.get(col)
        if validator is not None and value is not None and not validator(value):
            raise ValueError(
                f"user_state field {col!r} failed validation: "
                f"{value!r} is not a valid value. See "
                f"user_store._FIELD_VALIDATORS."
            )

    _backend.save_state(user_id, updates)
    return get_state(user_id)


def clear_state(user_id: str) -> None:
    """Delete a user's row. Idempotent — no error if the row is absent.

    Use cases: user revokes consent in claude.ai's connector UI and
    we want to clean up; user manually invokes a reset tool;
    integration tests cleaning up between runs.
    """
    if not user_id:
        raise ValueError("user_id is required")
    _backend.clear_state(user_id)


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
