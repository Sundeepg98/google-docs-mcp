"""Auto-discovery SAFETY tests (Δ3 + bonus).

The auto-discovery registration mechanism's safety design is only worth
anything if it's VERIFIED to work. These tests EXERCISE the safety
machinery, not just the happy path:

  Δ3a  boot test          — importing ``server`` fresh runs discovery
                            with no exception + the boot count-floor
                            passes (catches a boot crash in CI, before
                            deploy; ``flyctl deploy --build-only`` can't
                            catch a boot crash).
  Δ3b  import-safety       — every module under services/ reachable by
                            discovery imports cleanly with NO network +
                            NO credentials (codifies the invariant that
                            LICENSES the harmless-superset import — a
                            future Gmail/Calendar tool must not load
                            creds at import).
  Δ3c  fail-loud-on-dup    — a duplicate-name registration raises,
                            proving on_duplicate="error" fails loud (not
                            silently overwrite).
  Δ3c' fail-loud-on-broken — a deliberately-broken service module import
                            (simulated in a subprocess) makes server.py's
                            boot raise the discovery RuntimeError, proving
                            the loop's aggregation + fail-loud actually
                            refuses to boot a partial surface.
  bonus subprocess         — the REAL prod/CI entry (FILE execution) in
                            process isolation registers exactly the
                            golden count — catching any import-ordering
                            regression a same-process test would mask.

NOTE on ``python -c``: discovery (like the old explicit-import chain)
registers a PARTIAL surface under ``python -c`` in an editable/src-layout
install — a Python packaging artifact (the ``google_docs_mcp.services``
subpackage doesn't resolve under ``-c`` editable), upstream of and
unrelated to the registration mechanism, and NOT a context any real
entry uses (prod = console script = file; CI = pytest = file). The
bonus test therefore asserts the FILE entry (the real one), in a
subprocess, NOT ``-c``. Asserting ``-c`` would encode a packaging
artifact as a requirement.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN_PATH = _REPO_ROOT / "tests" / "golden" / "tool_surface.json"


def _golden_count() -> int:
    import json
    return len(json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------
# Δ3a — boot test: discovery runs clean + count floor passes
# ---------------------------------------------------------------------


def test_boot_discovery_runs_and_count_floor_passes():
    """Importing ``google_docs_mcp.server`` MUST succeed (discovery runs
    with no exception) and register at least the floor count.

    server.py raises a RuntimeError at module-load if (a) any service
    module import fails, or (b) the registered count < the boot floor.
    So a SUCCESSFUL import here IS the proof both guards passed. We then
    re-assert the count >= floor explicitly for a clear message.

    This runs in the normal pytest process (FILE execution) — the same
    context prod's console-script entry uses — so it faithfully
    exercises the production boot path's registration.
    """
    import asyncio

    # If discovery or the floor failed, this import raises (and the test
    # errors with that RuntimeError — exactly the boot-crash signal we
    # want surfaced in CI before deploy).
    from google_docs_mcp.server import mcp
    from google_docs_mcp.server import _MIN_EXPECTED_TOOL_COUNT

    count = len(asyncio.run(mcp.list_tools()))
    assert count >= _MIN_EXPECTED_TOOL_COUNT, (
        f"Boot count floor breached: registered {count}, floor "
        f"{_MIN_EXPECTED_TOOL_COUNT}. (This should be unreachable — "
        f"server.py's module-load floor guard would have raised first.)"
    )


def test_boot_floor_constant_matches_golden():
    """The boot floor (``_MIN_EXPECTED_TOOL_COUNT``) MUST equal the
    golden surface size. The floor is a runtime backstop; the golden is
    the CI exact-anchor — they should agree on the known-good count.

    If they drift (e.g. someone bumped the floor but didn't re-freeze
    the golden, or vice versa), that's a maintenance bug — the two
    count-of-record sources must stay in lockstep.
    """
    from google_docs_mcp.server import _MIN_EXPECTED_TOOL_COUNT

    assert _MIN_EXPECTED_TOOL_COUNT == _golden_count(), (
        f"_MIN_EXPECTED_TOOL_COUNT ({_MIN_EXPECTED_TOOL_COUNT}) != golden "
        f"surface size ({_golden_count()}). Keep them in lockstep: if you "
        f"intentionally changed the tool count, update BOTH the floor in "
        f"server.py AND re-run `python scripts/freeze_tool_surface.py`."
    )


# ---------------------------------------------------------------------
# Δ3b — import-safety: every discovered module imports w/o network/creds
# ---------------------------------------------------------------------
#
# This test LICENSES the harmless-superset import rule (Option B): we
# import a superset of the tool modules (incl. decoration-free helpers).
# That's only safe if EVERY module under services/ is import-safe. This
# codifies it: a future tool/helper that loads creds or hits the network
# at import time fails here.


@pytest.fixture
def _block_network_and_creds(monkeypatch, tmp_path):
    """Block outbound sockets + point credential/data paths at an empty
    tmp dir, so any import-time network call or credential read FAILS
    loudly rather than silently succeeding against the dev machine's
    real network/creds.
    """
    import socket

    def _no_network(*_a, **_kw):  # pragma: no cover - the point is it's NOT hit
        raise AssertionError(
            "import-time network access detected — a services/ module "
            "performed I/O at import. All network I/O must be deferred to "
            "tool INVOCATION (the auto-discovery import-safety invariant)."
        )

    # Block the two socket entry points an HTTP client would use.
    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    # Point creds/data at an empty tmp dir + clear the bearer so any
    # import-time credential load finds nothing (and, if it tried to
    # REQUIRE creds at import, would fail).
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_CONFIG", raising=False)
    yield


def _all_discoverable_service_modules() -> list[str]:
    """Every module the discovery walk would import — the EXACT same
    enumeration server.py uses (underscore-skip + {api,scopes} denylist),
    so this test covers precisely the modules discovery loads at boot.

    Per the brief: cover EVERY discovered module (not just tool modules)
    — ideally every service module, since all sit on the boot graph.
    We additionally include the ``api``/``scopes`` support modules here
    (beyond what discovery imports) because they too are on the boot
    graph transitively and must be import-safe — a strictly stronger
    guarantee than discovery strictly needs.
    """
    import pkgutil

    import google_docs_mcp.services as services_pkg

    mods: list[str] = []
    for modinfo in pkgutil.walk_packages(
        services_pkg.__path__, prefix=services_pkg.__name__ + "."
    ):
        if modinfo.ispkg:
            continue
        leaf = modinfo.name.rsplit(".", 1)[-1]
        # Skip the pure-declaration _expected_tools + dunder/private.
        # KEEP api/scopes here (stronger-than-discovery safety check).
        if leaf.startswith("_"):
            continue
        mods.append(modinfo.name)
    return mods


def test_every_service_module_imports_without_network_or_creds(
    _block_network_and_creds,
):
    """Every services/ module reachable at boot MUST import cleanly with
    network blocked + no credentials. This is the invariant that LICENSES
    auto-discovery's harmless-superset import.

    A module that loads creds or hits the network at import time will
    raise here (the blocked socket → AssertionError, or a cred-required
    path → its own error). Future Gmail/Calendar tools: defer ALL I/O to
    invocation.
    """
    import importlib

    modules = _all_discoverable_service_modules()
    # Sanity: the enumeration found a non-trivial set (guards against the
    # walk silently returning [] and the test vacuously passing).
    assert len(modules) >= 15, (
        f"discovery enumeration found only {len(modules)} service modules; "
        f"expected >= 15. The walk may be misconfigured (or running under "
        f"the editable -c packaging artifact). Modules: {modules}"
    )

    failures: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: {type(e).__name__}: {e}")
    assert not failures, (
        "service module(s) failed import-safety (network blocked, no "
        f"creds):\n  " + "\n  ".join(failures)
        + "\nAll services/ modules MUST be import-safe — defer network + "
        "credential I/O to tool invocation, never module load."
    )


# ---------------------------------------------------------------------
# Δ3c — fail-loud: a duplicate registration raises (on_duplicate=error)
# ---------------------------------------------------------------------


def test_duplicate_tool_registration_fails_loud():
    """A double-registration of the same tool name MUST raise — proving
    ``on_duplicate="error"`` on the FastMCP ctor (the prod-critical
    posture) actually fails loud rather than silently overwriting.

    This is the mechanism the discovery loop relies on: if two feature
    modules ever decorated the same tool name, the second decoration
    raises ``ValueError: Component already exists``, the discovery
    loop's try/except captures it as a failure, and the boot
    RuntimeError fires. Here we exercise that raise directly against the
    live mcp instance (the same instance discovery registered onto),
    under a throwaway name we register twice then remove.
    """
    from google_docs_mcp.server import mcp

    dup_name = "_test_discovery_safety_dup_probe"

    def _probe() -> dict:
        return {}

    try:
        # First registration under the throwaway name — succeeds.
        mcp.tool(name=dup_name)(_probe)
        # Second registration of the SAME name MUST raise — this is the
        # on_duplicate="error" guard firing (default FastMCP would warn
        # + overwrite silently).
        with pytest.raises(ValueError, match="(?i)exist|duplicate") as exc:
            mcp.tool(name=dup_name)(_probe)
        assert "exist" in str(exc.value).lower(), (
            f"dup registration raised, but not the expected component-"
            f"exists error: {exc.value!r}"
        )
    finally:
        # Remove the throwaway so it never leaks into the live surface
        # (the golden / declared witnesses would otherwise flag it).
        # local_provider.remove_tool is the non-deprecated path.
        try:
            mcp.local_provider.remove_tool(dup_name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------
# Bonus — subprocess FILE-entry registers exactly the golden count
# ---------------------------------------------------------------------


def test_file_entry_subprocess_registers_full_surface():
    """The REAL prod/CI entry context (FILE execution) registers exactly
    the golden surface count, verified in a CLEAN SUBPROCESS.

    Process isolation matters: a same-process test inherits this
    session's already-imported modules, which could mask an
    import-ORDERING regression (e.g. discovery depending on something a
    test happened to import first). A fresh subprocess running a FILE
    (the prod console-script context) catches that.

    NOT ``python -c`` — see the module docstring: ``-c`` under an
    editable/src-layout install hits a packaging artifact that registers
    a partial surface for BOTH the old + new mechanisms, unrelated to the
    registration design and used by no real entry. Asserting it would
    encode a non-requirement. We assert the FILE entry, which is what
    prod (console script) and CI (pytest) actually use.
    """
    import tempfile

    golden = _golden_count()

    # A tiny entry SCRIPT run as `python <file>` — FILE execution, the
    # prod-relevant context (prod = console script = file; CI = pytest =
    # file). Deliberately NOT `python -c` (the editable/src-layout
    # packaging-artifact context that registers a partial surface for
    # both old + new mechanisms and is used by no real entry).
    probe_src = textwrap.dedent(
        """
        import asyncio
        from google_docs_mcp.server import mcp
        print(len(asyncio.run(mcp.list_tools())))
        """
    )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, dir=str(_REPO_ROOT),
        encoding="utf-8",
    ) as tf:
        tf.write(probe_src)
        probe_path = tf.name
    try:
        result = subprocess.run(
            [sys.executable, probe_path],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=120,
        )
    finally:
        Path(probe_path).unlink(missing_ok=True)

    assert result.returncode == 0, (
        f"subprocess file-entry boot FAILED (returncode "
        f"{result.returncode}). This means importing server.py as a file "
        f"crashed — a boot failure that would silent-502 the deploy.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    count = int(result.stdout.strip().splitlines()[-1])
    assert count == golden, (
        f"FILE-entry subprocess registered {count} tools; golden expects "
        f"{golden}. The real prod/CI entry context does NOT reproduce the "
        f"frozen surface — an import-ordering regression or a discovery "
        f"miss that only manifests in process isolation."
    )


# ---------------------------------------------------------------------
# Δ3c' — fail-loud on a BROKEN service module (subprocess, isolated)
# ---------------------------------------------------------------------


def test_broken_service_module_makes_boot_fail_loud():
    """A service module that raises on import MUST make ``server.py``'s
    boot raise the discovery ``RuntimeError`` — proving the discovery
    loop's try/except aggregation + fail-loud guard actually refuse to
    boot a partial tool surface (the #127-class silent-502 prevention).

    Run in a CLEAN SUBPROCESS that monkeypatches ``importlib.import_module``
    to sabotage one discovered module BEFORE importing server, then
    asserts the import dies with the discovery RuntimeError naming the
    broken module. Subprocess isolation is essential — sabotaging
    importlib + a half-imported ``server`` in this pytest process would
    corrupt the shared ``sys.modules`` for every later test.
    """
    sabotage_src = textwrap.dedent(
        """
        import importlib
        _real = importlib.import_module
        _BROKEN = "google_docs_mcp.services.sheets.tools"

        def _sabotaged(name, *a, **k):
            if name == _BROKEN:
                raise ImportError("SIMULATED broken module (fail-loud test)")
            return _real(name, *a, **k)

        importlib.import_module = _sabotaged
        try:
            import google_docs_mcp.server  # noqa: F401 — must raise
        except RuntimeError as e:
            msg = str(e)
            ok = ("discovery FAILED" in msg) and (_BROKEN in msg)
            print("FAILLOUD_OK" if ok else "FAILLOUD_WRONG_MSG")
        except BaseException as e:  # noqa: BLE001
            print(f"FAILLOUD_WRONG_EXC:{type(e).__name__}")
        else:
            print("FAILLOUD_NO_RAISE")
        """
    )

    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, dir=str(_REPO_ROOT),
        encoding="utf-8",
    ) as tf:
        tf.write(sabotage_src)
        path = tf.name
    try:
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=120,
        )
    finally:
        Path(path).unlink(missing_ok=True)

    out = result.stdout.strip()
    assert "FAILLOUD_OK" in out, (
        "A broken service module did NOT produce the expected boot "
        "RuntimeError naming the failure. The fail-loud discovery guard "
        "may be broken (it MUST refuse to boot a partial surface).\n"
        f"subprocess stdout: {out!r}\nstderr: {result.stderr[:500]!r}"
    )
