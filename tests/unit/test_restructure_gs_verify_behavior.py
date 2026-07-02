"""Behavioral tests for the Apps Script ``/exec`` HMAC verify path (Node).

The verify logic lives in JavaScript (``restructure.gs`` and the gas_deploy
auto-injected web-app guard), so a Python substring test cannot prove it
actually authenticates requests. These tests execute the REAL ``.gs`` source
under Node (``tests/js/verify_hmac_behavior.test.mjs``) against a REALISTIC
``doPost(e)`` event:

  * ``e.parameter`` carries the ``mcp_ts`` / ``mcp_sig`` query params;
  * ``e.postData.contents`` carries the raw JSON body;
  * ``e.headers`` is ABSENT, exactly as the Apps Script runtime delivers it.
    The runtime NEVER exposes HTTP request headers to ``doPost`` (only
    ``parameter`` / ``parameters`` / ``postData`` / ``queryString``), which
    is why a header-based verify rejects every request and bricks the
    feature the moment a key is provisioned.

The Node suite asserts the verify ACCEPTS a correctly signed request and
REJECTS forged / tampered / stale / future / missing / replayed / malformed
ones, plus fail-closed behavior when the key sentinels were never templated
in. See the ``.mjs`` file for the case list.

Skipped when Node is not on PATH (GitHub Actions runners ship Node, so CI
executes this).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_NODE_SUITE = _REPO / "tests" / "js" / "verify_hmac_behavior.test.mjs"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None, reason="node not on PATH; JS behavioral suite needs it"
)


def _run_node(*args: str) -> subprocess.CompletedProcess[str]:
    assert _NODE is not None  # narrowed by the skipif marker
    return subprocess.run(
        [_NODE, str(_NODE_SUITE), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO),
    )


def test_restructure_gs_verify_behavior_under_node():
    """restructure.gs::_verifyHmac must accept a correctly signed request
    delivered the way Apps Script actually delivers it (query params in
    e.parameter, NO e.headers) and reject all forged/stale/missing variants.
    """
    proc = _run_node()
    assert proc.returncode == 0, (
        "Node behavioral suite for restructure.gs::_verifyHmac failed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_gas_deploy_injected_guard_verify_behavior_under_node(tmp_path):
    """The gas_deploy auto-injected ANYONE_ANONYMOUS web-app guard must pass
    the same behavioral suite. The guarded source is produced by the REAL
    injector (``inject_webapp_hmac_guard``), then executed under Node with
    the guard's verify entry point."""
    from appscriptly.services.gas_deploy.api import inject_webapp_hmac_guard

    key = "ef" * 32
    guarded = inject_webapp_hmac_guard(
        "function doPost(e){ return ContentService.createTextOutput('ok'); }",
        key,
    )
    source = tmp_path / "guarded.gs"
    source.write_text(guarded, encoding="utf-8")

    proc = _run_node(str(source), "__mcpVerifyWebappHmac", key)
    assert proc.returncode == 0, (
        "Node behavioral suite for the gas_deploy injected guard failed.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
