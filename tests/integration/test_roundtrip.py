"""End-to-end: make → outline → trash → untrash → final-trash cleanup."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_create_outline_trash_untrash_roundtrip(live_creds):
    """Guards the basic happy path across 4 tools at once."""
    from google_docs_mcp.docs_api import (
        delete_tab,
        get_doc_outline,
        make_doc_with_tabs,
    )
    from google_docs_mcp.drive_api import trash_drive_file, untrash_drive_file

    # 1. CREATE — 3 tabs with icons
    spec = [
        {"title": "Alpha", "content": "alpha body", "icon_emoji": "🔀"},
        {"title": "Beta",  "content": "beta body",  "icon_emoji": "💡"},
        {"title": "Gamma", "content": "gamma body"},
    ]
    created = make_doc_with_tabs(live_creds, "test_roundtrip", spec)
    doc_id = created["doc_id"]
    try:
        # 2. OUTLINE — verify shape matches input
        outline = get_doc_outline(live_creds, doc_id)
        titles = [t["title"] for t in outline["tabs"]]
        assert "Alpha" in titles and "Beta" in titles and "Gamma" in titles
        assert outline["trashed"] is False

        # 3. TRASH
        t_result = trash_drive_file(live_creds, doc_id)
        assert t_result["trashed"] is True
        assert t_result["was_already_trashed"] is False

        # 4. UNTRASH
        u_result = untrash_drive_file(live_creds, doc_id)
        assert u_result["trashed"] is False
        assert u_result["was_already_active"] is False
    finally:
        # CLEANUP — trash for real (don't leave litter)
        try:
            trash_drive_file(live_creds, doc_id)
        except Exception:
            pass
