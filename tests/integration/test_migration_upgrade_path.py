"""Schema migration / upgrade-path coverage (v1.4.0b).

The user_state.db file lives on Fly's persistent volume — it survives
deploys. That means rows written by an OLDER version of the server
must keep working after the operator deploys a NEWER version. There's
no migration tool today; we rely on:

  - ``CREATE TABLE IF NOT EXISTS`` (idempotent on every connect)
  - ``UserState`` being ``TypedDict(total=False)`` (every field
    optional, callers tolerate absence)
  - ``get_state`` filtering out NULL columns so old rows look like
    sparse new rows

These tests pin those invariants in place. If a future PR adds a
required column or changes ``get_state``'s tolerance for NULLs, the
tests here turn red BEFORE the deploy breaks a live user.

Two flavors of "old row" simulated:

  S1. Pre-Apps-Script row — created when only google_creds_json was
      populated (the very first OAuth callback wrote this, before
      ``gdocs_setup_apps_script`` was ever run). Modern code path
      must surface a sensible "needs setup" gap, not crash.

  S2. Row written by an HYPOTHETICAL future-older schema that lacks
      newer columns entirely. Simulated by dropping the table and
      recreating it with a subset of columns, inserting a row, then
      letting modern ``_ensure_initialized`` run again. Modern code
      must not lose the row's data and must add the missing columns
      via subsequent writes without exploding on the NULL gaps.
"""
from __future__ import annotations

import sqlite3
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_user_store(tmp_path, monkeypatch):
    """Per-test SQLite file + clear the per-path init cache.

    The module caches ``_initialized_paths`` to skip re-running PRAGMA
    journal_mode=WAL on every connect. Tests that mutate the schema
    behind the module's back must clear this cache so the next call
    re-runs ``CREATE TABLE IF NOT EXISTS`` against the new state.
    """
    from appscriptly import user_store
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    user_store._initialized_paths.clear()
    yield db_file
    user_store._initialized_paths.clear()


# ----------------------------------------------------------------------
# S1: row from a pre-setup install — only google_creds_json populated
# ----------------------------------------------------------------------


def test_pre_setup_row_round_trips_through_get_state(isolated_user_store):
    """Use the modern API to write a credentials-only row, then read
    it back. This mirrors the state of a user who finished OAuth but
    never ran ``gdocs_setup_apps_script``.

    Property: ``apps_script_*`` columns are NULL in SQLite, and the
    returned dict must simply LACK those keys — not contain them with
    None values, which would break ``if "apps_script_url" in state``
    checks all over the codebase.
    """
    from appscriptly.user_store import get_state, save_state

    save_state("pre-setup-user", {"google_creds_json": '{"token": "x"}'})
    state = get_state("pre-setup-user")

    assert state["user_id"] == "pre-setup-user"
    assert state["google_creds_json"] == '{"token": "x"}'
    # Existence-based checks (the entire codebase relies on this shape):
    for col in (
        "apps_script_url", "apps_script_script_id",
        "apps_script_deployment_id", "apps_script_version_number",
        "apps_script_content_hash",
    ):
        assert col not in state, (
            f"NULL column {col!r} leaked into get_state's result — "
            f"this breaks `if {col!r} in state` checks downstream"
        )


def test_pre_setup_row_can_be_enriched_with_setup_columns(isolated_user_store):
    """The classic upgrade path: a user who originally only had creds
    runs setup, populating the Apps Script columns on the SAME row.
    save_state's merge semantics must preserve google_creds_json
    while adding the new fields.
    """
    from appscriptly.user_store import get_state, save_state

    save_state("upgrade-user", {"google_creds_json": '{"token": "creds"}'})
    save_state("upgrade-user", {
        "apps_script_url": "https://script.google.com/macros/s/X/exec",
        "apps_script_script_id": "S1",
        "apps_script_deployment_id": "D1",
        "apps_script_version_number": 1,
        "apps_script_content_hash": "abc",
    })

    state = get_state("upgrade-user")
    assert state["google_creds_json"] == '{"token": "creds"}', (
        "the initial credentials row was clobbered by the later setup "
        "write — merge semantics regression"
    )
    assert state["apps_script_url"] == "https://script.google.com/macros/s/X/exec"
    assert state["apps_script_version_number"] == 1


# ----------------------------------------------------------------------
# S2: row from a hypothetical older schema missing newer columns
# ----------------------------------------------------------------------


def _create_legacy_schema(db_file, *, columns: list[str]) -> None:
    """Build a user_state table with only ``columns`` populated.

    Simulates an older deploy whose CREATE TABLE listed a subset of
    today's columns. WE STILL include user_id / created_at / updated_at
    because those have always been there.
    """
    cols_ddl = ", ".join(f"{c} TEXT" for c in columns)
    conn = sqlite3.connect(db_file, isolation_level=None)
    try:
        conn.execute("DROP TABLE IF EXISTS user_state")
        conn.execute(
            "CREATE TABLE user_state ("
            "user_id TEXT PRIMARY KEY, "
            f"{cols_ddl}, "
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL"
            ")"
        )
    finally:
        conn.close()


def test_legacy_schema_with_missing_columns_still_reads_old_rows(
    isolated_user_store,
):
    """Drop the table and recreate it with ONLY ``google_creds_json``
    and ``apps_script_url`` (mimicking a much earlier deploy that
    didn't have apps_script_script_id / deployment_id yet). Insert a
    row directly, then call get_state via the modern API.

    The modern code path runs ``CREATE TABLE IF NOT EXISTS`` — which
    is a NO-OP because the table already exists, even with the old
    column subset. The legacy row must still come back through
    get_state without crashing.

    This pins down: SQLite's permissive ``SELECT *`` doesn't blow up
    against a narrower schema, and our row.keys() iteration only
    surfaces the columns that ACTUALLY exist.
    """
    _create_legacy_schema(
        isolated_user_store,
        columns=["google_creds_json", "apps_script_url"],
    )

    now = int(time.time())
    conn = sqlite3.connect(isolated_user_store, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO user_state "
            "(user_id, google_creds_json, apps_script_url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "legacy-user",
                '{"token": "legacy-tok"}',
                "https://script.google.com/macros/s/LEGACY/exec",
                now, now,
            ),
        )
    finally:
        conn.close()

    from appscriptly.user_store import get_state

    state = get_state("legacy-user")
    assert state["user_id"] == "legacy-user"
    assert state["google_creds_json"] == '{"token": "legacy-tok"}'
    assert state["apps_script_url"] == "https://script.google.com/macros/s/LEGACY/exec"
    # Columns the legacy schema didn't have must simply be absent —
    # NOT raise AttributeError / KeyError.
    assert "apps_script_script_id" not in state
    assert "apps_script_deployment_id" not in state


def test_get_state_on_fresh_schema_is_lazy_initialized(
    isolated_user_store, tmp_path, monkeypatch,
):
    """Fresh deployment with NO database file: the first call to any
    user_store function creates the file + schema + WAL mode. No
    operator setup ritual required.

    This is the "first deploy" property — Fly spins up the machine,
    user clicks OAuth, callback writes user_state.db on demand. If
    init were eager and the volume wasn't mounted yet, the first
    OAuth would fail.
    """
    nested = tmp_path / "nested" / "subdir" / "fresh_user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(nested))
    # Clear the path cache so the new path triggers real init.
    from appscriptly import user_store
    user_store._initialized_paths.clear()

    assert not nested.exists()

    from appscriptly.user_store import get_state
    assert get_state("never-seen-user") == {}
    assert nested.exists(), "first call did not create the DB file"

    # The created file must be a valid SQLite DB with WAL mode.
    conn = sqlite3.connect(nested, isolation_level=None)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", (
            f"first-call init did not set WAL mode (got {mode!r}) — "
            "concurrent OAuth callback + tool reads will block each other"
        )
        # The schema must contain at least the columns the modern code
        # path relies on.
        rows = conn.execute(
            "SELECT name FROM pragma_table_info('user_state')"
        ).fetchall()
        cols = {r[0] for r in rows}
    finally:
        conn.close()

    for required in (
        "user_id", "google_creds_json", "apps_script_url",
        "apps_script_script_id", "created_at", "updated_at",
    ):
        assert required in cols, (
            f"fresh-deploy schema is missing column {required!r}: {cols}"
        )
