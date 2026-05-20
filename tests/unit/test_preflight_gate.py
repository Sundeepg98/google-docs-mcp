"""Bash preflight script's gate semantics — exit-code coverage (v2.6 R8).

scripts/preflight_strict_flip.sh is bash + jq + curl. We test its
EXIT CODES against canned /info JSON by:
  1. Spinning a tiny Starlette server on localhost:<ephemeral> that
     returns the canned JSON on GET /info.
  2. Setting MCP_BEARER_TOKEN-shaped bearer for the curl call.
  3. Invoking the script via subprocess and asserting returncode +
     selected stderr/stdout content.

This is the cheapest way to assert the gate semantics without a bats
or shellspec dependency. Skips automatically if bash, jq, or curl
aren't available on PATH (Windows dev box without WSL, for example).
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest


# Skip the whole module if the underlying tools aren't on PATH. The
# real gate is exercised by CI (Linux) and by operators (Linux on
# Fly); the unit-test purpose is "we can reproduce the exit codes
# deterministically when bash+jq+curl are present."
_TOOLS = ("bash", "jq", "curl")
_MISSING = [t for t in _TOOLS if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"missing tools on PATH: {_MISSING}; skipping bash-level gate test",
)


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "preflight_strict_flip.sh"
)


def _free_port() -> int:
    """Grab an ephemeral free port. Closing the socket releases it
    back to the OS before the test server binds — small race, but
    fine for local single-thread test runs."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_fake_info_server(payload: dict, bearer: str) -> tuple[str, threading.Event]:
    """Stand up a tiny HTTP server on an ephemeral port that returns
    ``payload`` (as JSON) on GET /info, gated by bearer auth.

    Returns the base URL and a stop event. Caller fires the event to
    shut the server down.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib API
            if self.path != "/info":
                self.send_response(404)
                self.end_headers()
                return
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {bearer}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
            # Silence the stdlib's per-request access log — keeps
            # pytest -v output clean.
            return

    port = _free_port()
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    stop_evt = threading.Event()

    def serve() -> None:
        while not stop_evt.is_set():
            httpd.handle_request()  # one-at-a-time; matches our test traffic

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    # Tiny wait for the listener to actually accept on the socket — we
    # grabbed the port THEN closed THEN bound, so there's a no-op
    # window. 50ms is plenty in practice.
    time.sleep(0.05)
    return f"http://127.0.0.1:{port}", stop_evt


def _run_preflight(base_url: str, token: str) -> subprocess.CompletedProcess:
    """Invoke the script. Captures stdout + stderr; never raises on
    non-zero exit.

    Force UTF-8 decoding for stdout/stderr — the script's error messages
    contain ``§`` (U+00A7) for the RUNBOOK section reference. On Windows
    subprocess defaults to CP1252 when ``text=True``, which mangles
    UTF-8 ``§`` bytes (``\\xc2\\xa7``) into a two-character ``Â§``
    sequence, causing assertion-string mismatch even though the visible
    output looks right.
    """
    # `bash <script>` runs even on Windows-with-Git-Bash without the
    # script needing execute bit set in the worktree checkout.
    return subprocess.run(
        ["bash", str(SCRIPT), base_url, token],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


_BEARER = "x" * 32


# ---------------------------------------------------------------------
# 1. Shim still serving traffic -> exit 4 (BLOCKER pre-R8 path was
#    "exit 4 forever in shim window"; post-R8 exit 4 means "operator
#    hasn't done Step 1 of §3.6").
# ---------------------------------------------------------------------


def test_gate_exit_4_when_shim_still_active():
    """shim_hits=150, total=150 → exit 4 (overrides not set / not picked up).

    Total deliberately above the 100-call floor so the floor check at
    exit-3 doesn't pre-empt the shim check at exit-4. The gate's
    intentional order is "floor first" (you can't judge shim activity
    without enough signal); this test isolates the SHIM branch by
    keeping us past the floor.
    """
    base_url, stop = _start_fake_info_server(
        payload={
            "version": "1.5.1",
            "key_back_compat_shim_active_hits": {"api_bearer": 150, "oauth_state": 0, "signed_url": 0},
            "key_call_totals": {"api_bearer": 150, "oauth_state": 0, "signed_url": 0},
        },
        bearer=_BEARER,
    )
    try:
        result = _run_preflight(base_url, _BEARER)
    finally:
        stop.set()
    assert result.returncode == 4, (
        f"expected exit 4 (shim active); got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    # Error message must name the override env vars so operators know
    # what to set.
    assert "MCP_API_BEARER_KEY" in result.stderr
    assert "RUNBOOK §3.6" in result.stderr


# ---------------------------------------------------------------------
# 2. Overrides serving traffic, shim quiet -> exit 0 (the actual
#    green-light state after operator has completed §3.6 Step 1).
# ---------------------------------------------------------------------


def test_gate_exit_0_when_overrides_serving_all_traffic():
    """shim_hits=0, total=150 → exit 0 (override path working)."""
    base_url, stop = _start_fake_info_server(
        payload={
            "version": "1.5.1",
            "key_back_compat_shim_active_hits": {"api_bearer": 0, "oauth_state": 0, "signed_url": 0},
            "key_call_totals": {"api_bearer": 80, "oauth_state": 30, "signed_url": 40},
        },
        bearer=_BEARER,
    )
    try:
        result = _run_preflight(base_url, _BEARER)
    finally:
        stop.set()
    assert result.returncode == 0, (
        f"expected exit 0 (green-light); got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "safe to merge v2.0b" in result.stdout
    # override_hits == total when shim_hits == 0; the script should
    # surface that derived value for operator visibility.
    assert "override hits: 150" in result.stdout


def test_gate_exit_0_when_multi_purpose_overrides_serving():
    """Per the brief's case 3: partial override across purposes is fine
    so long as shim_hits==0 and total>=100 overall.
    shim_hits=0, total=110 (api_bearer:50 + signed_url:60) → exit 0."""
    base_url, stop = _start_fake_info_server(
        payload={
            "version": "1.5.1",
            "key_back_compat_shim_active_hits": {"api_bearer": 0, "oauth_state": 0, "signed_url": 0},
            "key_call_totals": {"api_bearer": 50, "oauth_state": 0, "signed_url": 60},
        },
        bearer=_BEARER,
    )
    try:
        result = _run_preflight(base_url, _BEARER)
    finally:
        stop.set()
    assert result.returncode == 0, (
        f"expected exit 0 (multi-purpose green-light); got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------
# 3. Insufficient signal -> exit 3 (the floor is 100 calls).
# ---------------------------------------------------------------------


def test_gate_exit_3_when_total_below_100():
    """total=50 (even with shim_hits=0) → exit 3 (insufficient signal)."""
    base_url, stop = _start_fake_info_server(
        payload={
            "version": "1.5.1",
            "key_back_compat_shim_active_hits": {"api_bearer": 0, "oauth_state": 0, "signed_url": 0},
            "key_call_totals": {"api_bearer": 30, "oauth_state": 10, "signed_url": 10},
        },
        bearer=_BEARER,
    )
    try:
        result = _run_preflight(base_url, _BEARER)
    finally:
        stop.set()
    assert result.returncode == 3, (
        f"expected exit 3 (insufficient signal); got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "RUNBOOK §3.6" in result.stderr


# ---------------------------------------------------------------------
# 4. Missing fields -> exit 5 (server too old / wrong build).
# ---------------------------------------------------------------------


def test_gate_exit_5_when_required_fields_missing():
    """Response missing the telemetry fields → exit 5."""
    base_url, stop = _start_fake_info_server(
        payload={"version": "0.9.0"},  # no telemetry
        bearer=_BEARER,
    )
    try:
        result = _run_preflight(base_url, _BEARER)
    finally:
        stop.set()
    assert result.returncode == 5, (
        f"expected exit 5 (missing fields); got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
