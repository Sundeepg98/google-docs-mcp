"""Cross-tool consistency: find_doc_by_title's owned_by_app MUST match
whether trash_drive_file would actually succeed.

This is the v0.19.0 regression guard. Before v0.19.1 the find tool
used capabilities.canTrash (a USER-LEVEL signal) which disagreed with
the actual write probe — so a caller could see owned_by_app=true and
then get app_not_authorized on trash. Permanent guard against that.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_owned_by_app_agrees_with_trash_outcome(live_creds):
    from google_docs_mcp.docs_api import make_doc_with_tabs
    from google_docs_mcp.drive_api import find_doc_by_title, trash_drive_file

    # Create an app-owned file with a distinctive title we can search for.
    unique_title = "consistency_test_app_owned_doc"
    created = make_doc_with_tabs(
        live_creds, unique_title,
        [{"title": "x", "content": "x"}],
    )
    doc_id = created["doc_id"]

    try:
        # Find by title with verify_writable on (the default).
        search = find_doc_by_title(live_creds, unique_title, exact=True)
        match = next(
            (m for m in search["matches"] if m["file_id"] == doc_id), None
        )
        assert match is not None, "freshly-created doc not in search results"
        owned_by_app = match["owned_by_app"]

        # Cross-check: actually run the trash and see if reason is set.
        trash_result = trash_drive_file(live_creds, doc_id)
        trash_succeeded = trash_result.get("reason") is None

        assert owned_by_app == trash_succeeded, (
            f"owned_by_app={owned_by_app} but trash_succeeded={trash_succeeded} "
            f"— THE EXACT v0.19.0 BUG. Tools disagree about app ownership."
        )
    finally:
        try:
            trash_drive_file(live_creds, doc_id)
        except Exception:
            pass
