"""Durable convert-job rows for the /api/convert async job model (T1.1).

Why this exists: /api/convert used to be a synchronous multi-minute POST
bound to the client connection. A client read-timeout mid-conversion
silently lost the work AND burned the single-use signed-URL nonce (the
2026-07-08 field-feedback T1.1 failure, reproduced with timeout=(10,30)).
The job model decouples execution from the request: the handler creates
a job row here, spawns the work as an asyncio task, and the work
completes even if the client disconnects.

Storage mirrors ``user_store``'s SQLite patterns (same rationale: atomic
updates, stdlib, WAL for concurrent read/write, one file on the Fly
``/data`` volume) but in a SEPARATE DB file - jobs are operational
transients with their own lifecycle (opportunistically purged), not
per-user configuration, and coupling them into ``user_state.db`` would
tie schema migrations of the two stores together for no benefit.

**Deploy/restart semantics (the row must not lie).** A Fly deploy or
machine restart kills in-flight asyncio tasks; nothing marks the row.
Truth is restored by DERIVATION, not by writes: a row still in
``queued``/``running`` whose ``heartbeat_at`` is older than
``STALLED_AFTER_SECONDS`` reads as ``stalled`` (see ``derive_status``).
The client's recovery path is the request-fingerprint attach in the
convert route: re-POSTing the identical request within
``FINGERPRINT_ATTACH_WINDOW_SECONDS`` of job creation re-arms the SAME
job row instead of duplicating work or documents, so every previously
issued status URL keeps pointing at the live retry.

Known honest limitation: if the doomed process's converter thread had
already finished against Google (doc created) but the process died
before the row was marked done, a re-arm re-runs the conversion and can
produce a second document. The window is seconds wide and requires a
deploy to land inside it; the fingerprint model cannot see Google-side
effects it never recorded.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .auth import default_data_dir

_log = logging.getLogger("appscriptly.job_store")

# Terminal + live states persisted in the DB. ``stalled`` is NEVER
# persisted - it is derived at read time from heartbeat age so a killed
# process can't leave a lying row (it had no chance to write one).
VALID_STATUSES = ("queued", "running", "done", "error")

# A live row whose heartbeat is older than this reads as stalled. The
# runner heartbeats every ~30s (see http_server/jobs.py), so 120s means
# roughly four missed beats before we declare the process dead.
STALLED_AFTER_SECONDS = 120

# Retry-attach window: a request whose fingerprint matches a job created
# within this many seconds attaches to that job instead of creating a
# new one. Converts run 1-3 min typically; 15 min covers slow retries
# without keeping fingerprints sticky forever.
FINGERPRINT_ATTACH_WINDOW_SECONDS = 15 * 60

# Rows older than this are opportunistically purged on the next
# create_job call (any status - a week-old queued/running row is a
# fossil of a long-gone process). Results are meant to be collected
# within hours; a week is generous.
_PURGE_AFTER_SECONDS = 7 * 24 * 3600

# Same per-path init-guard pattern as user_store: WAL transition needs
# an exclusive lock, so first-time init is serialized; later connects
# skip it (WAL mode persists in the DB file).
_initialized_paths: set[Path] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    """Resolve the SQLite file path for the job store.

    Override with ``GOOGLE_DOCS_JOB_STORE_PATH`` (tests; exotic mounts).
    Default sits next to ``user_state.db`` on the data dir, which on
    Fly is the persistent ``/data`` volume - job rows survive deploys
    even though the in-flight tasks do not (that asymmetry is the whole
    point: the row outlives the process and derives ``stalled``).
    """
    override = os.environ.get("GOOGLE_DOCS_JOB_STORE_PATH")
    if override:
        return Path(override)
    return default_data_dir() / "convert_jobs.db"


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
                CREATE TABLE IF NOT EXISTS convert_jobs (
                    job_id       TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    fingerprint  TEXT NOT NULL,
                    status       TEXT NOT NULL
                        CHECK (status IN ('queued','running','done','error')),
                    created_at   INTEGER NOT NULL,
                    updated_at   INTEGER NOT NULL,
                    heartbeat_at INTEGER NOT NULL,
                    result_json  TEXT,
                    error_json   TEXT
                )
                """
            )
            # Fingerprint lookups are always "latest match inside the
            # attach window" - index (fingerprint, created_at) serves
            # the ORDER BY created_at DESC probe without a scan.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_convert_jobs_fingerprint "
                "ON convert_jobs (fingerprint, created_at)"
            )
        finally:
            conn.close()
        _initialized_paths.add(path)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection to the (initialized) DB and close it.

    Opened per operation in whatever thread calls it (event loop for
    status reads / heartbeats; ``asyncio.to_thread`` workers never touch
    it directly) - no connection is ever shared across threads, so the
    default ``check_same_thread`` stands. ``busy_timeout`` lets
    concurrent writers wait for the WAL lock instead of failing fast,
    exactly like ``user_store``.
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
    return {k: row[k] for k in row.keys()}


def create_job(user_id: str, fingerprint: str) -> str:
    """Insert a fresh ``queued`` row and return its job_id.

    Also opportunistically purges rows older than ``_PURGE_AFTER_SECONDS``
    so the table stays bounded without a separate janitor process.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if not fingerprint:
        raise ValueError("fingerprint is required")
    job_id = str(uuid.uuid4())
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "DELETE FROM convert_jobs WHERE created_at < ?",
            (now - _PURGE_AFTER_SECONDS,),
        )
        conn.execute(
            "INSERT INTO convert_jobs "
            "(job_id, user_id, fingerprint, status, created_at, updated_at, "
            "heartbeat_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (job_id, user_id, fingerprint, now, now, now),
        )
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM convert_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def find_attachable_job(fingerprint: str, user_id: str) -> dict[str, Any] | None:
    """Latest job with this fingerprint AND owner inside the attach window.

    The fingerprint already encodes the user identity (the convert route
    hashes it into the material), so the explicit ``user_id`` predicate
    is defense in depth: if a future refactor ever dropped the user from
    the hash material, attach still could not cross tenants.
    """
    if not user_id:
        raise ValueError("user_id is required")
    cutoff = int(time.time()) - FINGERPRINT_ATTACH_WINDOW_SECONDS
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM convert_jobs WHERE fingerprint = ? "
            "AND user_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fingerprint, user_id, cutoff),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def mark_running(job_id: str) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE convert_jobs SET status = 'running', updated_at = ?, "
            "heartbeat_at = ? WHERE job_id = ?",
            (now, now, job_id),
        )


def touch_heartbeat(job_id: str) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE convert_jobs SET heartbeat_at = ? WHERE job_id = ?",
            (now, job_id),
        )


def finish_done(job_id: str, result: dict[str, Any]) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE convert_jobs SET status = 'done', result_json = ?, "
            "error_json = NULL, updated_at = ?, heartbeat_at = ? "
            "WHERE job_id = ?",
            (json.dumps(result), now, now, job_id),
        )


def finish_error(job_id: str, http_status: int, payload: dict[str, Any]) -> None:
    """Record a classified failure.

    ``http_status`` + ``payload`` are exactly what the synchronous
    response path would have returned for this exception (the classifier
    lives in ``http_server/jobs.py``), so the status endpoint and a
    sync caller report one identical truth.
    """
    now = int(time.time())
    error_json = json.dumps({"http_status": http_status, "payload": payload})
    with _connect() as conn:
        conn.execute(
            "UPDATE convert_jobs SET status = 'error', error_json = ?, "
            "result_json = NULL, updated_at = ?, heartbeat_at = ? "
            "WHERE job_id = ?",
            (error_json, now, now, job_id),
        )


def rearm_job(job_id: str) -> None:
    """Reset a stalled row back to ``queued`` so a retry can re-run it.

    Only meaningful for rows whose process died (derived ``stalled``);
    the convert route never re-arms ``done``/``error`` rows. Reusing the
    SAME row (same job_id) is deliberate: every status URL issued for
    the original attempt keeps working across the retry.
    ``created_at`` is preserved so the attach window anchors to the
    FIRST creation, per the 15-minute contract.
    """
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE convert_jobs SET status = 'queued', result_json = NULL, "
            "error_json = NULL, updated_at = ?, heartbeat_at = ? "
            "WHERE job_id = ?",
            (now, now, job_id),
        )


def derive_status(row: dict[str, Any], now: int | None = None) -> str:
    """The status a caller should SEE, including the derived ``stalled``.

    ``queued``/``running`` with a heartbeat older than
    ``STALLED_AFTER_SECONDS`` reads as ``stalled``: the process that
    owned the task is gone (deploy/restart/crash) and had no chance to
    write the row. ``queued`` is included because a kill can land
    between row insert and task start; a fresh queued row (heartbeat
    just written) reads as queued, never stalled.
    """
    if now is None:
        now = int(time.time())
    status = row["status"]
    if status in ("queued", "running") and (
        now - int(row["heartbeat_at"]) > STALLED_AFTER_SECONDS
    ):
        return "stalled"
    return status


def result_dict(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("result_json")
    return json.loads(raw) if raw else None


def error_dict(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("error_json")
    return json.loads(raw) if raw else None
