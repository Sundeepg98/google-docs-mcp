"""Unit tests for ``appscriptly.job_store`` (T1.1 async job model).

Pure persistence semantics: row lifecycle, the fingerprint attach
window, the DERIVED ``stalled`` status (the row must not lie after a
deploy kills the process), re-arm behavior, and retention purge.

The autouse ``isolated_db`` fixture (conftest) points
``GOOGLE_DOCS_DATA_DIR`` at a per-test tmp dir, so ``job_store.db_path()``
resolves to a fresh ``convert_jobs.db`` per test; the local autouse
fixture below resets the module's per-path init guard.
"""
from __future__ import annotations

import time

import pytest

from appscriptly import job_store


@pytest.fixture(autouse=True)
def _reset_job_store_state():
    job_store._initialized_paths.clear()
    yield
    job_store._initialized_paths.clear()


def _set_row(job_id: str, **columns) -> None:
    """Directly mutate row columns (test-only time travel)."""
    sets = ", ".join(f"{col} = ?" for col in columns)
    with job_store._connect() as conn:
        conn.execute(
            f"UPDATE convert_jobs SET {sets} WHERE job_id = ?",
            [*columns.values(), job_id],
        )


def test_create_and_get_roundtrip():
    job_id = job_store.create_job("user-A", "fp-1")
    row = job_store.get_job(job_id)
    assert row is not None
    assert row["job_id"] == job_id
    assert row["user_id"] == "user-A"
    assert row["fingerprint"] == "fp-1"
    assert row["status"] == "queued"
    assert row["created_at"] == row["updated_at"] == row["heartbeat_at"]
    assert row["result_json"] is None and row["error_json"] is None
    assert job_store.get_job("no-such-job") is None


def test_create_job_requires_identity_and_fingerprint():
    with pytest.raises(ValueError):
        job_store.create_job("", "fp")
    with pytest.raises(ValueError):
        job_store.create_job("user-A", "")


def test_find_attachable_respects_15_minute_window():
    job_id = job_store.create_job("user-A", "fp-window")
    found = job_store.find_attachable_job("fp-window", "user-A")
    assert found is not None and found["job_id"] == job_id

    # Age the row past the attach window: no longer attachable.
    too_old = int(time.time()) - job_store.FINGERPRINT_ATTACH_WINDOW_SECONDS - 5
    _set_row(job_id, created_at=too_old)
    assert job_store.find_attachable_job("fp-window", "user-A") is None

    assert job_store.find_attachable_job("fp-never-seen", "user-A") is None


def test_find_attachable_returns_latest_match():
    old = job_store.create_job("user-A", "fp-multi")
    _set_row(old, created_at=int(time.time()) - 60)
    newer = job_store.create_job("user-A", "fp-multi")
    found = job_store.find_attachable_job("fp-multi", "user-A")
    assert found is not None and found["job_id"] == newer


def test_find_attachable_is_user_scoped_defense_in_depth():
    """The fingerprint hash already encodes the user, but the query
    ALSO predicates on user_id: even a (hypothetical) future refactor
    that dropped the user from the hash material could not attach one
    tenant to another tenant's job."""
    job_store.create_job("user-A", "fp-shared")
    assert job_store.find_attachable_job("fp-shared", "user-B") is None
    found = job_store.find_attachable_job("fp-shared", "user-A")
    assert found is not None and found["user_id"] == "user-A"
    with pytest.raises(ValueError):
        job_store.find_attachable_job("fp-shared", "")


def test_lifecycle_done_and_error_roundtrip():
    job_id = job_store.create_job("user-A", "fp-life")
    job_store.mark_running(job_id)
    row = job_store.get_job(job_id)
    assert row is not None and row["status"] == "running"

    result = {"doc_id": "D1", "url": "https://x", "tabs": []}
    job_store.finish_done(job_id, result)
    row = job_store.get_job(job_id)
    assert row is not None and row["status"] == "done"
    assert job_store.result_dict(row) == result
    assert job_store.error_dict(row) is None

    job_store.finish_error(job_id, 400, {"error": "boom"})
    row = job_store.get_job(job_id)
    assert row is not None and row["status"] == "error"
    assert job_store.error_dict(row) == {
        "http_status": 400, "payload": {"error": "boom"},
    }
    assert job_store.result_dict(row) is None


def test_derive_status_stalled_from_stale_heartbeat():
    job_id = job_store.create_job("user-A", "fp-stall")
    job_store.mark_running(job_id)
    row = job_store.get_job(job_id)
    assert row is not None
    # Fresh heartbeat: running reads as running.
    assert job_store.derive_status(row) == "running"

    stale = int(time.time()) - job_store.STALLED_AFTER_SECONDS - 1
    _set_row(job_id, heartbeat_at=stale)
    row = job_store.get_job(job_id)
    assert row is not None
    assert job_store.derive_status(row) == "stalled"

    # queued rows stall too (kill landed between insert and task start).
    _set_row(job_id, status="queued")
    row = job_store.get_job(job_id)
    assert row is not None
    assert job_store.derive_status(row) == "stalled"

    # Terminal states never stall, however old the heartbeat.
    job_store.finish_done(job_id, {"ok": True})
    _set_row(job_id, heartbeat_at=stale)
    row = job_store.get_job(job_id)
    assert row is not None
    assert job_store.derive_status(row) == "done"


def test_rearm_resets_to_queued_and_preserves_created_at():
    job_id = job_store.create_job("user-A", "fp-rearm")
    row = job_store.get_job(job_id)
    assert row is not None
    original_created = row["created_at"]

    job_store.mark_running(job_id)
    stale = int(time.time()) - job_store.STALLED_AFTER_SECONDS - 1
    _set_row(job_id, heartbeat_at=stale)

    job_store.rearm_job(job_id)
    row = job_store.get_job(job_id)
    assert row is not None
    assert row["status"] == "queued"
    # Heartbeat is fresh again (no longer derives stalled)...
    assert job_store.derive_status(row) == "queued"
    # ...and the attach window still anchors to the FIRST creation.
    assert row["created_at"] == original_created
    assert row["result_json"] is None and row["error_json"] is None


def test_create_job_purges_week_old_rows():
    fossil = job_store.create_job("user-A", "fp-fossil")
    _set_row(fossil, created_at=int(time.time()) - job_store._PURGE_AFTER_SECONDS - 10)
    job_store.create_job("user-A", "fp-new")
    assert job_store.get_job(fossil) is None
