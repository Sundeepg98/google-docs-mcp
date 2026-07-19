"""Durable single-use nonce store on the ``/data`` SQLite pattern.

The in-process ``crypto.NonceStore`` forgets every consumed nonce on a
Fly deploy/restart, so a state or signed-URL nonce could be replayed
ONCE within its ≤10-min TTL after a restart. This subclass persists the
consumed set to SQLite on the ``/data`` volume (the same durable-storage
pattern as ``job_store``), so single-use survives restarts and holds
across instances that share the volume — strict replay protection with
no in-flight window lost to a deploy.

Deliberately a SEPARATE DB file from ``user_state.db`` / ``convert_jobs.db``:
consumed nonces are short-lived operational transients with their own
lifecycle (opportunistically purged on write), not per-user config, so
coupling their schema to the other stores would buy nothing.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .auth import default_data_dir
from .crypto import NonceStore

# Same per-path init-guard pattern as ``job_store``: the WAL transition
# needs an exclusive lock, so first-time init is serialized; later
# connects skip it (WAL mode persists in the DB file).
_initialized_paths: set[Path] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    """Resolve the SQLite file for the durable nonce store.

    Override with ``GOOGLE_DOCS_NONCE_STORE_PATH`` (tests / exotic mounts).
    Default sits next to ``user_state.db`` / ``convert_jobs.db`` on the
    data dir, which on Fly is the persistent ``/data`` volume — so
    consumed nonces survive deploys even though the process that consumed
    them does not.
    """
    override = os.environ.get("GOOGLE_DOCS_NONCE_STORE_PATH")
    if override:
        return Path(override)
    return default_data_dir() / "oauth_nonces.db"


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
                CREATE TABLE IF NOT EXISTS consumed_nonces (
                    nonce TEXT PRIMARY KEY,
                    exp   INTEGER NOT NULL
                )
                """
            )
        finally:
            conn.close()
        _initialized_paths.add(path)


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived autocommit connection to the initialized DB.

    Opened per operation in whatever thread calls it — no connection is
    shared across threads, so the default ``check_same_thread`` stands.
    ``busy_timeout`` lets concurrent writers wait for the WAL lock instead
    of failing fast, exactly like ``job_store``.
    """
    _ensure_initialized(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


class DurableNonceStore(NonceStore):
    """``NonceStore`` whose consumed set persists to SQLite on ``/data``.

    Drop-in for the in-process base: same ``consume(nonce, exp) -> bool``
    contract (first use ``True``, replay ``False``), so it substitutes
    anywhere a ``NonceStore`` is expected — the OAuth-callback state check
    and the signed-URL replay check both just hold a ``NonceStore``. The
    only difference is durability: a consumed nonce stays consumed across
    a deploy/restart, closing the ≤TTL post-restart replay window the
    in-process store leaves open.

    Atomicity comes from the ``nonce`` PRIMARY KEY: a concurrent second
    ``consume`` of the same nonce loses the INSERT with an
    ``IntegrityError`` and returns ``False`` — a compare-and-set without
    an explicit transaction.
    """

    def __init__(self) -> None:
        # No in-process dict/lock — the DB IS the store. Deliberately does
        # NOT call ``super().__init__()`` (which sets up the in-memory
        # ``_consumed`` map the base uses); this subclass overrides
        # ``consume`` entirely and never touches it.
        pass

    def consume(self, nonce: str, exp: int) -> bool:
        now = int(time.time())
        with _connect(db_path()) as conn:
            # Opportunistic purge of expired rows — bounds the table
            # without a janitor, mirroring job_store's purge-on-write.
            # Safe against replay: verify_state / verify_signed_params
            # reject an expired token BEFORE calling consume, so a row
            # this purge removes can never be validly redeemed anyway.
            conn.execute("DELETE FROM consumed_nonces WHERE exp <= ?", (now,))
            try:
                conn.execute(
                    "INSERT INTO consumed_nonces (nonce, exp) VALUES (?, ?)",
                    (nonce, exp),
                )
            except sqlite3.IntegrityError:
                return False
            return True
