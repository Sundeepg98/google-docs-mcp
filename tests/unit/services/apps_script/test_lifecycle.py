"""Tests for the automation lifecycle orchestration (Stream-2).

Covers ``services/apps_script/_lifecycle.py``:
  * ``mint_bound_automation`` writes a ledger row IN THE SAME FLOW as the
    deploy (the "a mint without a row is a bug" pin), records handler
    functions + content hash, and honors the on_conflict matrix
    (new / replace / skip).
  * ``uninstall_automation`` undeploys every versioned deployment (skipping
    @HEAD), disarms the content, forgets the ledger row, and reports the
    honest partial truth (the project file lingers); the already-gone path.
  * ``build_disarm_script`` self-disarms the known trigger handlers and is
    injection-safe.

The Apps Script REST primitives are monkeypatched with in-memory fakes so
the tests exercise the ORCHESTRATION + the real SQLite ledger, not the API
plumbing (that lives in test_api.py).
"""
from __future__ import annotations

import json

import pytest

from appscriptly import automation_ledger
from appscriptly.services.apps_script import _lifecycle
from appscriptly.services.apps_script._lifecycle import (
    _ledger_user_id,
    _manifest_scopes,
    _reactivation_function,
    build_disarm_script,
    mint_bound_automation,
    resolve_install_conflict,
    uninstall_automation,
    update_automation,
    validate_on_conflict,
)

_CREDS = object()  # opaque sentinel; the fakes ignore it


class _FakeApi:
    """Records calls to the Apps Script primitives + serves canned results."""

    def __init__(self) -> None:
        self._n = 0
        self.created: list[tuple] = []
        self.pushed: list[tuple] = []
        self.deployed: list[tuple] = []
        self.listed: list[str] = []
        self.deleted: list[tuple] = []
        # script_id -> deployments returned by list_deployments.
        self.deployments_by_script: dict[str, list[dict]] = {}
        # script_id -> content returned by get_project_content (for the
        # update path's scope-change detection). Absent -> empty-scope manifest.
        self.content_by_script: dict[str, dict] = {}
        # script_ids whose list_deployments / get_project_content raise 404.
        self.gone: set[str] = set()

    def create_bound_project(self, creds, container_id, name):
        self._n += 1
        sid = f"SID{self._n}"
        self.created.append((container_id, name, sid))
        return {"scriptId": sid}

    def set_project_content(self, creds, script_id, body, manifest):
        self.pushed.append((script_id, body, manifest))
        return {}

    def create_deployment(self, creds, script_id, description):
        self.deployed.append((script_id, description))
        return {"deploymentId": f"DEP-{script_id}"}

    def list_deployments(self, creds, script_id):
        self.listed.append(script_id)
        if script_id in self.gone:
            raise _http_error(404)
        return self.deployments_by_script.get(
            script_id,
            [{"deploymentId": f"DEP-{script_id}",
              "deploymentConfig": {"versionNumber": 1}}],
        )

    def delete_deployment(self, creds, script_id, deployment_id):
        self.deleted.append((script_id, deployment_id))

    def get_project_content(self, creds, script_id):
        if script_id in self.gone:
            raise _http_error(404)
        return self.content_by_script.get(
            script_id,
            {"files": [{"name": "appsscript", "type": "JSON",
                        "source": json.dumps({"oauthScopes": []})}]},
        )


def _http_error(status: int):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, s):
            self.status = s
            self.reason = "test"

    return HttpError(_Resp(status), b"boom")


@pytest.fixture
def fake_api(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr(_lifecycle, "_create_bound_project", api.create_bound_project)
    monkeypatch.setattr(_lifecycle, "_set_project_content", api.set_project_content)
    monkeypatch.setattr(_lifecycle, "_create_deployment", api.create_deployment)
    monkeypatch.setattr(_lifecycle, "_list_deployments", api.list_deployments)
    monkeypatch.setattr(_lifecycle, "_delete_deployment", api.delete_deployment)
    monkeypatch.setattr(_lifecycle, "_get_project_content", api.get_project_content)
    return api


def _content_with_scopes(scopes: list[str]) -> dict:
    """A projects.getContent payload whose manifest declares ``scopes``."""
    return {
        "files": [
            {"name": "appsscript", "type": "JSON",
             "source": json.dumps({"oauthScopes": scopes, "runtimeVersion": "V8"})},
            {"name": "Code", "type": "SERVER_JS", "source": "// old"},
        ]
    }


def _mint(**over):
    kwargs = dict(
        tool="as_install_sheet_menu",
        container_id="SHEET1",
        container_kind="sheets",
        project_name="proj",
        script_body="function onOpen(e){}",
        manifest_dict={"runtimeVersion": "V8", "timeZone": "Etc/UTC"},
    )
    kwargs.update(over)
    return mint_bound_automation(_CREDS, **kwargs)


# ---------------------------------------------------------------------
# mint_bound_automation — the ledger-row-per-mint pin
# ---------------------------------------------------------------------


def test_mint_writes_a_ledger_row_in_the_same_flow(fake_api):
    """A mint without a ledger row is a bug (S0-1): the row must exist the
    instant the mint returns."""
    result = _mint()
    assert result.reused is False
    row = automation_ledger.get_automation(result.script_id)
    assert row is not None
    assert row["tool"] == "as_install_sheet_menu"
    assert row["container_id"] == "SHEET1"
    assert row["container_kind"] == "sheets"
    assert row["deployment_id"] == f"DEP-{result.script_id}"
    assert row["project_url"].endswith(f"/d/{result.script_id}/edit")


def test_mint_records_handler_functions_for_self_disarm(fake_api):
    result = _mint(
        tool="as_install_sheet_dashboard",
        handler_functions=["refreshDashboard"],
    )
    row = automation_ledger.get_automation(result.script_id)
    assert row["handler_functions"] == ["refreshDashboard"]


def test_mint_records_a_content_hash(fake_api):
    result = _mint()
    row = automation_ledger.get_automation(result.script_id)
    assert row["content_hash"]  # non-empty
    # Same body+manifest hashes stably (the __plan__ echo is stripped).
    again = _lifecycle.compute_automation_hash(
        "function onOpen(e){}",
        {"runtimeVersion": "V8", "timeZone": "Etc/UTC", "__plan__": {"x": 1}},
    )
    assert again == row["content_hash"]


# ---------------------------------------------------------------------
# on_conflict matrix
# ---------------------------------------------------------------------


def test_on_conflict_new_mints_a_second_distinct_project(fake_api):
    r1 = _mint(on_conflict="new")
    r2 = _mint(on_conflict="new")
    assert r1.script_id != r2.script_id
    assert r2.reused is False and r2.replaced == 0
    # Both rows coexist (the S0-3 littering, now at least tracked).
    rows = automation_ledger.list_automations(_ledger_user_id())
    assert {r["script_id"] for r in rows} == {r1.script_id, r2.script_id}


def test_on_conflict_skip_returns_existing_without_a_new_mint(fake_api):
    r1 = _mint(on_conflict="new")
    creates_after_first = len(fake_api.created)
    r2 = _mint(on_conflict="skip")
    assert r2.reused is True
    assert r2.script_id == r1.script_id
    # No new project was created for the skip.
    assert len(fake_api.created) == creates_after_first
    # Still exactly one row for this (tool, container).
    found = automation_ledger.find_automations(
        _ledger_user_id(), "as_install_sheet_menu", "SHEET1"
    )
    assert len(found) == 1


def test_on_conflict_replace_uninstalls_prior_then_mints(fake_api):
    r1 = _mint(on_conflict="new")
    r2 = _mint(on_conflict="replace")
    assert r2.replaced == 1
    assert r2.script_id != r1.script_id
    # The prior was undeployed + disarmed as part of the replace.
    assert r1.script_id in fake_api.listed
    assert any(s == r1.script_id for s, _ in fake_api.deleted)
    # Disarm content was pushed to the prior (a second push beyond the
    # two mints' content pushes).
    disarm_pushes = [p for p in fake_api.pushed if p[0] == r1.script_id
                     and "UNINSTALLED" in p[1]]
    assert disarm_pushes
    # The prior row is gone; only the fresh one remains for this target.
    found = automation_ledger.find_automations(
        _ledger_user_id(), "as_install_sheet_menu", "SHEET1"
    )
    assert [r["script_id"] for r in found] == [r2.script_id]


def test_validate_on_conflict_rejects_unknown_value():
    with pytest.raises(ValueError, match="on_conflict"):
        validate_on_conflict("clobber")
    # The three valid values pass through unchanged.
    for v in ("new", "replace", "skip"):
        assert validate_on_conflict(v) == v


def test_resolve_install_conflict_new_is_a_noop(fake_api):
    skip_row, replaced = resolve_install_conflict(
        _CREDS, tool="t", container_id="C", on_conflict="new"
    )
    assert skip_row is None and replaced == 0


# ---------------------------------------------------------------------
# uninstall_automation — undeploy + disarm + forget + honesty
# ---------------------------------------------------------------------


def test_uninstall_undeploys_disarms_forgets_and_is_honest(fake_api):
    result = _mint()
    sid = result.script_id

    out = uninstall_automation(_CREDS, sid)

    assert out["status"] == "uninstalled"
    assert out["undeployed_count"] == 1
    assert out["content_disarmed"] is True
    assert out["ledger_forgotten"] is True
    # HONESTLY PARTIAL: the project file always lingers (S0-4).
    assert out["project_file_removed"] is False
    assert "Move to trash" in out["message"]
    assert out["project_url"].endswith(f"/d/{sid}/edit")
    # The deployment was deleted and an inert stub was pushed.
    assert (sid, f"DEP-{sid}") in fake_api.deleted
    assert any(s == sid and "UNINSTALLED" in body for s, body, _ in fake_api.pushed)
    # The automation left the inventory.
    assert automation_ledger.get_automation(sid) is None


def test_uninstall_skips_the_head_deployment(fake_api):
    result = _mint()
    sid = result.script_id
    # A @HEAD deployment (no versionNumber) plus a versioned one.
    fake_api.deployments_by_script[sid] = [
        {"deploymentId": "HEAD", "deploymentConfig": {}},
        {"deploymentId": "V1", "deploymentConfig": {"versionNumber": 3}},
    ]
    out = uninstall_automation(_CREDS, sid)
    # Only the versioned deployment is deleted; @HEAD is left alone.
    deleted_ids = [d for s, d in fake_api.deleted if s == sid]
    assert deleted_ids == ["V1"]
    assert out["undeployed_count"] == 1


def test_uninstall_already_gone_when_project_404s(fake_api):
    result = _mint()
    sid = result.script_id
    fake_api.gone.add(sid)  # list_deployments raises 404
    out = uninstall_automation(_CREDS, sid)
    assert out["status"] == "already_gone"
    assert out["content_disarmed"] is False
    # Still forgotten from the inventory + NO disarm push attempted (only
    # the mint's original content push is present, never a disarm stub).
    assert automation_ledger.get_automation(sid) is None
    assert not any(s == sid and "UNINSTALLED" in body
                   for s, body, _ in fake_api.pushed)


def test_uninstall_reads_handlers_from_ledger_for_the_disarm(fake_api):
    result = _mint(
        tool="as_install_sheet_dashboard",
        handler_functions=["refreshDashboard"],
    )
    sid = result.script_id
    uninstall_automation(_CREDS, sid)  # handler_functions=None -> from ledger
    disarm_body = next(body for s, body, _ in fake_api.pushed
                       if s == sid and "UNINSTALLED" in body)
    # The recorded handler is redefined as a self-disarmer.
    assert "function refreshDashboard(e) { __mcpDisarmAllTriggers(); }" in disarm_body


# ---------------------------------------------------------------------
# build_disarm_script — pure inert-body generator
# ---------------------------------------------------------------------


def test_disarm_body_has_noop_onopen_and_a_reaper():
    body = build_disarm_script()
    assert "function onOpen(e) {}" in body
    assert "function __mcpDisarmAllTriggers()" in body
    assert "ScriptApp.getProjectTriggers()" in body
    assert "ScriptApp.deleteTrigger(triggers[i])" in body


def test_disarm_body_self_disarms_each_known_handler():
    body = build_disarm_script(["handlerA", "handlerB"])
    assert "function handlerA(e) { __mcpDisarmAllTriggers(); }" in body
    assert "function handlerB(e) { __mcpDisarmAllTriggers(); }" in body


def test_disarm_body_skips_invalid_and_reserved_handler_names():
    body = build_disarm_script(
        ["good", "bad name", "on Open", "onOpen", "__mcpDisarmAllTriggers", "good"]
    )
    # The one valid, non-reserved, non-duplicate name is emitted.
    assert "function good(e) { __mcpDisarmAllTriggers(); }" in body
    # Injection / reserved / duplicate names are NOT emitted as handlers.
    assert "function bad name" not in body
    assert "function on Open" not in body
    # onOpen appears exactly once (the noop the stub owns), never as a
    # self-disarmer redefinition.
    assert body.count("function onOpen") == 1
    assert body.count("function good(e)") == 1


def test_disarm_manifest_declares_only_the_trigger_scope():
    scopes = _lifecycle._DISARM_MANIFEST["oauthScopes"]
    assert scopes == ["https://www.googleapis.com/auth/script.scriptapp"]


# ---------------------------------------------------------------------
# update_automation — consent-preserving in-place re-push (Stream 5)
# ---------------------------------------------------------------------


def _update(script_id, *, script_body="function onOpen(e){ /* v2 */ }",
            manifest_dict=None, handler_functions=("h",)):
    row = automation_ledger.get_automation(script_id)
    return update_automation(
        _CREDS,
        script_id,
        script_body=script_body,
        manifest_dict=manifest_dict
        or {"runtimeVersion": "V8", "timeZone": "Etc/UTC"},
        handler_functions=handler_functions,
        row=row,
    )


def test_update_repushes_new_content_and_refreshes_the_ledger_hash(fake_api):
    minted = _mint()
    sid = minted.script_id
    before = automation_ledger.get_automation(sid)["content_hash"]

    result = _update(sid, script_body="function onOpen(e){ /* CHANGED */ }")

    assert result.status == "updated"
    assert result.content_hash_before == before
    assert result.content_hash_after != before
    # The new content was pushed to the SAME project (a second push for sid).
    sid_pushes = [p for p in fake_api.pushed if p[0] == sid]
    assert len(sid_pushes) == 2  # mint + update
    # The ledger row now carries the new hash (UPSERT on the same script_id).
    row = automation_ledger.get_automation(sid)
    assert row["content_hash"] == result.content_hash_after
    assert row["script_id"] == sid  # never a new project


def test_update_is_a_noop_when_content_is_identical(fake_api):
    minted = _mint()
    sid = minted.script_id
    pushes_after_mint = len([p for p in fake_api.pushed if p[0] == sid])

    # Update with the SAME body + manifest the mint used.
    result = _update(
        sid,
        script_body="function onOpen(e){}",
        manifest_dict={"runtimeVersion": "V8", "timeZone": "Etc/UTC"},
    )

    assert result.status == "unchanged"
    assert result.content_hash_before == result.content_hash_after
    assert result.needs_reactivation is False
    # Nothing was re-pushed.
    assert len([p for p in fake_api.pushed if p[0] == sid]) == pushes_after_mint


def test_update_flags_needs_reactivation_on_a_scope_addition(fake_api):
    minted = _mint()
    sid = minted.script_id
    # The LIVE manifest currently declares only the calendar scope.
    fake_api.content_by_script[sid] = _content_with_scopes(
        ["https://www.googleapis.com/auth/calendar"]
    )
    # The update's manifest adds the tasks scope.
    result = _update(
        sid,
        script_body="function refresh(e){ /* v2 */ }",
        manifest_dict={
            "runtimeVersion": "V8",
            "timeZone": "Etc/UTC",
            "oauthScopes": [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/tasks",
            ],
        },
    )
    assert result.status == "updated"
    assert result.needs_reactivation is True
    assert result.added_scopes == ["https://www.googleapis.com/auth/tasks"]


def test_update_no_reactivation_when_scopes_unchanged(fake_api):
    minted = _mint()
    sid = minted.script_id
    fake_api.content_by_script[sid] = _content_with_scopes(
        ["https://www.googleapis.com/auth/calendar"]
    )
    result = _update(
        sid,
        script_body="function refresh(e){ /* only body changed */ }",
        manifest_dict={
            "runtimeVersion": "V8",
            "timeZone": "Etc/UTC",
            "oauthScopes": ["https://www.googleapis.com/auth/calendar"],
        },
    )
    assert result.status == "updated"
    assert result.needs_reactivation is False
    assert result.added_scopes == []


def test_update_preserves_created_at_and_refreshes_handlers(fake_api):
    minted = _mint(
        tool="as_install_sheet_dashboard", handler_functions=["oldHandler"]
    )
    sid = minted.script_id
    created_at = automation_ledger.get_automation(sid)["created_at"]

    _update(
        sid,
        script_body="function newHandler(e){ /* v2 */ }",
        handler_functions=["newHandler"],
    )

    row = automation_ledger.get_automation(sid)
    # Consent-preserving: same project, created_at preserved (UPSERT).
    assert row["created_at"] == created_at
    # Handler names refreshed so a later uninstall self-disarms the new one.
    assert row["handler_functions"] == ["newHandler"]


def test_manifest_scopes_extracts_and_tolerates_a_missing_manifest():
    content = _content_with_scopes(["a", "b"])
    assert _manifest_scopes(content) == {"a", "b"}
    # No manifest file -> empty set (the safe over-warn direction).
    assert _manifest_scopes({"files": [{"name": "Code", "type": "SERVER_JS"}]}) == set()
    # Unparseable manifest source -> empty set, not a crash.
    bad = {"files": [{"name": "appsscript", "type": "JSON", "source": "{not json"}]}
    assert _manifest_scopes(bad) == set()


def test_reactivation_function_prefers_installtrigger_then_first_function():
    assert _reactivation_function(
        "function installTrigger(){} function refresh(){}"
    ) == "installTrigger"
    assert _reactivation_function("function refreshDashboard(e){}") == "refreshDashboard"
    assert _reactivation_function("// no functions here") == "any function"
