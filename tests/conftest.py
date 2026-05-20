"""Shared pytest config + fixtures.

Live (``@pytest.mark.live``) tests are opt-in — they hit real Drive.
Run with ``pytest --live`` to enable; default skips them so unit
tests stay fast and CI-friendly.

R23 B3: canonical ``isolated_db`` fixture lives here. Previously, five
unit-test modules each declared their own copy with subtly different
``_initialized_paths.clear()`` discipline (some pre-only, some post-only,
test_user_store.py none at all, test_credentials.py also resetting
``_per_user_locks``). That divergence was the symptom of test-isolation
debt — under ``pytest -n auto`` it would surface as flakes the moment
two workers raced on the shared module-level dicts.

The canonical fixture resets EVERY shared module-level dict in
``google_docs_mcp`` both pre- and post-yield: the union of all five
historical variants. Per-test cost: ~four dict clears, well under 1ms.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# --- Worktree-src shadow guard --------------------------------------------
# If a developer has `pip install -e .` against the main checkout and then
# runs pytest from a git worktree, Python imports `google_docs_mcp` from
# the main checkout's `src/` rather than the worktree's. Test files
# referencing modules added in worktree commits (or merged PRs the main
# checkout hasn't fast-forwarded to) then fail with
# ``ModuleNotFoundError: No module named 'google_docs_mcp.<new_module>'``
# even though the source is right there on disk.
#
# Prepending this worktree's `src/` to sys.path before any
# ``google_docs_mcp`` import resolves the shadow. Idempotent — has no
# effect when the editable install already points at the worktree (e.g.
# in CI), and harmless even then.
_WORKTREE_SRC = (Path(__file__).resolve().parent.parent / "src").resolve()
if _WORKTREE_SRC.is_dir() and str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))
    # Invalidate any cached negative-import results from prior pytest runs
    # in the same interpreter session (matters for `pytest --reload-`-style
    # plugins; benign no-op for the standard runner).
    import importlib
    importlib.invalidate_caches()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Per-test SQLite path + reset of every shared module-level dict.

    Canonical fixture consolidated in v2.0.5 (R23 B3). Replaces five
    per-file copies with identical-but-not-quite cleanup discipline.

    Resets, both pre- and post-yield:
      - ``user_store._initialized_paths``  (set of paths that already
        had schema-init applied; without resetting, the next test's
        fresh tmp DB skips the ALTER path)
      - ``credentials._per_user_locks``    (per-user threading.Lock
        registry; would accumulate across tests and let one test's
        lock contention bleed into another)
      - ``keys._shim_hit_counter``         (per-purpose shim-path
        increments; if a prior test triggered the shim, a later
        test asserting ``shim_hits == 0`` would falsely fail)
      - ``server._creds_cache``            (operator OAuth creds cache;
        a prior stdio-mode test would leave creds in place and a
        later HTTP-mode test would skip the per-user resolver)

    Also points ``user_store`` and ``default_data_dir()`` at ``tmp_path``
    via env-var override so no test touches ``~/.google-docs-mcp/``.

    Yields the per-test SQLite path for tests that want to seed it
    directly (e.g. legacy-schema migration tests).
    """
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))

    _reset_shared_module_state()
    yield db_file
    _reset_shared_module_state()


def _reset_shared_module_state() -> None:
    """Clear every shared module-level dict in google_docs_mcp.

    Kept as a top-level helper so a test that mutates state mid-body
    (e.g. spawning threads, then asserting a count, then continuing)
    can call it explicitly. Imports are inside so importing conftest
    doesn't drag the package in for live-test-only collection.
    """
    from google_docs_mcp import credentials, keys, user_store
    from google_docs_mcp import server as server_mod

    user_store._initialized_paths.clear()
    credentials._per_user_locks.clear()
    # keys.py exposes a public test helper that takes the right lock —
    # don't reach into the dict directly here.
    keys._reset_shim_hit_counters_for_tests()
    server_mod._creds_cache = None


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run live integration tests against real Drive (requires OAuth creds).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live test; pass --live to enable")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def live_creds():
    """Load real Google OAuth creds for live tests.

    Reads from the same data dir the server uses
    (``~/.google-docs-mcp/`` by default, override with
    ``GOOGLE_DOCS_DATA_DIR``).
    """
    from google_docs_mcp.auth import default_data_dir, load_credentials
    data_dir = Path(os.environ.get("GOOGLE_DOCS_DATA_DIR") or default_data_dir())
    if not (data_dir / "token.json").exists():
        pytest.skip(
            f"no token.json in {data_dir} — live tests need real creds"
        )
    return load_credentials(data_dir)


@pytest.fixture
def created_docs(live_creds):
    """Test-scoped list to register doc IDs for auto-trash on teardown.

    Use instead of per-test try/finally cleanup. Tests append created
    doc IDs to the list; teardown trashes every one of them, ignoring
    individual failures. Already-trashed / already-deleted docs are
    fine — trash_drive_file is idempotent enough.

    Example:
        def test_x(live_creds, created_docs):
            d = make_doc_with_tabs(live_creds, "t", [...])
            created_docs.append(d["doc_id"])
            # ... assertions ...
            # No finally needed — fixture trashes on teardown.
    """
    from google_docs_mcp.drive_api import trash_drive_file
    ids: list[str] = []
    yield ids
    for doc_id in ids:
        try:
            trash_drive_file(live_creds, doc_id)
        except Exception:
            pass  # best-effort; don't fail teardown on cleanup hiccups


@pytest.fixture(scope="session")
def test_folder_id(live_creds):
    """Get-or-create a 'google-docs-mcp-tests' folder in My Drive.

    All live-test artifacts go in here. Created on first run; reused
    after that. Lets us isolate test debris from real docs and gives
    us a stable target for cleanup audits.
    """
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=live_creds)
    FOLDER_NAME = "google-docs-mcp-tests"

    # Look for existing
    resp = drive.files().list(
        q=(
            f"name = '{FOLDER_NAME}' and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        ),
        fields="files(id,name)",
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]

    # Create
    created = drive.files().create(
        body={
            "name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
        },
        fields="id",
    ).execute()
    return created["id"]
