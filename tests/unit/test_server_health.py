"""T1.2 (2026-07-10): server_health — the three-layer health report.

The tool must NEVER raise for auth problems and NEVER start an
interactive consent flow — it reports. These tests drive the decision
tree through monkeypatched seams:

- ``_peek_credentials_non_interactive`` — the no-consent creds resolver
- ``_read_runtime_state`` — the per-identity install ledger read
- ``probe_webapp_health`` — the /exec liveness classifier
- the Google API round-trips — via the InMemory client port

Every returned payload is validated against the declared
``SERVER_HEALTH_OUTPUT_SCHEMA``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import jsonschema
import pytest
from googleapiclient.errors import HttpError

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.admin import tools as admin_tools
from appscriptly.setup_apps_script import WebAppHealth
from appscriptly.tool_schemas import SERVER_HEALTH_OUTPUT_SCHEMA

_EXEC_URL = "https://script.google.com/macros/s/DEPLOY123/exec"


class _Resp(dict):
    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.reason = "Synthetic"


def _http_error(status: int, content: bytes = b"") -> HttpError:
    return HttpError(
        resp=_Resp(status),
        content=content,
        uri="https://script.googleapis.com/v1/projects/S1",
    )


APPS_SCRIPT_DISABLED_CONTENT = (
    b'{"error": {"code": 403, "message": "User has not enabled the Apps '
    b"Script API. Enable it by visiting "
    b'https://script.google.com/home/usersettings then retry.",'
    b' "status": "PERMISSION_DENIED"}}'
)


@pytest.fixture
def google_stubs():
    """drive v3 (about.get ok) + script v1 (projects.get ok) stubs."""
    drive = MagicMock(name="drive-v3-stub")
    drive.about().get().execute.return_value = {"user": {"emailAddress": "u@x"}}
    script = MagicMock(name="script-v1-stub")
    script.projects().get().execute.return_value = {"scriptId": "S1"}
    return drive, script


def _run_health(
    monkeypatch,
    *,
    creds,
    peek_status="ok",
    peek_detail=None,
    state=("S1", _EXEC_URL),
    probe=WebAppHealth.HEALTHY,
    stubs=None,
):
    monkeypatch.setattr(
        admin_tools,
        "_peek_credentials_non_interactive",
        lambda: (creds, peek_status, peek_detail),
    )
    monkeypatch.setattr(admin_tools, "_read_runtime_state", lambda: state)
    probe_calls: list[str] = []

    def fake_probe(url):
        probe_calls.append(url)
        return probe

    monkeypatch.setattr(admin_tools, "probe_webapp_health", fake_probe)
    registry = {}
    if stubs is not None:
        drive, script = stubs
        registry = {("drive", "v3"): drive, ("script", "v1"): script}
    with with_google_api_client(InMemoryGoogleAPIClient(registry)):
        result = admin_tools.server_health()
    jsonschema.validate(result, SERVER_HEALTH_OUTPUT_SCHEMA)
    return result, probe_calls


def test_healthy_end_to_end_reports_serving(monkeypatch, google_stubs):
    result, probe_calls = _run_health(
        monkeypatch, creds=MagicMock(name="creds"), stubs=google_stubs
    )
    assert result["server"] == "ok"
    assert result["google_api"] == "ok"
    rt = result["automation_runtime"]
    assert rt["installed"] is True
    assert rt["exec"] == "serving"
    assert rt["remediation_url"] is None
    assert probe_calls == [_EXEC_URL]


def test_every_payload_states_what_the_runtime_gates(monkeypatch, google_stubs):
    """R3 (retest 2): needs_activation was read as convert-blocking (it
    is not - the convert path is pure REST since #222, proven by 7/7
    converts succeeding while the runtime needed activation). Every
    automation_runtime payload carries a ``gates`` field naming the
    as_*-only blast radius."""
    result, _ = _run_health(
        monkeypatch, creds=MagicMock(name="creds"), stubs=google_stubs
    )
    gates = result["automation_runtime"]["gates"]
    assert "as_*" in gates
    assert "unaffected" in gates
    # PR-D: the gating set was derived from code and is EMPTY - the
    # field must say so, not merely scope the blast radius to as_*.
    assert "nothing at runtime" in gates
    # Operator decision (2026-07-10, brief header): the web app is KEPT
    # as dormant infrastructure - the field carries that framing so the
    # empty gating set reads as deliberate, not as an obsolete leftover.
    assert "dormant" in gates

    # The not_installed early return carries it too.
    result, _ = _run_health(
        monkeypatch,
        creds=None,
        peek_status="unauthorized",
        peek_detail="no token",
        state=(None, None),
    )
    assert "as_*" in result["automation_runtime"]["gates"]


def test_needs_activation_detail_prevents_false_abort(
    monkeypatch, google_stubs
):
    """R3 (sharpened by PR-D): the needs_activation detail itself must
    prevent the false-abort. PR-D derived the gating set as EMPTY, so
    the detail now states no tool is blocked at all."""
    result, _ = _run_health(
        monkeypatch,
        creds=MagicMock(name="creds"),
        stubs=google_stubs,
        probe=WebAppHealth.CONSENT_GATED,
    )
    rt = result["automation_runtime"]
    assert rt["exec"] == "needs_activation"
    assert "No tool is blocked" in rt["detail"]


def test_not_installed_short_circuits_before_any_probe(monkeypatch):
    result, probe_calls = _run_health(
        monkeypatch,
        creds=None,
        peek_status="unauthorized",
        peek_detail="no token",
        state=(None, None),
    )
    assert result["google_api"] == "unauthorized"
    rt = result["automation_runtime"]
    assert rt["installed"] is False
    assert rt["exec"] == "not_installed"
    assert rt["remediation_url"] is None
    assert "as_install_automation" in rt["detail"]
    assert probe_calls == []


def test_consent_gated_probe_reports_needs_activation_with_editor_url(
    monkeypatch, google_stubs
):
    """The 403 door page (CONSENT_GATED probe) = the one-time
    interactive activation is missing; remediation is the script
    editor's Run + Allow."""
    result, _ = _run_health(
        monkeypatch,
        creds=MagicMock(name="creds"),
        probe=WebAppHealth.CONSENT_GATED,
        stubs=google_stubs,
    )
    rt = result["automation_runtime"]
    assert rt["installed"] is True
    assert rt["exec"] == "needs_activation"
    assert rt["remediation_url"] == "https://script.google.com/d/S1/edit"
    assert "Allow" in rt["detail"]


def test_gone_probe_reports_not_installed_with_redeploy_detail(
    monkeypatch, google_stubs
):
    """PR-D (Finding C): a 404-dead deployment is NOT a consent problem
    - the old code mislabeled it needs_activation and sent the user to
    a consent remediation that cannot fix a deleted deployment. It now
    reads as not_installed (functionally there is no runtime to reach)
    with a redeploy detail that promises consent preservation."""
    result, _ = _run_health(
        monkeypatch,
        creds=MagicMock(name="creds"),
        probe=WebAppHealth.GONE,
        stubs=google_stubs,
    )
    rt = result["automation_runtime"]
    assert rt["installed"] is False
    assert rt["exec"] == "not_installed"
    assert rt["remediation_url"] is None
    assert "404" in rt["detail"]
    assert "as_install_automation" in rt["detail"]
    assert "consent" in rt["detail"]


def test_unknown_probe_reports_unknown_not_a_guess(monkeypatch, google_stubs):
    result, _ = _run_health(
        monkeypatch,
        creds=MagicMock(name="creds"),
        probe=WebAppHealth.UNKNOWN,
        stubs=google_stubs,
    )
    rt = result["automation_runtime"]
    assert rt["exec"] == "unknown"
    assert rt["remediation_url"] is None


def test_apps_script_api_disabled_detected_with_usersettings_url(
    monkeypatch, google_stubs
):
    """T1.2's distinctive case: the cheap script API call fails with
    Google's 'has not enabled the Apps Script API' 403 — the report
    says api_disabled and points at the usersettings toggle. The URL
    probe is NOT consulted (a disabled API blocks all management
    regardless of what /exec serves)."""
    drive, script = google_stubs
    script.projects().get().execute.side_effect = _http_error(
        403, APPS_SCRIPT_DISABLED_CONTENT
    )
    result, probe_calls = _run_health(
        monkeypatch,
        creds=MagicMock(name="creds"),
        stubs=(drive, script),
    )
    rt = result["automation_runtime"]
    assert rt["exec"] == "api_disabled"
    assert rt["remediation_url"] == (
        "https://script.google.com/home/usersettings"
    )
    assert probe_calls == []


def test_script_project_404_reports_not_installed(monkeypatch, google_stubs):
    """A recorded script id that no longer resolves (user deleted the
    project) is a reinstall case, not an activation case."""
    drive, script = google_stubs
    script.projects().get().execute.side_effect = _http_error(404)
    result, probe_calls = _run_health(
        monkeypatch,
        creds=MagicMock(name="creds"),
        stubs=(drive, script),
    )
    rt = result["automation_runtime"]
    assert rt["installed"] is False
    assert rt["exec"] == "not_installed"
    assert "as_install_automation" in rt["detail"]
    assert probe_calls == []


def test_unauthorized_creds_still_probe_the_exec_url(monkeypatch):
    """Broken OAuth must not hide the /exec state: the anonymous GET
    probe needs no creds, so the runtime is still classified."""
    result, probe_calls = _run_health(
        monkeypatch,
        creds=None,
        peek_status="unauthorized",
        peek_detail="token revoked",
        probe=WebAppHealth.HEALTHY,
    )
    assert result["google_api"] == "unauthorized"
    assert result["automation_runtime"]["exec"] == "serving"
    assert probe_calls == [_EXEC_URL]


def test_google_api_http_error_maps_401_403_to_unauthorized(monkeypatch):
    drive = MagicMock(name="drive-v3-stub")
    drive.about().get().execute.side_effect = _http_error(403)
    script = MagicMock(name="script-v1-stub")
    script.projects().get().execute.return_value = {"scriptId": "S1"}
    result, _ = _run_health(
        monkeypatch, creds=MagicMock(name="creds"), stubs=(drive, script)
    )
    assert result["google_api"] == "unauthorized"
    assert result["google_api_detail"] is not None


def test_google_api_5xx_maps_to_error_with_detail(monkeypatch):
    drive = MagicMock(name="drive-v3-stub")
    drive.about().get().execute.side_effect = _http_error(500)
    script = MagicMock(name="script-v1-stub")
    script.projects().get().execute.return_value = {"scriptId": "S1"}
    result, _ = _run_health(
        monkeypatch, creds=MagicMock(name="creds"), stubs=(drive, script)
    )
    assert result["google_api"] == "error"
    assert "500" in result["google_api_detail"]


def test_is_apps_script_api_disabled_matcher():
    assert admin_tools._is_apps_script_api_disabled(
        _http_error(403, APPS_SCRIPT_DISABLED_CONTENT)
    )
    assert not admin_tools._is_apps_script_api_disabled(
        _http_error(403, b'{"error": {"code": 403, "message": "nope"}}')
    )
