"""Per-service docs/ tool registration + import tests (v2.1.3 / M3 POC).

These tests verify the M3 POC's central invariant: importing
``google_docs_mcp`` (and therefore ``server.py``) registers all 12
docs-service tools via the side-effect import at the bottom of
server.py. The Round 1 landmine the spec warned about — "if a
services/X/tools.py is missing from the chain, its tools silently
don't register" — is caught by this file's fixture-discovery test.

After M3 expands to drive/ + gas_deploy/, the test architect's
critique applies: consumer tests under tests/unit/services/<svc>/
use ``with_google_api_client(InMemoryGoogleAPIClient({...}))``
(M2 / PR #92). Per-credential isolation tests (e.g.
``test_distinct_credentials_get_distinct_resources``) STAY in
``tests/unit/test_google_clients.py`` against the production adapter
— ``InMemoryGoogleAPIClient`` deliberately does NOT key on
credentials, since the per-credential behavior is a production-adapter
concern not a port-shape concern.
"""
from __future__ import annotations

import asyncio


# The full set of tool names this server registers, partitioned by
# the M3 POC migration line. Pinning both halves explicitly makes
# regressions (a renamed tool, a forgotten registration, a tool
# moved to the wrong file) visible at CI time.

DOCS_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gdocs_make_tabbed_doc",
    "gdocs_add_tabs",
    "gdocs_get_doc_outline",
    "gdocs_read_doc",
    "gdocs_append_to_tab",
    "gdocs_tab_existing_doc",
    "gdocs_rename_tab",
    "gdocs_get_tab_url",
    "gdocs_delete_tab",
    "gdocs_replace_all_text",
    "gdocs_set_tab_icons",
    "gdocs_preview_tab_split",
})

# Drive-service tools — moved to ``services/drive/tools.py`` in M3 Phase B (v2.1.4).
DRIVE_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gdocs_find_doc_by_title",
    "gdocs_move_to_folder",
    "gdocs_trash_file",
    "gdocs_untrash_file",
})

# Remaining tools — still in server.py for M3 Phase B. After gas_deploy/
# migrates in Phase C, this set shrinks again.
NON_SERVICE_TOOLS: frozenset[str] = frozenset({
    # gas_deploy (next to migrate in Phase C)
    "gdocs_setup_apps_script",
    # admin / introspection / local-only
    "gdocs_server_info",
    "gdocs_test_manifest",
    "gdocs_guide",
    "gdocs_help",
    "gdocs_admin_audit",
    # auth / signed URLs
    "gdocs_get_signed_upload_url",
    "gdocs_reset_authorization",
})

# Backward-compat alias for any external readers / future test sweeps.
# ``NON_DOCS_TOOLS`` was the Phase A name; Phase B partitions it into
# ``DRIVE_SERVICE_TOOLS`` (moved) + ``NON_SERVICE_TOOLS`` (still in server.py).
NON_DOCS_TOOLS: frozenset[str] = DRIVE_SERVICE_TOOLS | NON_SERVICE_TOOLS

EXPECTED_TOOLS: frozenset[str] = (
    DOCS_SERVICE_TOOLS | DRIVE_SERVICE_TOOLS | NON_SERVICE_TOOLS
)


def _registered_tool_names() -> set[str]:
    """Snapshot of currently-registered tool names from the live mcp."""
    from google_docs_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


# ---------------------------------------------------------------------
# Fixture-discovery: all 24 tools register on import
# ---------------------------------------------------------------------


def test_all_24_tools_register_from_correct_locations():
    """Importing ``google_docs_mcp.server`` MUST register all 24 tools —
    12 from ``services/docs/tools.py`` (via the side-effect import at
    the bottom of server.py) and 12 from server.py's own decorators.

    Catches the Round 1 landmine the M3 POC spec called out: if a
    services/X/tools.py is missing from the registration chain (e.g.
    server.py forgot the bottom import, or a typo in the module name),
    its tools silently don't register — giving "works in stdio, 401s
    in HTTP" surprises in production.
    """
    registered = _registered_tool_names()

    missing = EXPECTED_TOOLS - registered
    unexpected = registered - EXPECTED_TOOLS

    assert not missing, (
        f"Expected tools NOT registered: {sorted(missing)}. "
        f"Most likely cause: ``services/docs/tools.py`` was added but the "
        f"``from .services.docs import tools`` line at the bottom of "
        f"server.py is missing or misspelled. See M3 POC migration plan "
        f"in docs/ARCHITECTURE.md §5.1."
    )
    assert not unexpected, (
        f"Unexpected tools registered (not in EXPECTED_TOOLS): {sorted(unexpected)}. "
        f"Either a new tool was added without updating this guard's "
        f"DOCS_SERVICE_TOOLS / NON_DOCS_TOOLS set, or a tool was renamed."
    )
    assert len(registered) == 24, (
        f"Tool count drift: expected 24, got {len(registered)}. "
        f"Tools: {sorted(registered)}"
    )


def test_docs_service_tools_register_from_services_docs_tools_module():
    """Specific guard for the M3 POC: the 12 docs-service tools must
    be defined in ``services/docs/tools.py``, NOT in server.py. This
    catches a regression where someone re-adds a docs tool to server.py
    by accident (e.g. a copy-paste from a pre-M3 commit)."""
    import inspect

    from google_docs_mcp.services.docs import tools as docs_tools

    for tool_name in DOCS_SERVICE_TOOLS:
        # The tool function must exist as a module-level attribute of
        # services/docs/tools.py.
        assert hasattr(docs_tools, tool_name), (
            f"{tool_name} not found in services.docs.tools — "
            f"the M3 POC moved it; ensure it's defined there."
        )
        fn = getattr(docs_tools, tool_name)
        # The function's __module__ must point at services.docs.tools.
        assert fn.__module__ == "google_docs_mcp.services.docs.tools", (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"'google_docs_mcp.services.docs.tools'. The M3 POC moved "
            f"this tool out of server.py."
        )
        # The function must NOT also exist at server module-level
        # (that would be a half-finished migration).
        from google_docs_mcp import server
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. The M3 POC removed the server.py copy; ensure "
            f"the deletion is complete."
        )


def test_non_service_tools_still_register_from_server_py():
    """The remaining (non-service-folder) tools must STILL be defined in
    server.py. After each M3 phase migrates another service group, this
    test's ``NON_SERVICE_TOOLS`` set shrinks.

    Phase A (v2.1.3) moved 12 docs tools.
    Phase B (v2.1.4) moved 4 drive tools + ``_run_batch`` helper.
    Phase C (next) will move 1 gas_deploy tool — at which point the
    Phase-C author updates ``DRIVE_SERVICE_TOOLS``/``NON_SERVICE_TOOLS``
    here to add ``GAS_DEPLOY_SERVICE_TOOLS``.
    """
    from google_docs_mcp import server

    for tool_name in NON_SERVICE_TOOLS:
        assert hasattr(server, tool_name), (
            f"{tool_name} not found in server.py — this tool was NOT "
            f"slated for the current M3 phase migration. Either move it "
            f"to the appropriate services/X/tools.py (and update "
            f"NON_SERVICE_TOOLS here) or restore the definition in "
            f"server.py."
        )


def test_drive_service_tools_register_from_services_drive_tools_module():
    """M3 Phase B (v2.1.4): the 4 drive-service tools must be defined in
    ``services/drive/tools.py``, NOT in server.py. Symmetric to
    ``test_docs_service_tools_register_from_services_docs_tools_module``
    in concept — pins the no-shadow invariant + ``__module__`` invariant
    that the test architect's review of Phase A called out.

    Catches a regression where someone re-adds a drive tool to server.py
    by accident (e.g. a copy-paste from a pre-Phase-B commit)."""
    from google_docs_mcp.services.drive import tools as drive_tools

    for tool_name in DRIVE_SERVICE_TOOLS:
        # The tool function must exist as a module-level attribute of
        # services/drive/tools.py.
        assert hasattr(drive_tools, tool_name), (
            f"{tool_name} not found in services.drive.tools — "
            f"M3 Phase B moved it; ensure it's defined there."
        )
        fn = getattr(drive_tools, tool_name)
        # The function's __module__ must point at services.drive.tools.
        assert fn.__module__ == "google_docs_mcp.services.drive.tools", (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"'google_docs_mcp.services.drive.tools'. M3 Phase B moved "
            f"this tool out of server.py."
        )
        # The function must NOT also exist at server module-level
        # (that would be a half-finished migration).
        from google_docs_mcp import server
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. M3 Phase B removed the server.py copy; ensure "
            f"the deletion is complete."
        )


# ---------------------------------------------------------------------
# Sanity: a docs-service tool is callable through the live registry
# ---------------------------------------------------------------------


def test_gdocs_get_tab_url_works_through_registration():
    """End-to-end sanity: call a docs-service tool through its
    registered-from-services-folder path. ``gdocs_get_tab_url`` is the
    cleanest target — pure URL composition, no Google API call needed.
    """
    from google_docs_mcp.services.docs.tools import gdocs_get_tab_url

    result = gdocs_get_tab_url("DOC123", "TAB456")
    assert result == {
        "doc_id": "DOC123",
        "tab_id": "TAB456",
        "url": "https://docs.google.com/document/d/DOC123/edit?tab=TAB456",
    }
