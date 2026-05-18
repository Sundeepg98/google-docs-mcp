"""Shared pytest config + fixtures.

Live (``@pytest.mark.live``) tests are opt-in — they hit real Drive.
Run with ``pytest --live`` to enable; default skips them so unit
tests stay fast and CI-friendly.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


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
