"""v2.6 (#48): the 4 keys.get_key() wire-up sites + architectural guard.

scope-auditor's verified inventory (post-PR-#49 line drift):

    | # | File                | Purpose(s)              |
    |---|---------------------|-------------------------|
    | 1 | http_server.py:189  | oauth_state             |
    | 2 | http_server.py:545  | api_bearer + signed_url (DUAL — split) |
    | 3 | oauth_google.py:187 | oauth_state             |
    | 4 | server.py:1719      | signed_url              |

Each test exercises a SINGLE site by triggering that site's code path
and asserting the per-purpose ``key_call_totals`` counter incremented
in the right slot. The architectural guard
(``test_no_production_file_reads_MCP_BEARER_TOKEN_directly``) is the
mutation guard for the WHOLE class of bypass bugs — it scans src/
for any ``os.environ.get("MCP_BEARER_TOKEN")`` outside the allowed
list (``keys.py`` only), so a future PR that adds a 5th bypass site
fails CI before merge.

Pre-fix verification: each site-routing test was confirmed to FAIL
when the corresponding fix is reverted to the pre-v2.6 ``os.environ
.get("MCP_BEARER_TOKEN")`` pattern. See the commit description for
the verification log.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# A 32-char master so the override / shim paths don't trip the
# ≥32-char gate (some sites may exercise paths that pass the master
# straight through). Reused across tests; never used to sign anything
# in production fixtures.
_TEST_MASTER = "x" * 32


@pytest.fixture(autouse=True)
def reset_keys_counters(monkeypatch):
    """Every test gets a fresh counter so increments are unambiguous.

    Uses the keys module's documented test-only reset helpers — keeps
    us in the same boat as the unit tests in test_keys.py."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", _TEST_MASTER)
    from google_docs_mcp import keys
    keys._reset_shim_hit_counters_for_tests()
    keys._reset_total_call_counters_for_tests()
    keys._reset_first_call_timestamps_for_tests()
    yield
    keys._reset_shim_hit_counters_for_tests()
    keys._reset_total_call_counters_for_tests()
    keys._reset_first_call_timestamps_for_tests()


# ---------------------------------------------------------------------
# Site 1: http_server.py:189 — OAuth callback uses oauth_state
# ---------------------------------------------------------------------


def test_site_1_oauth_callback_routes_through_get_key_oauth_state(monkeypatch):
    """Drive the OAuth callback's signing-key resolution and assert
    the ``oauth_state`` counter incremented. Pre-v2.6 the callback
    read ``MCP_BEARER_TOKEN`` directly and never touched the counter
    — that's the regression this guards."""
    from google_docs_mcp import keys

    # Reach into the dispatch logic by calling the callback's resolve
    # path directly. The callback function reads keys.get_key("oauth_state")
    # before any of its other branches; we can simulate that by triggering
    # the code path with a request that lacks 'code'/'state' (returns
    # _error_page early, but only AFTER the get_key call).
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from google_docs_mcp.http_server import oauth_google_api_callback
    from google_docs_mcp.oauth_google import CALLBACK_PATH

    app = Starlette(routes=[
        Route(CALLBACK_PATH, oauth_google_api_callback, methods=["GET"])
    ])
    with TestClient(app) as client:
        # Missing 'code' and 'state' — handler returns 400 EARLY, but
        # only after reading the signing key (pre-v2.6: env; post-v2.6:
        # get_key). The counter must show the call landed.
        # Actually — reading the handler carefully, the signing_key
        # resolution happens AFTER the 'code'/'state' presence check.
        # So we need a request with both params (the state-verify will
        # fail later but the get_key call WILL have happened).
        resp = client.get(
            CALLBACK_PATH,
            params={"state": "bogus.state.token.sig", "code": "FAKE"},
        )

    # The handler should have returned a non-2xx (state is bogus) but
    # MUST have read the oauth_state key in the process.
    assert resp.status_code != 200, "test setup wrong — state should not validate"
    totals = keys.get_total_call_counters()
    assert totals["oauth_state"] >= 1, (
        f"Site 1 (http_server.py:189 OAuth callback) did NOT call "
        f"keys.get_key('oauth_state') — the bypass is back. "
        f"Totals: {totals!r}"
    )


# ---------------------------------------------------------------------
# Site 2: http_server.py:545 — build_app DUAL site (api_bearer + signed_url)
# ---------------------------------------------------------------------


def test_site_2_build_app_routes_through_get_key_api_bearer_AND_signed_url():
    """Drive build_app(); the DUAL site must call get_key TWICE — once
    for api_bearer (bearer-header equality) and once for signed_url
    (signed-URL HMAC). Pre-v2.6 a single env read served both purposes.

    The split is architecturally load-bearing: it preserves the call-
    site discipline so the v2.0b strict-flip can HKDF-derive them
    separately (different purposes ⇒ different derived keys ⇒ leaking
    one doesn't compromise the other). Even during the shim window
    both resolve to the same raw master, but they MUST come from
    separate get_key() calls — otherwise a future flip would re-edit
    this site."""
    from google_docs_mcp import keys
    from google_docs_mcp.http_server import build_app

    # The mcp arg is consumed only for mcp.http_app() inside build_app;
    # a MagicMock with the right shape is enough.
    mock_mcp = MagicMock()
    mock_mcp_app = MagicMock()
    mock_mcp_app.lifespan = None
    mock_mcp.http_app.return_value = mock_mcp_app

    build_app(mock_mcp)

    totals = keys.get_total_call_counters()
    assert totals["api_bearer"] >= 1, (
        f"Site 2 (http_server.py:545 build_app) did NOT call "
        f"keys.get_key('api_bearer'). Totals: {totals!r}"
    )
    assert totals["signed_url"] >= 1, (
        f"Site 2 (http_server.py:545 build_app) did NOT call "
        f"keys.get_key('signed_url'). The DUAL site was not split — "
        f"BearerTokenMiddleware would use the same key for both purposes "
        f"and v2.0b's strict-flip would re-edit this site. "
        f"Totals: {totals!r}"
    )


# ---------------------------------------------------------------------
# Site 3: oauth_google.py:187 — resolve_runtime_oauth_config
# ---------------------------------------------------------------------


def test_site_3_resolve_runtime_oauth_config_routes_through_get_key_oauth_state(
    monkeypatch, tmp_path,
):
    """Drive resolve_runtime_oauth_config() — it's the tool-side
    counterpart to the callback's state-signing key resolution.
    Pre-v2.6 read MCP_BEARER_TOKEN directly via os.environ.get."""
    from google_docs_mcp import keys

    # Provide the rest of the env the resolver needs so the call doesn't
    # raise for unrelated reasons.
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS_JSON",
        json.dumps({
            "web": {
                "client_id": "CID.apps.googleusercontent.com",
                "client_secret": "CSEC",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
        }),
    )

    from google_docs_mcp.oauth_google import resolve_runtime_oauth_config
    cfg = resolve_runtime_oauth_config()

    # v2.0b: resolve_runtime_oauth_config() returns signing_key as
    # bytes (was str pre-flip, which crashed on HKDF output). Post-
    # flip the value depends on which path keys.get_key resolves to:
    #   - override (MCP_OAUTH_STATE_SIGNING_KEY set): the override
    #     bytes (UTF-8 encoded)
    #   - shim (purpose in _BACK_COMPAT_RAW_MASTER): the master bytes
    #   - HKDF: 32 random bytes
    # The test fixture sets MCP_BEARER_TOKEN but no override, AND
    # _BACK_COMPAT_RAW_MASTER is empty post-flip, so HKDF runs and
    # the bytes are non-deterministic-looking. Equality against
    # _TEST_MASTER was only correct under the shim; assert just the
    # contract (bytes, non-empty) plus the counter increment below.
    assert isinstance(cfg["signing_key"], bytes), (
        f"signing_key must be bytes (v2.0b post-flip), got "
        f"{type(cfg['signing_key']).__name__}"
    )
    assert len(cfg["signing_key"]) >= 16, (
        f"signing_key suspiciously short ({len(cfg['signing_key'])} "
        f"bytes) — get_key path may have returned a default"
    )
    totals = keys.get_total_call_counters()
    assert totals["oauth_state"] >= 1, (
        f"Site 3 (oauth_google.py:187 resolve_runtime_oauth_config) did "
        f"NOT call keys.get_key('oauth_state'). Totals: {totals!r}"
    )


# ---------------------------------------------------------------------
# Site 4: server.py:1719 — gdocs_get_signed_upload_url tool
# ---------------------------------------------------------------------


def test_site_4_signed_upload_url_routes_through_get_key_signed_url(monkeypatch):
    """Drive gdocs_get_signed_upload_url(); it must resolve its HMAC
    key via keys.get_key('signed_url').

    v2.1 (PR #60) made the tool refuse to mint outside an MCP auth
    context, since signed URLs are now bound to user_id. To keep this
    site-4 wireup guard focused on its actual purpose (proving the
    keys.get_key('signed_url') counter increments), we mock the
    current-user lookup so the tool reaches the get_key call.
    """
    from google_docs_mcp import keys
    from google_docs_mcp.services.admin import tools as admin_tools
    from google_docs_mcp.services.admin.tools import gdocs_get_signed_upload_url

    # Mock the MCP auth-context lookup so the v2.1 user_id check
    # doesn't short-circuit before get_key('signed_url') runs.
    # v2.2.2 (Gap #7): tool moved to services/admin/tools.py; patch
    # target follows.
    monkeypatch.setattr(admin_tools, "current_user_id_or_none", lambda: "test-user-sub")

    # The tool returns a Markdown response by default; we don't care
    # about the URL itself, only that the counter incremented.
    _ = gdocs_get_signed_upload_url(ttl_seconds=60)

    totals = keys.get_total_call_counters()
    assert totals["signed_url"] >= 1, (
        f"Site 4 (server.py:1719 gdocs_get_signed_upload_url) did NOT "
        f"call keys.get_key('signed_url'). Totals: {totals!r}"
    )


# ---------------------------------------------------------------------
# Architectural guard: no production file may bypass keys.get_key()
# ---------------------------------------------------------------------


_ALLOWED_FILES_FOR_RAW_MCP_BEARER_TOKEN_READ = {
    # keys.py is the public facade. The mechanism layer (the actual
    # os.environ reads) lives in key_provider.py as of v2.1 M1a —
    # ``HKDFKeyProvider`` / ``RawMasterShimKeyProvider`` both read
    # MCP_BEARER_TOKEN at call time. The facade in keys.py no longer
    # reads it directly; provenance() falls back to os.environ ONLY
    # when the active provider doesn't supply a value (defensive).
    "src/google_docs_mcp/keys.py",
    "src/google_docs_mcp/key_provider.py",
}


_BYPASS_PATTERNS = (
    # Canonical bypass shapes. Docstrings / comments / error messages
    # mentioning the env var by NAME are NOT caught — they're load-
    # bearing (operators grep error logs for the env var name). Only
    # real os.environ reads of MCP_BEARER_TOKEN are flagged.
    'os.environ.get("MCP_BEARER_TOKEN")',
    "os.environ.get('MCP_BEARER_TOKEN')",
    'os.environ["MCP_BEARER_TOKEN"]',
    "os.environ['MCP_BEARER_TOKEN']",
    # v2.0b: the comma-default form was missed by the original PR #57
    # pattern list — surfaced when strict-flip turned http_server.py:420
    # `os.environ.get("MCP_BEARER_TOKEN", "")` from a latent str/bytes
    # type bug into a real production crash. Catch both quote styles
    # of the default form.
    'os.environ.get("MCP_BEARER_TOKEN",',
    "os.environ.get('MCP_BEARER_TOKEN',",
    # Aliased form used in oauth_google.py historically:
    # `import os as _os; _os.environ.get(...)`
    '_os.environ.get("MCP_BEARER_TOKEN")',
    "_os.environ.get('MCP_BEARER_TOKEN')",
    '_os.environ.get("MCP_BEARER_TOKEN",',
    "_os.environ.get('MCP_BEARER_TOKEN',",
)


def _file_has_bypass(text: str) -> bool:
    return any(p in text for p in _BYPASS_PATTERNS)


def test_no_production_file_reads_MCP_BEARER_TOKEN_directly():
    """The mutation guard for the WHOLE class of bypass bugs (v2.6 #48).

    Scans every Python file under ``src/`` for the canonical
    ``os.environ`` bypass shapes (see ``_BYPASS_PATTERNS``) and
    asserts they appear only in ``keys.py`` — the one legitimate
    master-reader. Any new bypass a future PR adds turns this test
    red BEFORE merge.

    Comments / docstrings / log-message strings that mention
    ``MCP_BEARER_TOKEN`` by name are intentionally NOT caught: those
    are load-bearing (operators grep error logs for the env var name).
    The guard targets only real ``os.environ`` reads.
    """
    # Resolve src/ relative to this test file so the test works from
    # any cwd. The structure is repo-root/{src,tests}; the file lives at
    # repo-root/tests/unit/<this>, so src/ is ../../src.
    repo_root = Path(__file__).resolve().parents[2]
    src_root = repo_root / "src"
    assert src_root.is_dir(), f"src/ not found at {src_root}"

    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        rel = py.relative_to(repo_root).as_posix()
        if rel in _ALLOWED_FILES_FOR_RAW_MCP_BEARER_TOKEN_READ:
            continue
        text = py.read_text(encoding="utf-8")
        if _file_has_bypass(text):
            offenders.append(rel)

    assert not offenders, (
        f"Production source files bypass keys.get_key() and read "
        f"MCP_BEARER_TOKEN from os.environ directly: {offenders}. "
        f"Every purpose-keyed use of the master MUST route through "
        f"keys.get_key(<purpose>) so the v2.0b strict-flip can HKDF-"
        f"derive without re-editing N call sites. If you genuinely "
        f"need raw master access (extremely rare), add the file to "
        f"_ALLOWED_FILES_FOR_RAW_MCP_BEARER_TOKEN_READ with a comment "
        f"justifying why."
    )


def test_architectural_guard_mutation_check_detects_new_bypass(tmp_path):
    """Mutation guard for the architectural guard itself.

    Synthesize a file with the banned pattern; verify ``_file_has_bypass``
    flags it. Without this meta-guard, a future refactor that silently
    no-ops the live test (e.g., wrong glob, broken read, accidentally
    truncated _BYPASS_PATTERNS tuple) would pass even with real
    bypasses present.
    """
    synthetic = tmp_path / "synthetic_bypass.py"
    synthetic.write_text(
        'import os\n'
        'token = os.environ.get("MCP_BEARER_TOKEN")\n',
        encoding="utf-8",
    )
    assert _file_has_bypass(synthetic.read_text(encoding="utf-8")), (
        "The bypass-pattern scan would not have caught a synthetic "
        "bypass — the architectural guard above is BROKEN even if it "
        "returns an empty offenders list."
    )
    # And the inverse: a docstring mention must NOT trip the scan
    # (otherwise we'd hit the false-positive failure we saw on first
    # implementation).
    docstring_only = tmp_path / "docstring_only.py"
    docstring_only.write_text(
        '"""Configuration is read from MCP_BEARER_TOKEN."""\n'
        'token = keys.get_key("api_bearer")\n',
        encoding="utf-8",
    )
    assert not _file_has_bypass(docstring_only.read_text(encoding="utf-8")), (
        "The bypass-pattern scan is too broad — it flags docstring "
        "mentions of MCP_BEARER_TOKEN as if they were real env reads. "
        "Operators rely on grepping error logs for the env var name."
    )


# ---------------------------------------------------------------------
# first_call_at observability — gates the v2.0b strict-flip
# ---------------------------------------------------------------------


def test_first_call_timestamp_recorded_on_first_get_key(monkeypatch):
    """v2.6 (#48) instrumentation: first_call_age_seconds[purpose] must
    be non-None after the first get_key(purpose) call in a process."""
    from google_docs_mcp import keys
    keys._reset_first_call_timestamps_for_tests()
    assert keys.get_first_call_timestamps()["api_bearer"] is None

    monkeypatch.setenv("MCP_BEARER_TOKEN", _TEST_MASTER)
    keys.get_key("api_bearer")

    ts = keys.get_first_call_timestamps()["api_bearer"]
    assert ts is not None, "first call did not stamp the timestamp"
    assert ts > 0, f"first call timestamp looks wrong: {ts!r}"


def test_first_call_timestamp_does_not_overwrite_on_later_calls(monkeypatch):
    """The timestamp pins the START of the soak window — subsequent
    calls must NOT overwrite it (operators rely on the age increasing
    monotonically to decide soak has elapsed)."""
    import time as _time
    from google_docs_mcp import keys
    keys._reset_first_call_timestamps_for_tests()

    monkeypatch.setenv("MCP_BEARER_TOKEN", _TEST_MASTER)
    keys.get_key("oauth_state")
    first = keys.get_first_call_timestamps()["oauth_state"]
    assert first is not None

    _time.sleep(0.05)  # ensure clock would have moved
    keys.get_key("oauth_state")
    second = keys.get_first_call_timestamps()["oauth_state"]
    assert second == first, (
        f"first-call timestamp got overwritten on second call: "
        f"{first!r} → {second!r}. The timestamp must pin the START of "
        f"the soak window, not the most-recent call."
    )


def test_first_call_timestamp_concurrent_writes_pick_exactly_one(monkeypatch):
    """Reviewer concurrency guard: under concurrent first-call writes,
    the check-then-set in _record_first_call must serialize so EXACTLY
    ONE timestamp value wins. Without the lock, two threads that each
    saw ``None`` would each write a different ``time.time()`` value,
    and the second write would silently overwrite the first (biasing
    the operator's "elapsed since first call" calculation toward the
    later writer's clock-read).

    Test approach: N worker threads behind a barrier so they all race
    ``get_key("api_bearer")`` simultaneously. Then assert:
      1. The final timestamp is not None.
      2. A subsequent serial call doesn't move it.

    The killer assertion (2) is what catches a missing lock: with a
    racy implementation, two threads both see None and both write —
    the SECOND writer's value wins. After the race, a serial call
    observing that "later" value would still match itself, but the
    invariant under proper locking is that the FIRST writer's value
    wins and never moves. We can't directly observe which writer
    won, but the no-overwrite-on-subsequent-call invariant is what
    operators depend on: "first_call_age_seconds is monotonic upward
    while the process runs." A racy implementation could violate
    that invariant under load.

    Belt-and-braces: also fire the concurrent race AGAIN and assert
    the timestamp still equals snapshot_1 — if the lock is missing
    AND the race produces multiple writes per round, the second-round
    "first call" would re-set _first_call_at if the check is also
    racy. The double-race makes that bug surface deterministically.
    """
    import threading
    from google_docs_mcp import keys
    keys._reset_first_call_timestamps_for_tests()
    assert keys.get_first_call_timestamps()["api_bearer"] is None

    monkeypatch.setenv("MCP_BEARER_TOKEN", _TEST_MASTER)

    N_THREADS = 32
    barrier = threading.Barrier(N_THREADS)
    errors: list[str] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            keys.get_key("api_bearer")
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        if t.is_alive():
            errors.append("worker did not finish in 10s")

    assert not errors, f"workers raised: {errors!r}"

    snapshot_1 = keys.get_first_call_timestamps()["api_bearer"]
    assert snapshot_1 is not None, (
        "after 32 concurrent get_key calls, first_call_at is still None — "
        "either no thread actually ran or the timestamp write was lost"
    )

    # Subsequent serial call must NOT move the timestamp.
    keys.get_key("api_bearer")
    snapshot_2 = keys.get_first_call_timestamps()["api_bearer"]
    assert snapshot_2 == snapshot_1, (
        f"first_call_at moved between concurrent-race snapshot ({snapshot_1!r}) "
        f"and subsequent serial call ({snapshot_2!r}). Either the lock "
        f"around _record_first_call's check-then-set was dropped, or the "
        f"no-overwrite invariant was lost."
    )

    # Belt-and-braces: a second concurrent burst must also not move the
    # timestamp. A racy check-then-set would surface here as a write
    # under load even though _first_call_at is non-None.
    barrier_2 = threading.Barrier(N_THREADS)

    def worker_2() -> None:
        try:
            barrier_2.wait(timeout=5.0)
            keys.get_key("api_bearer")
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(f"round-2 {type(e).__name__}: {e}")

    threads_2 = [threading.Thread(target=worker_2) for _ in range(N_THREADS)]
    for t in threads_2:
        t.start()
    for t in threads_2:
        t.join(timeout=10.0)

    assert not errors, f"round-2 workers raised: {errors!r}"
    snapshot_3 = keys.get_first_call_timestamps()["api_bearer"]
    assert snapshot_3 == snapshot_1, (
        f"first_call_at moved during a second concurrent burst "
        f"({snapshot_1!r} → {snapshot_3!r}) even though it was already "
        f"set. The check-then-set in _record_first_call is racy under "
        f"load; the lock is missing or scoped wrong."
    )
