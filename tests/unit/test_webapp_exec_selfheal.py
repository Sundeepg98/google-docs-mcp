"""Web-app ``/exec`` probe classification + consent-preserving heal tests.

History (PR-D, 2026-07-10): the original self-heal (#221) lumped two
OPPOSITE conditions into one ``DEAD`` verdict. A 403 on ``/exec`` is
Google's per-script consent door (the deployment is fine; the user's
one-time Run + Allow in the editor opens it) — re-provisioning on it
mints a NEW project whose consent starts over: the thrash that kept the
feature broken. A 404 means the deployment is genuinely gone — but the
PROJECT usually survives, and cutting a fresh version + deployment on
the SAME project preserves the consent already granted.

The contract under test:

1. ``probe_webapp_health`` four-way classification (urlopen mocked):
   200+JSON → HEALTHY; 403 or 200+HTML → CONSENT_GATED; 404/other
   definitive 4xx → GONE; transport trouble / 5xx / 429 → UNKNOWN.
2. Ledger wiring — cloud path (``setup_apps_script_for_user``):
   HEALTHY and CONSENT_GATED and UNKNOWN all reuse the cache (no
   re-provision); GONE redeploys on the SAME script project (new
   project ONLY when ``script_exists`` says the project itself is
   gone); cold start never probes; no verification re-probe after a
   heal (deployment-state classification happens at the CALLER now).
3. Ledger wiring — local path (``setup_apps_script_auto``) mirror.

Spec: _audit/2026-07-10-pr-d-brief.md.
"""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch
from urllib import error as urlerror

import pytest

from appscriptly.setup_apps_script import WebAppHealth, probe_webapp_health

EXEC_URL = "https://script.google.com/macros/s/DEPLOY_ID/exec"
FRESH_URL = "https://script.google.com/macros/s/DEPLOY_FRESH/exec"

# Shape of the real thing: Google's door page is HTML carrying the
# window['ppConfig'] bootstrap — definitively not doGet's JSON.
DOOR_PAGE_HTML = (
    "<!DOCTYPE html><html><head><script nonce=\"x\">"
    "window['ppConfig'] = {};</script></head>"
    "<body>You need access</body></html>"
)
DOGET_JSON = '{"ok": true, "service": "google-docs-mcp restructure", "version": "1"}'


def _fake_creds():
    return MagicMock(name="fake_creds")


def _deployment(script_id: str, deployment_id: str, url: str):
    from appscriptly.services.gas_deploy.api import WebAppDeployment

    return WebAppDeployment(
        script_id=script_id,
        deployment_id=deployment_id,
        version=1,
        url=url,
    )


def _http_error(code: int, body: bytes = b"") -> urlerror.HTTPError:
    return urlerror.HTTPError(EXEC_URL, code, "message", None, io.BytesIO(body))


# ---------------------------------------------------------------
# 1. probe_webapp_health — classification (urlopen mocked)
# ---------------------------------------------------------------


class _FakeResponse:
    """Context-manager stand-in for a urlopen 200 response."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, limit: int | None = None) -> bytes:
        return self._body if limit is None else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_probe_transport(monkeypatch, outcome):
    """Make urlopen inside setup_apps_script produce ``outcome``.

    ``outcome``: bytes → a 200 response with that body; an Exception
    instance → raised. Returns a dict capturing the request urlopen saw.
    """
    from appscriptly import setup_apps_script

    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(
        setup_apps_script.urlrequest, "urlopen", _fake_urlopen
    )
    return captured


def test_probe_healthy_on_200_json(monkeypatch):
    """A live deployment: doGet answers 200 + JSON. Also pins the probe's
    transport contract — a GET with a short (non-None) timeout."""
    captured = _patch_probe_transport(monkeypatch, DOGET_JSON.encode("utf-8"))
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.HEALTHY
    assert captured["req"].get_method() == "GET"
    assert captured["req"].full_url == EXEC_URL
    assert captured["timeout"] is not None and captured["timeout"] <= 30


def test_probe_consent_gated_on_403_door_page(monkeypatch):
    """THE live-proven signature: Google answers /exec with its own 403
    HTML access page — the deployment EXISTS, the user's one-time
    Run + Allow opens it. Must NOT read as gone (re-provisioning on it
    mints a new project whose consent starts over)."""
    _patch_probe_transport(
        monkeypatch, _http_error(403, DOOR_PAGE_HTML.encode("utf-8"))
    )
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.CONSENT_GATED


def test_probe_consent_gated_on_200_html_interstitial(monkeypatch):
    """200 whose body is HTML, not JSON: a Google sign-in interstitial
    answered, not doGet — the same consent door in a different dress."""
    _patch_probe_transport(monkeypatch, DOOR_PAGE_HTML.encode("utf-8"))
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.CONSENT_GATED


@pytest.mark.parametrize("code", [400, 404, 410])
def test_probe_gone_on_definitive_4xx(monkeypatch, code):
    """A deleted deployment's URL 404s (and other non-consent definitive
    4xx read the same) — the deployment no longer exists."""
    _patch_probe_transport(monkeypatch, _http_error(code))
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.GONE


def test_probe_unknown_on_url_error(monkeypatch):
    """DNS/connection failures say nothing about the deployment."""
    _patch_probe_transport(
        monkeypatch, urlerror.URLError(ConnectionResetError("reset"))
    )
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.UNKNOWN


def test_probe_unknown_on_timeout(monkeypatch):
    """A socket timeout is transient — must NOT classify as gone."""
    _patch_probe_transport(monkeypatch, TimeoutError("timed out"))
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.UNKNOWN


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_probe_unknown_on_retryable_status(monkeypatch, code):
    """5xx / 429 are server-side or throttling blips, not proof the
    deployment is gone — treating them as GONE would redeploy on
    every Google hiccup."""
    _patch_probe_transport(monkeypatch, _http_error(code))
    assert probe_webapp_health(EXEC_URL) is WebAppHealth.UNKNOWN


# ---------------------------------------------------------------
# 2. Ledger wiring — cloud path (setup_apps_script_for_user)
# ---------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock AppsScriptClient with cold-start return values (mirrors the
    fixture in test_setup_apps_script_for_user.py)."""
    with patch(
        "appscriptly.setup_apps_script.AppsScriptClient"
    ) as client_class:
        client = MagicMock()
        client_class.return_value = client
        client.script_exists.return_value = True
        client.create_project.return_value = "SCRIPT_ID_NEW"
        client.create_version.return_value = 1
        client.deploy_webapp.return_value = _deployment(
            "SCRIPT_ID_NEW", "DEPLOY_ID", EXEC_URL
        )
        yield client


@pytest.fixture
def probe(monkeypatch):
    """Controllable stand-in for probe_webapp_health (default HEALTHY).

    The ledger tests exercise the WIRING (when the probe runs and what
    its verdict triggers); classification itself is covered above.
    """
    from appscriptly import setup_apps_script

    stub = MagicMock(return_value=WebAppHealth.HEALTHY)
    monkeypatch.setattr(setup_apps_script, "probe_webapp_health", stub)
    return stub


def test_cold_start_does_not_probe(mock_client, probe):
    """No cached URL, nothing to verify — a fresh install must not pay
    a probe round-trip (or grow a new network failure mode)."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    probe.assert_not_called()


def test_healthy_cached_deployment_is_reused(mock_client, probe):
    """HEALTHY probe → reconstruct from cache, no API calls redone."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    result = setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_client.create_project.call_count == 1
    assert mock_client.push_files.call_count == 1
    assert mock_client.create_version.call_count == 1
    assert mock_client.deploy_webapp.call_count == 1
    assert result.url == EXEC_URL
    # The second run verified the cached URL (the first had none yet).
    probe.assert_called_once_with(EXEC_URL)


def test_consent_gated_cached_deployment_is_reused(mock_client, probe):
    """CONSENT_GATED → the ledger STANDS. The deployment exists behind
    Google's consent door; only the user's one-time Run + Allow opens
    it. Re-provisioning here is the #221 thrash: a new project whose
    consent gate starts over. The installer surfaces activation
    instructions instead (covered in the gas_deploy tool tests)."""
    from appscriptly import user_store
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    probe.return_value = WebAppHealth.CONSENT_GATED
    result = setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_client.create_project.call_count == 1
    assert mock_client.create_version.call_count == 1
    assert mock_client.deploy_webapp.call_count == 1
    assert result.url == EXEC_URL
    assert result.script_id == "SCRIPT_ID_NEW"
    assert user_store.get_state("user-1")["apps_script_url"] == EXEC_URL


def test_gone_cached_deployment_redeploys_on_same_project(mock_client, probe):
    """GONE probe (404) → cut a fresh version + web-app deployment on
    the SAME script project. The project survives (script_exists is
    True), and reusing it preserves the user's one-time per-script
    consent — minting a new project here would re-gate the user. The
    HMAC key also survives so the redeployed script keeps signing with
    the user's stable key. No verification re-probe afterwards: the
    fresh deployment serves the consent door until Run + Allow, and the
    CALLER classifies that, never this layer."""
    from appscriptly import user_store
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    key_before = user_store.get_state("user-1")["apps_script_hmac_key"]

    probe.return_value = WebAppHealth.GONE
    mock_client.deploy_webapp.return_value = _deployment(
        "SCRIPT_ID_NEW", "DEPLOY_FRESH", FRESH_URL
    )

    result = setup_apps_script_for_user(_fake_creds(), "user-1")

    # Same project: no second projects.create; new version + deployment.
    assert mock_client.create_project.call_count == 1
    assert mock_client.push_files.call_count == 2
    assert mock_client.create_version.call_count == 2
    assert mock_client.deploy_webapp.call_count == 2
    # The redeploy targeted the SURVIVING script project.
    assert mock_client.deploy_webapp.call_args_list[-1].args[0] == "SCRIPT_ID_NEW"
    assert result.script_id == "SCRIPT_ID_NEW"
    assert result.url == FRESH_URL

    state = user_store.get_state("user-1")
    assert state["apps_script_url"] == FRESH_URL
    assert state["apps_script_script_id"] == "SCRIPT_ID_NEW"
    assert state["apps_script_hmac_key"] == key_before, (
        "ledger reset rotated the HMAC key — the redeployed script would "
        "be cut with a key the server no longer signs with"
    )
    # Exactly one probe (the GONE verdict); no post-heal verification
    # probe exists any more — that raise was the #221 misdiagnosis.
    probe.assert_called_once_with(EXEC_URL)


def test_project_gone_mints_new_project(mock_client, probe):
    """New-project mint happens ONLY on the project-gone branch:
    projects.get says the script itself no longer exists, so there is
    no consent left to preserve. The reset happens without paying a
    probe round-trip (the deployment can't outlive its project)."""
    from appscriptly import user_store
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")

    mock_client.script_exists.return_value = False
    mock_client.create_project.return_value = "SCRIPT_ID_FRESH"
    mock_client.deploy_webapp.return_value = _deployment(
        "SCRIPT_ID_FRESH", "DEPLOY_FRESH", FRESH_URL
    )

    result = setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_client.create_project.call_count == 2
    assert result.script_id == "SCRIPT_ID_FRESH"
    assert result.url == FRESH_URL
    probe.assert_not_called()

    state = user_store.get_state("user-1")
    assert state["apps_script_script_id"] == "SCRIPT_ID_FRESH"
    assert state["apps_script_url"] == FRESH_URL


def test_unknown_probe_reuses_cached_deployment(mock_client, probe):
    """UNKNOWN (network blip) → do NOT redeploy. A flaky network must
    never thrash a working deployment."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")
    probe.return_value = WebAppHealth.UNKNOWN
    result = setup_apps_script_for_user(_fake_creds(), "user-1")

    assert mock_client.create_project.call_count == 1
    assert mock_client.deploy_webapp.call_count == 1
    assert result.url == EXEC_URL


def test_hash_mismatch_reset_skips_probe(mock_client, probe, tmp_path):
    """Ordering guard: when the content hash already forces a reset, the
    cached URL is being discarded anyway — no probe round-trip first."""
    from appscriptly.setup_apps_script import setup_apps_script_for_user

    setup_apps_script_for_user(_fake_creds(), "user-1")

    # Operator-edited source (same trick as the existing reset tests;
    # the fake must carry the HMAC sentinel for key injection).
    fake_path = tmp_path / "edited_restructure.gs"
    fake_path.write_text(
        "// totally different content\nvar MCP_HMAC_KEY = '__MCP_HMAC_KEY__';"
    )
    with patch(
        "appscriptly.setup_apps_script.RESTRUCTURE_GS_PATH", fake_path
    ):
        mock_client.create_project.return_value = "SCRIPT_ID_FRESH"
        mock_client.deploy_webapp.return_value = _deployment(
            "SCRIPT_ID_FRESH", "DEPLOY_FRESH", FRESH_URL
        )
        setup_apps_script_for_user(_fake_creds(), "user-1")

    probe.assert_not_called()
    assert mock_client.create_project.call_count == 2


# ---------------------------------------------------------------
# 3. Ledger wiring — local path (setup_apps_script_auto)
# ---------------------------------------------------------------


@pytest.fixture
def mock_local_setup(tmp_path):
    """Mock AppsScriptClient + creds + config store for the local CLI
    path (mirrors the fixture in test_setup_idempotency.py)."""
    with (
        patch("appscriptly.setup_apps_script.load_credentials") as load_oauth,
        patch("appscriptly.setup_apps_script.AppsScriptClient") as client_class,
        patch("appscriptly.setup_apps_script.config") as cfg_mod,
    ):
        load_oauth.return_value = MagicMock()
        client = MagicMock()
        client_class.return_value = client
        _cfg_store: dict = {}

        def _cfg_load():
            return dict(_cfg_store)

        def _cfg_save(updates):
            _cfg_store.update(updates)
            return dict(_cfg_store)

        cfg_mod.load.side_effect = _cfg_load
        cfg_mod.save.side_effect = _cfg_save

        client.script_exists.return_value = True
        client.create_project.return_value = "SCRIPT_ID_NEW"
        client.create_version.return_value = 1
        client.deploy_webapp.return_value = _deployment(
            "SCRIPT_ID_NEW", "DEPLOY_ID", EXEC_URL
        )
        yield {
            "client": client,
            "data_dir": tmp_path,
            "cfg_store": _cfg_store,
        }


def test_local_healthy_cached_deployment_is_reused(mock_local_setup, probe):
    from appscriptly.setup_apps_script import setup_apps_script_auto

    setup_apps_script_auto(data_dir=mock_local_setup["data_dir"])
    setup_apps_script_auto(data_dir=mock_local_setup["data_dir"])

    assert mock_local_setup["client"].create_project.call_count == 1
    assert mock_local_setup["client"].deploy_webapp.call_count == 1
    probe.assert_called_once_with(EXEC_URL)


def test_local_gone_cached_deployment_redeploys_on_same_project(
    mock_local_setup, probe,
):
    """Local mirror of the GONE heal: same project kept, fresh
    version + deployment cut, and the runtime config (what docx_import
    reads) ends up on the new URL with the HMAC key unchanged."""
    from appscriptly.setup_apps_script import setup_apps_script_auto

    setup_apps_script_auto(data_dir=mock_local_setup["data_dir"])
    key_before = mock_local_setup["cfg_store"]["apps_script_hmac_key"]

    probe.return_value = WebAppHealth.GONE
    client = mock_local_setup["client"]
    client.deploy_webapp.return_value = _deployment(
        "SCRIPT_ID_NEW", "DEPLOY_FRESH", FRESH_URL
    )

    result = setup_apps_script_auto(data_dir=mock_local_setup["data_dir"])

    assert client.create_project.call_count == 1
    assert client.create_version.call_count == 2
    assert client.deploy_webapp.call_count == 2
    assert client.deploy_webapp.call_args_list[-1].args[0] == "SCRIPT_ID_NEW"
    assert result.url == FRESH_URL
    assert result.script_id == "SCRIPT_ID_NEW"
    assert mock_local_setup["cfg_store"]["apps_script_webapp_url"] == FRESH_URL
    assert mock_local_setup["cfg_store"]["apps_script_hmac_key"] == key_before


def test_door_page_fixture_is_not_json():
    """Meta-guard: the HTML fixture used across this file must actually
    be non-JSON, or the probe's consent-door classification tests prove
    nothing."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(DOOR_PAGE_HTML)
