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

from appscriptly import automation_ledger


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
