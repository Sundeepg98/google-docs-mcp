"""Tests for ``as_check_activation`` (Stream 3 activation verification).

Two answer paths, discriminated by whether ``exec_url`` is supplied:
  * ``_check_webapp`` - GETs a web app's /exec and maps the WebAppHealth
    verdict to a tri-state ``activated`` (serving / needs_activation / gone /
    unknown).
  * ``_check_processes`` - reads execution history and judges whether the
    activation function has a COMPLETED run.

The internal helpers are tested directly (pure logic, no decorator); a few
tests drive the decorated tool to pin the dispatch + creds envelope.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import jsonschema
import pytest

from appscriptly import decorators
from appscriptly.services.apps_script import check_activation
from appscriptly.setup_apps_script import WebAppHealth
from appscriptly.tool_schemas import AS_CHECK_ACTIVATION_OUTPUT_SCHEMA

_EXEC_URL = "https://script.google.com/macros/s/DEP/exec"
_EDITOR_URL = "https://script.google.com/d/SID/edit"


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=[GAS_PROCESSES_SCOPE]) envelope does
    not launch real OAuth for the tool-level dispatch tests."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _proc(fn, status, start="2026-07-13T10:00:00Z"):
    """One simplified process row (the shape processes.py::_simplify emits)."""
    return {
        "project_name": "auto",
        "function_name": fn,
        "process_type": "EDITOR",
        "process_status": status,
        "start_time": start,
        "duration": "1.2s",
        "user_access_level": "OWNER",
    }


def _patch_listing(monkeypatch, processes):
    monkeypatch.setattr(
        check_activation,
        "list_script_processes",
        lambda creds, script_id, page_size=50: {
            "script_id": script_id,
            "processes": processes,
            "next_page_token": None,
            "count": len(processes),
        },
    )


# ---------------------------------------------------------------------
# Web-app path (_check_webapp)
# ---------------------------------------------------------------------


def test_webapp_healthy_is_activated(monkeypatch):
    monkeypatch.setattr(
        check_activation, "probe_webapp_health", lambda url, **kw: WebAppHealth.HEALTHY
    )
    res = check_activation._check_webapp(_EXEC_URL, "SID")
    assert res["method"] == "webapp_probe"
    assert res["activated"] is True
    assert res["exec_state"] == "serving"
    assert res["activation_url"] == _EDITOR_URL
    jsonschema.validate(res, AS_CHECK_ACTIVATION_OUTPUT_SCHEMA)


def test_webapp_consent_gated_is_not_activated(monkeypatch):
    monkeypatch.setattr(
        check_activation,
        "probe_webapp_health",
        lambda url, **kw: WebAppHealth.CONSENT_GATED,
    )
    res = check_activation._check_webapp(_EXEC_URL, "SID")
    assert res["activated"] is False
    assert res["exec_state"] == "needs_activation"
    assert "Allow" in res["message"]
    jsonschema.validate(res, AS_CHECK_ACTIVATION_OUTPUT_SCHEMA)


def test_webapp_gone_is_indeterminate(monkeypatch):
    monkeypatch.setattr(
        check_activation, "probe_webapp_health", lambda url, **kw: WebAppHealth.GONE
    )
    res = check_activation._check_webapp(_EXEC_URL, "SID")
    assert res["activated"] is None
    assert res["exec_state"] == "gone"


def test_webapp_unknown_is_indeterminate(monkeypatch):
    monkeypatch.setattr(
        check_activation,
        "probe_webapp_health",
        lambda url, **kw: WebAppHealth.UNKNOWN,
    )
    res = check_activation._check_webapp(_EXEC_URL, "SID")
    assert res["activated"] is None
    assert res["exec_state"] == "unknown"


def test_webapp_probe_uses_require_json_false(monkeypatch):
    """DISCRIMINATING: a user web app may return HTML/text or be doPost-only,
    so the probe MUST pass require_json=False (any 200 = ran). If it probed
    with the default require_json=True, a non-JSON 200 would false-positive
    as CONSENT_GATED - the exact bug this flag avoids."""
    captured = {}

    def _rec(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return WebAppHealth.HEALTHY

    monkeypatch.setattr(check_activation, "probe_webapp_health", _rec)
    check_activation._check_webapp(_EXEC_URL, "SID")
    assert captured["url"] == _EXEC_URL
    assert captured["kw"].get("require_json") is False


# ---------------------------------------------------------------------
# Process-history path (_check_processes)
# ---------------------------------------------------------------------


def test_process_completed_run_is_activated(monkeypatch):
    _patch_listing(monkeypatch, [_proc("installTrigger", "COMPLETED")])
    res = check_activation._check_processes(MagicMock(), "SID", "installTrigger")
    assert res["method"] == "process_history"
    assert res["activated"] is True
    assert res["activation_function"] == "installTrigger"
    assert res["last_status"] == "COMPLETED"
    assert res["last_run_time"] == "2026-07-13T10:00:00Z"
    jsonschema.validate(res, AS_CHECK_ACTIVATION_OUTPUT_SCHEMA)


def test_process_failed_run_is_not_activated(monkeypatch):
    _patch_listing(monkeypatch, [_proc("installTrigger", "FAILED")])
    res = check_activation._check_processes(MagicMock(), "SID", "installTrigger")
    assert res["activated"] is False
    assert res["last_status"] == "FAILED"
    assert "FAILED" in res["message"]


def test_process_running_is_indeterminate(monkeypatch):
    _patch_listing(monkeypatch, [_proc("installTrigger", "RUNNING")])
    res = check_activation._check_processes(MagicMock(), "SID", "installTrigger")
    assert res["activated"] is None
    assert res["last_status"] == "RUNNING"


def test_process_no_matching_run_is_not_activated(monkeypatch):
    """History exists but none of it is the activation function -> not yet."""
    _patch_listing(monkeypatch, [_proc("someOtherHandler", "COMPLETED")])
    res = check_activation._check_processes(MagicMock(), "SID", "installTrigger")
    assert res["activated"] is False
    assert res["last_status"] is None
    assert res["matched_processes"] == []
    assert "no run" in res["message"].lower()


def test_process_completed_wins_over_failed(monkeypatch):
    """DISCRIMINATING: a prior FAILED run does not veto a later COMPLETED one -
    a single COMPLETED run anywhere in history proves it activated."""
    _patch_listing(
        monkeypatch,
        [
            _proc("installTrigger", "FAILED", "2026-07-13T09:00:00Z"),
            _proc("installTrigger", "COMPLETED", "2026-07-13T10:00:00Z"),
        ],
    )
    res = check_activation._check_processes(MagicMock(), "SID", "installTrigger")
    assert res["activated"] is True
    assert res["last_status"] == "COMPLETED"


def test_process_default_scan_finds_known_activation_function(monkeypatch):
    """With no activation_function passed, the tool scans for ANY known
    activation function and resolves the name from the matched process."""
    _patch_listing(monkeypatch, [_proc("gradeResponses", "COMPLETED")])
    res = check_activation._check_processes(MagicMock(), "SID", None)
    assert res["activated"] is True
    assert res["activation_function"] == "gradeResponses"


def test_process_function_filter_is_discriminating(monkeypatch):
    """DISCRIMINATING: naming a function scopes the verdict to THAT function.
    History has installTrigger COMPLETED + renderFrames FAILED. Filtering on
    renderFrames must report NOT activated (its run FAILED), even though a
    different function COMPLETED - proving the filter is honored, not ignored."""
    processes = [
        _proc("installTrigger", "COMPLETED"),
        _proc("renderFrames", "FAILED"),
    ]
    _patch_listing(monkeypatch, processes)
    res = check_activation._check_processes(MagicMock(), "SID", "renderFrames")
    assert res["activated"] is False
    assert res["activation_function"] == "renderFrames"
    assert res["last_status"] == "FAILED"
    # And the un-filtered scan sees installTrigger's COMPLETED run -> live.
    _patch_listing(monkeypatch, processes)
    res_scan = check_activation._check_processes(MagicMock(), "SID", None)
    assert res_scan["activated"] is True
    assert res_scan["activation_function"] == "installTrigger"


# ---------------------------------------------------------------------
# Tool-level dispatch + envelope (the decorated as_check_activation)
# ---------------------------------------------------------------------


def test_tool_dispatches_to_webapp_when_exec_url_given(monkeypatch):
    monkeypatch.setattr(
        check_activation, "probe_webapp_health", lambda url, **kw: WebAppHealth.HEALTHY
    )
    res = check_activation.as_check_activation(
        script_id="SID", exec_url=_EXEC_URL
    )
    assert res["method"] == "webapp_probe"
    assert res["activated"] is True


def test_tool_dispatches_to_processes_without_exec_url(monkeypatch):
    _patch_listing(monkeypatch, [_proc("installTrigger", "COMPLETED")])
    res = check_activation.as_check_activation(
        script_id="SID", activation_function="installTrigger"
    )
    assert res["method"] == "process_history"
    assert res["activated"] is True


def test_tool_rejects_empty_script_id():
    with pytest.raises(ValueError, match="script_id cannot be empty"):
        check_activation.as_check_activation(script_id="   ")


def test_tool_is_registered_readonly():
    """as_check_activation must register (auto-discovery) and be read-only."""
    import asyncio

    from appscriptly.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "as_check_activation" in tools
