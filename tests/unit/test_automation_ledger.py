"""Tests for ``appscriptly.automation_ledger`` (Stream-2 lifecycle).

The ledger is the ONLY discovery surface for minted automations (S0-1:
minted projects are invisible to drive.file). It is forward-only and its
integrity is load-bearing, so these pin: record/read round-trips,
upsert-on-script_id idempotency, the (tool, container) find used by
on_conflict, NULL-container handling, per-user isolation, and forget.

Isolation: the autouse ``isolated_db`` fixture (tests/conftest.py) points
``GOOGLE_DOCS_DATA_DIR`` at a per-test tmp dir, so ``db_path()`` resolves
to a fresh ``automation_ledger.db`` each test; conftest also clears the
per-path init guard.
"""
from __future__ import annotations

import json
import sqlite3

from appscriptly import automation_ledger

# The ORIGINAL (pre-S5) ledger schema, WITHOUT the recipe / params_json columns
# S5 adds. A test builds a DB with exactly this to prove the in-place migration
# upgrades an existing /data DB (created before this wave) safely.
_PRE_S5_CREATE = """
    CREATE TABLE automation_ledger (
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


def test_record_then_get_round_trips_and_parses_handlers():
    automation_ledger.record_automation(
        user_id="u1",
        script_id="S1",
        tool="as_install_sheet_dashboard",
        container_id="SHEET1",
        container_kind="sheets",
        deployment_id="D1",
        project_url="https://script.google.com/d/S1/edit",
        content_hash="hash1",
        handler_functions=["refreshDashboard"],
    )
    row = automation_ledger.get_automation("S1")
    assert row is not None
    assert row["user_id"] == "u1"
    assert row["tool"] == "as_install_sheet_dashboard"
    assert row["container_id"] == "SHEET1"
    assert row["container_kind"] == "sheets"
    assert row["deployment_id"] == "D1"
    # handler_functions is stored as JSON TEXT but read back as a list.
    assert row["handler_functions"] == ["refreshDashboard"]
    assert isinstance(row["created_at"], int)


def test_get_absent_returns_none():
    assert automation_ledger.get_automation("nope") is None


def test_record_is_upsert_keyed_on_script_id():
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="t", container_id="C1",
        container_kind="sheets", deployment_id="D1",
    )
    first = automation_ledger.get_automation("S1")
    assert first is not None
    # Re-record the SAME script_id (an idempotent re-install) updates the
    # row in place rather than duplicating it, and preserves created_at.
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="t", container_id="C1",
        container_kind="sheets", deployment_id="D2",
    )
    rows = automation_ledger.list_automations("u1")
    assert len(rows) == 1
    updated = rows[0]
    assert updated["deployment_id"] == "D2"
    assert updated["created_at"] == first["created_at"]  # preserved
    assert updated["updated_at"] >= first["updated_at"]


def test_list_is_newest_first_and_scoped_by_user():
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="t", container_id="C1",
        container_kind="sheets",
    )
    automation_ledger.record_automation(
        user_id="u1", script_id="S2", tool="t", container_id="C2",
        container_kind="sheets",
    )
    # A different user's automation must NOT leak into u1's inventory.
    automation_ledger.record_automation(
        user_id="u2", script_id="S3", tool="t", container_id="C3",
        container_kind="sheets",
    )
    rows = automation_ledger.list_automations("u1")
    ids = [r["script_id"] for r in rows]
    assert set(ids) == {"S1", "S2"}
    assert "S3" not in ids
    # Newest first: S2 was recorded after S1.
    assert ids[0] == "S2"


def test_find_automations_scopes_by_tool_and_container():
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_install_sheet_menu",
        container_id="SHEET1", container_kind="sheets",
    )
    automation_ledger.record_automation(
        user_id="u1", script_id="S2", tool="as_install_sheet_menu",
        container_id="SHEET2", container_kind="sheets",
    )
    automation_ledger.record_automation(
        user_id="u1", script_id="S3", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets",
    )
    found = automation_ledger.find_automations(
        "u1", "as_install_sheet_menu", "SHEET1"
    )
    assert [r["script_id"] for r in found] == ["S1"]
    # Different tool on the same container is a distinct automation.
    other = automation_ledger.find_automations(
        "u1", "as_install_sheet_dashboard", "SHEET1"
    )
    assert [r["script_id"] for r in other] == ["S3"]


def test_find_automations_matches_null_container():
    # A standalone web app records container_id=None (the SQL uses IS,
    # not =, so NULL matches NULL).
    automation_ledger.record_automation(
        user_id="u1", script_id="W1", tool="as_deploy_web_app",
        container_id=None, container_kind="webapp",
    )
    found = automation_ledger.find_automations("u1", "as_deploy_web_app", None)
    assert [r["script_id"] for r in found] == ["W1"]


def test_forget_removes_row_and_is_idempotent():
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="t", container_id="C1",
        container_kind="sheets",
    )
    assert automation_ledger.forget_automation("S1") is True
    assert automation_ledger.get_automation("S1") is None
    # Forgetting again is a no-op that returns False.
    assert automation_ledger.forget_automation("S1") is False


def test_forget_all_for_user_only_touches_that_user():
    for sid in ("S1", "S2"):
        automation_ledger.record_automation(
            user_id="u1", script_id=sid, tool="t", container_id=sid,
            container_kind="sheets",
        )
    automation_ledger.record_automation(
        user_id="u2", script_id="S3", tool="t", container_id="C3",
        container_kind="sheets",
    )
    removed = automation_ledger.forget_all_for_user("u1")
    assert removed == 2
    assert automation_ledger.list_automations("u1") == []
    # u2 is untouched.
    assert [r["script_id"] for r in automation_ledger.list_automations("u2")] == [
        "S3"
    ]


def test_exec_url_is_persisted_for_web_apps():
    automation_ledger.record_automation(
        user_id="u1", script_id="W1", tool="as_deploy_web_app",
        container_id="My webhook", container_kind="webapp",
        exec_url="https://script.google.com/macros/s/AKfy.../exec",
    )
    row = automation_ledger.get_automation("W1")
    assert row is not None
    assert row["exec_url"].endswith("/exec")


# ---------------------------------------------------------------------
# S5: recipe + params columns (deterministic update)
# ---------------------------------------------------------------------


def test_record_stores_and_reads_back_recipe_and_params():
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets",
        recipe="as_install_sheet_dashboard",
        recipe_params={"sheet_id": "SHEET1", "schedule": "daily", "hour": 9},
    )
    row = automation_ledger.get_automation("S1")
    assert row["recipe"] == "as_install_sheet_dashboard"
    assert json.loads(row["params_json"]) == {
        "sheet_id": "SHEET1", "schedule": "daily", "hour": 9,
    }


def test_record_without_recipe_reads_back_null():
    # A raw as_generate_bound_script / web-app mint records no recipe.
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_generate_bound_script",
        container_id="DOC1", container_kind="docs",
    )
    row = automation_ledger.get_automation("S1")
    assert row["recipe"] is None
    assert row["params_json"] is None


def test_upsert_without_recipe_preserves_the_recorded_recipe():
    # Initial mint records the recipe + params.
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets", deployment_id="D1",
        recipe="as_install_sheet_dashboard", recipe_params={"sheet_id": "SHEET1"},
    )
    # A later refresh that OMITS recipe/params (the caller-body update path)
    # must NOT clobber them to NULL - COALESCE preserves the recorded values.
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets", deployment_id="D2",
    )
    row = automation_ledger.get_automation("S1")
    assert row["deployment_id"] == "D2"  # the refresh landed
    assert row["recipe"] == "as_install_sheet_dashboard"  # preserved
    assert json.loads(row["params_json"]) == {"sheet_id": "SHEET1"}  # preserved


def test_upsert_with_new_params_restores_them():
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets",
        recipe="as_install_sheet_dashboard", recipe_params={"schedule": "daily"},
    )
    # A recipe regeneration with overrides re-stores the merged params.
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets",
        recipe="as_install_sheet_dashboard", recipe_params={"schedule": "weekly"},
    )
    row = automation_ledger.get_automation("S1")
    assert json.loads(row["params_json"]) == {"schedule": "weekly"}


def test_migration_upgrades_a_pre_s5_db_in_place():
    """An existing /data DB created before S5 (no recipe/params_json columns)
    gains them on first connect, and its old rows read the new columns as NULL."""
    path = automation_ledger.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_PRE_S5_CREATE)
    conn.execute(
        "INSERT INTO automation_ledger "
        "(script_id, user_id, tool, created_at, updated_at) "
        "VALUES ('OLD1', 'u1', 'as_install_sheet_menu', 1, 1)"
    )
    conn.commit()
    conn.close()
    # Nothing has initialized this path yet; clear the guard so the next op
    # runs _ensure_initialized -> the in-place migration.
    automation_ledger._initialized_paths.clear()

    row = automation_ledger.get_automation("OLD1")
    assert row is not None
    # The pre-existing row now carries the new columns, read back as NULL.
    assert row["recipe"] is None
    assert row["params_json"] is None
    # And the upgraded table accepts a new recipe-bearing row.
    automation_ledger.record_automation(
        user_id="u1", script_id="NEW1", tool="as_install_sheet_dashboard",
        container_id="S1", container_kind="sheets",
        recipe="as_install_sheet_dashboard", recipe_params={"sheet_id": "S1"},
    )
    new = automation_ledger.get_automation("NEW1")
    assert new["recipe"] == "as_install_sheet_dashboard"
    assert json.loads(new["params_json"]) == {"sheet_id": "S1"}


def test_migration_guard_is_idempotent():
    """_migrate_add_columns is safe to run repeatedly: a second pass finds the
    columns already present and is a no-op (no duplicate-column error)."""
    # Initialize a fresh DB via the normal path (schema already current).
    automation_ledger.record_automation(
        user_id="u1", script_id="S1", tool="t",
        container_id="C1", container_kind="sheets",
    )
    conn = sqlite3.connect(automation_ledger.db_path())
    try:
        # Running it twice more must not raise.
        automation_ledger._migrate_add_columns(conn)
        automation_ledger._migrate_add_columns(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(automation_ledger)")]
    finally:
        conn.close()
    # Exactly one of each new column (no duplicates from re-running).
    assert cols.count("recipe") == 1
    assert cols.count("params_json") == 1
