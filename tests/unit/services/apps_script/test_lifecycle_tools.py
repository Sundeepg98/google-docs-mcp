"""Tests for the lifecycle MCP tools + the installer -> ledger wiring.

  * ``as_list_installed_automations`` — the forward-only inventory (ledger
    read, per-user scoped, activation_model derived).
  * ``as_uninstall_automation`` — undeploy + disarm + forget through the
    tool boundary, with the cross-tenant guard + the not-in-inventory note.
  * Installer-boundary PINS: a real ``as_install_sheet_menu`` /
    ``as_deploy_web_app`` call writes a ledger row (the "every mint writes a
    row" invariant, proven end to end through the decorated tool).

The Apps Script primitives are faked (monkeypatch) so the tests exercise
the tool + orchestration + real ledger, not the Google API plumbing.
"""
from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from appscriptly import auth, automation_ledger, decorators
from appscriptly.services.apps_script import _lifecycle
from appscriptly.services.apps_script._lifecycle import _ledger_user_id
from appscriptly.services.apps_script.lifecycle_tools import (
    as_list_installed_automations,
    as_uninstall_automation,
)
from appscriptly.services.apps_script.sheet_menu import as_install_sheet_menu


@pytest.fixture(autouse=True)
def stub_creds(monkeypatch):
    """Stop the creds=True envelope from launching real OAuth."""
    creds = object()
    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: creds)
    return creds


class _FakeApi:
    def __init__(self) -> None:
        self._n = 0
        self.created: list = []
        self.pushed: list = []
        self.deleted: list = []

    def create_bound_project(self, creds, container_id, name):
        self._n += 1
        sid = f"SID{self._n}"
        self.created.append(sid)
        return {"scriptId": sid}

    def set_project_content(self, creds, script_id, body, manifest):
        self.pushed.append((script_id, body))
        return {}

    def create_deployment(self, creds, script_id, description):
        return {"deploymentId": f"DEP-{script_id}"}

    def list_deployments(self, creds, script_id):
        return [{"deploymentId": f"DEP-{script_id}",
                 "deploymentConfig": {"versionNumber": 1}}]

    def delete_deployment(self, creds, script_id, deployment_id):
        self.deleted.append((script_id, deployment_id))


@pytest.fixture
def fake_api(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr(_lifecycle, "_create_bound_project", api.create_bound_project)
    monkeypatch.setattr(_lifecycle, "_set_project_content", api.set_project_content)
    monkeypatch.setattr(_lifecycle, "_create_deployment", api.create_deployment)
    monkeypatch.setattr(_lifecycle, "_list_deployments", api.list_deployments)
    monkeypatch.setattr(_lifecycle, "_delete_deployment", api.delete_deployment)
    return api


# ---------------------------------------------------------------------
# as_list_installed_automations
# ---------------------------------------------------------------------


def test_list_is_empty_for_a_fresh_user():
    out = as_list_installed_automations()
    assert out == {"automations": [], "count": 0}


def test_list_returns_recorded_automations_with_activation_model():
    me = _ledger_user_id()
    automation_ledger.record_automation(
        user_id=me, script_id="S1", tool="as_install_sheet_dashboard",
        container_id="SHEET1", container_kind="sheets", deployment_id="D1",
        project_url="https://script.google.com/d/S1/edit",
        handler_functions=["refreshDashboard"],
    )
    automation_ledger.record_automation(
        user_id=me, script_id="W1", tool="as_deploy_web_app",
        container_id="My hook", container_kind="webapp",
        exec_url="https://script.google.com/macros/s/AK/exec",
    )
    out = as_list_installed_automations()
    assert out["count"] == 2
    by_id = {a["script_id"]: a for a in out["automations"]}
    assert by_id["S1"]["activation_model"] == "scheduled_trigger"
    assert by_id["S1"]["handler_functions"] == ["refreshDashboard"]
    assert by_id["W1"]["activation_model"] == "web_app"
    assert by_id["W1"]["exec_url"].endswith("/exec")
    # Internal columns are not leaked to the caller.
    assert "user_id" not in by_id["S1"]


def test_list_only_shows_the_callers_own_automations():
    automation_ledger.record_automation(
        user_id="somebody-else", script_id="S9", tool="as_install_sheet_menu",
        container_id="X", container_kind="sheets",
    )
    out = as_list_installed_automations()
    assert out["count"] == 0


# ---------------------------------------------------------------------
# as_uninstall_automation
# ---------------------------------------------------------------------


def test_uninstall_tool_undeploys_and_forgets(fake_api):
    me = _ledger_user_id()
    automation_ledger.record_automation(
        user_id=me, script_id="S1", tool="as_install_sheet_menu",
        container_id="SHEET1", container_kind="sheets", deployment_id="DEP-S1",
    )
    out = as_uninstall_automation(script_id="S1")
    assert out["status"] == "uninstalled"
    assert out["project_file_removed"] is False
    assert ("S1", "DEP-S1") in fake_api.deleted
    assert automation_ledger.get_automation("S1") is None


def test_uninstall_tool_refuses_cross_tenant(fake_api):
    automation_ledger.record_automation(
        user_id="a-different-account", script_id="S1",
        tool="as_install_sheet_menu", container_id="SHEET1",
        container_kind="sheets",
    )
    with pytest.raises(ToolError, match="different account"):
        as_uninstall_automation(script_id="S1")
    # The other tenant's row is untouched.
    assert automation_ledger.get_automation("S1") is not None


def test_uninstall_tool_flags_a_script_not_in_inventory(fake_api):
    out = as_uninstall_automation(script_id="UNKNOWN")
    assert "note" in out
    assert "not in your appscriptly inventory" in out["note"]


def test_uninstall_tool_rejects_blank_script_id():
    with pytest.raises(ValueError, match="script_id cannot be empty"):
        as_uninstall_automation(script_id="   ")


# ---------------------------------------------------------------------
# Installer-boundary pins: a real install writes a ledger row
# ---------------------------------------------------------------------


def test_install_sheet_menu_writes_a_ledger_row(fake_api):
    """End-to-end through the decorated tool: installing writes the row."""
    result = as_install_sheet_menu(
        sheet_id="SHEET1",
        menu_title="Tools",
        items=[{"label": "Go", "function_name": "go", "function_body": ""}],
    )
    assert result["reused_existing"] is False
    row = automation_ledger.get_automation(result["script_id"])
    assert row is not None
    assert row["tool"] == "as_install_sheet_menu"
    assert row["container_id"] == "SHEET1"


def test_install_sheet_menu_skip_returns_existing(fake_api):
    first = as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Tools",
        items=[{"label": "Go", "function_name": "go", "function_body": ""}],
    )
    creates = len(fake_api.created)
    second = as_install_sheet_menu(
        sheet_id="SHEET1", menu_title="Tools",
        items=[{"label": "Go", "function_name": "go", "function_body": ""}],
        on_conflict="skip",
    )
    assert second["reused_existing"] is True
    assert second["script_id"] == first["script_id"]
    assert len(fake_api.created) == creates  # no second mint


def test_deploy_web_app_writes_a_ledger_row(monkeypatch):
    from appscriptly.services.gas_deploy import tools as gas_tools
    from appscriptly.services.gas_deploy.api import WebAppDeployment
    from appscriptly.services.apps_script.lifecycle_tools import (
        as_list_installed_automations as _list,
    )

    def _fake_deploy(creds, *, script_body, title, execute_as, access):
        return WebAppDeployment(
            script_id="WSID", deployment_id="WDEP", version=1,
            url="https://script.google.com/macros/s/AK/exec",
        )

    monkeypatch.setattr(gas_tools, "_deploy_web_app_project", _fake_deploy)

    result = gas_tools.as_deploy_web_app(
        script_body="function doGet(e){ return ContentService.createTextOutput('ok'); }",
        title="My hook",
        access="MYSELF",  # avoids the ANYONE_ANONYMOUS HMAC-guard requirement
    )
    assert result["script_id"] == "WSID"
    row = automation_ledger.get_automation("WSID")
    assert row is not None
    assert row["tool"] == "as_deploy_web_app"
    assert row["container_kind"] == "webapp"
    assert row["exec_url"].endswith("/exec")
    # And it shows up in the inventory as a web app.
    listed = _list()
    assert any(a["script_id"] == "WSID" and a["activation_model"] == "web_app"
               for a in listed["automations"])


def test_deploy_web_app_rejects_skip(monkeypatch):
    from appscriptly.services.gas_deploy import tools as gas_tools

    with pytest.raises(ToolError, match="skip.*not supported"):
        gas_tools.as_deploy_web_app(
            script_body="function doGet(e){}",
            title="My hook",
            access="MYSELF",
            on_conflict="skip",
        )
