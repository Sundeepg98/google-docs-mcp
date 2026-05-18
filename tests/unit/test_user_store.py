"""Per-user state storage tests.

Covers the v1.1 multi-tenant storage abstraction. Guards against:
- silent data loss on merge-update (partial save blowing away other fields)
- cross-user leakage (one user's row visible to another)
- timestamp lies (created_at mutated, updated_at not bumped)
- typos in field names silently writing nothing
- schema not being auto-initialized on fresh deployments
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point user_store at a per-test SQLite file so tests don't bleed."""
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    # Also override data_dir so default_data_dir() doesn't touch the
    # real ~/.google-docs-mcp during test runs.
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    yield db_file


def test_get_state_returns_empty_dict_for_unknown_user():
    from google_docs_mcp.user_store import get_state
    assert get_state("user-does-not-exist") == {}


def test_save_then_get_roundtrip():
    from google_docs_mcp.user_store import get_state, save_state

    save_state(
        "user-1",
        {
            "apps_script_url": "https://script.google.com/macros/s/ABC/exec",
            "apps_script_script_id": "SCRIPT_ID_1",
        },
    )
    state = get_state("user-1")

    assert state["user_id"] == "user-1"
    assert state["apps_script_url"] == "https://script.google.com/macros/s/ABC/exec"
    assert state["apps_script_script_id"] == "SCRIPT_ID_1"
    assert isinstance(state["created_at"], int)
    assert isinstance(state["updated_at"], int)


def test_save_merges_existing_state_not_overwrites():
    """The killer guard: a partial update must NOT erase other fields."""
    from google_docs_mcp.user_store import get_state, save_state

    save_state("user-2", {"apps_script_url": "URL_V1", "apps_script_script_id": "S1"})
    save_state("user-2", {"apps_script_deployment_id": "D1"})  # partial!

    state = get_state("user-2")
    assert state["apps_script_url"] == "URL_V1", (
        "second save() with no apps_script_url field erased the first one — "
        "merge semantics are broken"
    )
    assert state["apps_script_script_id"] == "S1"
    assert state["apps_script_deployment_id"] == "D1"


def test_save_preserves_created_at_but_bumps_updated_at():
    from google_docs_mcp.user_store import get_state, save_state

    save_state("user-3", {"apps_script_url": "URL"})
    first = get_state("user-3")

    # Sleep just enough to guarantee a different unix timestamp.
    time.sleep(1.05)
    save_state("user-3", {"apps_script_script_id": "S2"})
    second = get_state("user-3")

    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]


def test_clear_removes_state():
    from google_docs_mcp.user_store import clear_state, get_state, save_state

    save_state("user-4", {"apps_script_url": "URL"})
    assert get_state("user-4") != {}

    clear_state("user-4")
    assert get_state("user-4") == {}


def test_clear_nonexistent_user_is_noop():
    """No exception when clearing a row that doesn't exist."""
    from google_docs_mcp.user_store import clear_state
    clear_state("user-never-existed")  # must not raise


def test_multiple_users_isolated():
    from google_docs_mcp.user_store import get_state, save_state

    save_state("alice", {"apps_script_url": "ALICE_URL"})
    save_state("bob", {"apps_script_url": "BOB_URL"})

    assert get_state("alice")["apps_script_url"] == "ALICE_URL"
    assert get_state("bob")["apps_script_url"] == "BOB_URL"


def test_unknown_field_raises_loudly():
    """Typos in field names must surface immediately, not silently no-op.

    Without this guard, ``save_state(uid, {"apps_script_ulr": ...})``
    would write nothing useful and you'd debug the consumer for hours.
    """
    from google_docs_mcp.user_store import save_state
    with pytest.raises(ValueError, match="Unknown user_state fields"):
        save_state("user-5", {"appz_scripit_url": "typo"})


def test_empty_user_id_rejected():
    from google_docs_mcp.user_store import clear_state, get_state, save_state
    for fn_call in (
        lambda: get_state(""),
        lambda: save_state("", {"apps_script_url": "x"}),
        lambda: clear_state(""),
    ):
        with pytest.raises(ValueError, match="user_id is required"):
            fn_call()


def test_db_initialized_lazily_on_fresh_deployment(tmp_path, monkeypatch):
    """No setup ritual required — first call creates the schema."""
    fresh_db = tmp_path / "nested" / "subdir" / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(fresh_db))
    assert not fresh_db.exists()

    from google_docs_mcp.user_store import get_state
    get_state("any-user")

    assert fresh_db.exists()


def test_google_creds_dict_parses_when_present():
    from google_docs_mcp.user_store import (
        google_creds_dict, get_state, save_state,
    )
    creds_json = json.dumps(
        {"token": "abc", "refresh_token": "xyz", "scopes": ["docs"]}
    )
    save_state("user-6", {"google_creds_json": creds_json})

    parsed = google_creds_dict(get_state("user-6"))
    assert parsed == {"token": "abc", "refresh_token": "xyz", "scopes": ["docs"]}


def test_google_creds_dict_returns_none_when_absent():
    from google_docs_mcp.user_store import google_creds_dict
    assert google_creds_dict({}) is None
    assert google_creds_dict({"apps_script_url": "URL"}) is None


def test_concurrent_writes_to_different_users_dont_corrupt():
    """SQLite WAL + per-connection isolation should handle parallel writes
    to distinct user_ids cleanly. If this fails, the storage layer is
    not multi-tenant-safe."""
    from google_docs_mcp.user_store import get_state, save_state

    def worker(uid: str) -> None:
        for i in range(10):
            save_state(uid, {"apps_script_url": f"{uid}_url_{i}"})

    threads = [
        threading.Thread(target=worker, args=(f"user-thread-{i}",))
        for i in range(5)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    for i in range(5):
        uid = f"user-thread-{i}"
        state = get_state(uid)
        assert state["apps_script_url"] == f"{uid}_url_9"  # last write wins
