"""Multi-service tool-registration guards (M3 — last updated v2.1.5 / Phase C).

These tests verify the central M3 invariant: importing
``google_docs_mcp`` (and therefore ``server.py``) registers every
expected tool from its expected source file via the side-effect
imports at the bottom of server.py. The Round 1 landmine the spec
called out — "if a ``services/X/tools.py`` is missing from the
registration chain, its tools silently don't register" — is caught
by ``test_all_24_tools_register_from_correct_locations`` below.

**File location:** lives at ``tests/unit/services/test_tool_registration.py``
(NOT ``tests/unit/services/docs/test_tools.py``) as of Phase C —
test architect Round 4 flagged that the pre-rename location no
longer reflected contents once drive's registration guard moved in
during Phase B. Multi-service registration guards now have an honest
home; per-service folders (``tests/unit/services/{docs,drive,gas_deploy}/``)
hold consumer tests (``test_api.py``, ``test_tools.py``) that don't
need a multi-service view.

**Partition state after Phase C** (sum must equal 24):

  DOCS_SERVICE_TOOLS      = 12  (Phase A, services/docs/tools.py)
  DRIVE_SERVICE_TOOLS     =  4  (Phase B, services/drive/tools.py)
  GAS_DEPLOY_SERVICE_TOOLS =  1  (Phase C, services/gas_deploy/tools.py)
  NON_SERVICE_TOOLS       =  7  (still in server.py — admin /
                                  introspection / auth / signed URLs)
                          ─────
  EXPECTED_TOOLS          = 24

Consumer tests under ``tests/unit/services/<svc>/`` use
``with_google_api_client(InMemoryGoogleAPIClient({...}))`` per the
M2 pattern (PR #92). Per-credential isolation tests (e.g.
``test_distinct_credentials_get_distinct_resources``) STAY in
``tests/unit/test_google_clients.py`` against the production adapter
— ``InMemoryGoogleAPIClient`` deliberately does NOT key on
credentials, since per-credential behavior is a production-adapter
concern, not a port-shape concern.
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

# Gas-deploy-service tools — moved to ``services/gas_deploy/tools.py``
# in M3 Phase C (v2.1.5). Just the one tool today; the per-service
# folder pattern still applies for consistency with docs + drive.
GAS_DEPLOY_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gdocs_setup_apps_script",
})

# Remaining tools — still in server.py after Phase C. These are admin
# / introspection / auth / signed-URL tools that don't fit a
# Google-API-service folder; staying in server.py is intentional.
NON_SERVICE_TOOLS: frozenset[str] = frozenset({
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
# Pre-Phase-B ``NON_DOCS_TOOLS`` covered everything outside docs;
# Phase B + C partitioned it into per-service sets plus the
# stay-in-server remainder.
NON_DOCS_TOOLS: frozenset[str] = (
    DRIVE_SERVICE_TOOLS | GAS_DEPLOY_SERVICE_TOOLS | NON_SERVICE_TOOLS
)

EXPECTED_TOOLS: frozenset[str] = (
    DOCS_SERVICE_TOOLS
    | DRIVE_SERVICE_TOOLS
    | GAS_DEPLOY_SERVICE_TOOLS
    | NON_SERVICE_TOOLS
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
    """Importing ``google_docs_mcp.server`` MUST register all 24 tools.

    Post-Phase-C source split: 12 from ``services/docs/tools.py`` +
    4 from ``services/drive/tools.py`` + 1 from
    ``services/gas_deploy/tools.py`` (via side-effect imports at the
    bottom of server.py) + 7 still in server.py's own decorators.

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
    """The 7 stay-in-server tools must STILL be defined in server.py.

    Phase A (v2.1.3) moved 12 docs tools.
    Phase B (v2.1.4) moved 4 drive tools + the ``_run_batch`` helper.
    Phase C (v2.1.5) moved the 1 gas_deploy tool.

    The remaining 7 (admin / introspection / auth / signed-URL) don't
    fit a Google-API-service folder; staying in server.py is the
    intended end-state, not a deferred migration. If a future PR adds
    a NEW Google-API service, this set may shrink further; otherwise
    it stops here.
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


def test_gas_deploy_service_tools_register_from_services_gas_deploy_tools_module():
    """M3 Phase C (v2.1.5): the 1 gas_deploy-service tool must be defined
    in ``services/gas_deploy/tools.py``, NOT in server.py. Symmetric to
    the docs + drive registration guards.

    Critically, this guard ALSO verifies the gas_deploy/tools.py site
    preserves ``creds=False`` (the tool's own NeedsReauthError →
    structured-response path is load-bearing for the cloud-mode
    first-run UX; the standard ``creds=True`` envelope would silently
    break it). The ``__module__`` assertion catches a partial migration;
    the creds-opt-out check is asserted via the decorator's wrapper
    behavior in ``tests/unit/services/gas_deploy/test_tools.py``."""
    from google_docs_mcp.services.gas_deploy import tools as gas_deploy_tools

    for tool_name in GAS_DEPLOY_SERVICE_TOOLS:
        # The tool function must exist as a module-level attribute of
        # services/gas_deploy/tools.py.
        assert hasattr(gas_deploy_tools, tool_name), (
            f"{tool_name} not found in services.gas_deploy.tools — "
            f"M3 Phase C moved it; ensure it's defined there."
        )
        fn = getattr(gas_deploy_tools, tool_name)
        # The function's __module__ must point at services.gas_deploy.tools.
        assert fn.__module__ == "google_docs_mcp.services.gas_deploy.tools", (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"'google_docs_mcp.services.gas_deploy.tools'. M3 Phase C "
            f"moved this tool out of server.py."
        )
        # The function must NOT also exist at server module-level
        # (that would be a half-finished migration).
        from google_docs_mcp import server
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. M3 Phase C removed the server.py copy; ensure "
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


# ---------------------------------------------------------------------
# M4 (v2.2.0) — service= annotation invariant
# ---------------------------------------------------------------------
#
# @workspace_tool(service=...) is now the canonical decorator (the
# old @gdocs_tool is preserved as a deprecation-warning shim that
# delegates to workspace_tool(service="docs", ...)). Every registered
# tool MUST carry a service= value on its ToolAnnotations so future
# telemetry / per-service routing / observability can branch on it
# without a separate registry sweep.
#
# The expected mapping is per-file (Hex specialist's "service= is a
# constant per file" guidance): tools defined in services/<svc>/tools.py
# get service="<svc>"; tools that stay in server.py (admin /
# introspection / auth / signed URLs) all get service="admin"
# (single bucket — see PR body for the "why not split into 3" reasoning).


# The expected service= for every tool, derived from the per-file
# partition above. Pinning the full mapping explicitly catches both
# "tool moved to wrong file" AND "tool tagged with wrong service".
_EXPECTED_SERVICE_BY_TOOL: dict[str, str] = {
    **{name: "docs" for name in DOCS_SERVICE_TOOLS},
    **{name: "drive" for name in DRIVE_SERVICE_TOOLS},
    **{name: "gas_deploy" for name in GAS_DEPLOY_SERVICE_TOOLS},
    # M4 judgment call: all 7 stay-in-server tools share service="admin".
    # The 3-way split (introspection / admin / auth) was considered and
    # rejected — adds enum values without behavioral payoff. See PR body.
    **{name: "admin" for name in NON_SERVICE_TOOLS},
}


def _registered_tools_by_name() -> dict:
    """Snapshot of every registered tool, keyed by name."""
    from google_docs_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


def test_every_tool_carries_service_annotation():
    """M4 invariant: every registered tool MUST have a ``service=`` value
    on its ToolAnnotations. Without this, future per-service telemetry /
    routing / observability features can't branch on which service a
    tool belongs to without re-deriving the partition from filesystem
    layout — which the per-service-folder refactor exists to AVOID.

    ToolAnnotations is pydantic-backed with ``extra: "allow"``, so the
    field rides as an extra attribute and round-trips via ``getattr``.
    """
    registered = _registered_tools_by_name()
    missing_service: list[str] = []
    for tool_name in EXPECTED_TOOLS:
        tool = registered[tool_name]
        # ``getattr`` returns ``None`` when the field is genuinely absent;
        # an empty string also counts as "not set" for our purposes.
        service = getattr(tool.annotations, "service", None)
        if not service:
            missing_service.append(tool_name)
    assert not missing_service, (
        f"Tools missing service= annotation: {sorted(missing_service)}. "
        f"M4 (v2.2.0) made service= REQUIRED on @workspace_tool — any "
        f"tool registered without it broke the M4 invariant. Most likely "
        f"cause: a new tool was added with @workspace_tool(...) but the "
        f"author forgot the service= kwarg, OR a tool was migrated to a "
        f"per-service folder without flipping its decorator from "
        f"@gdocs_tool to @workspace_tool."
    )


def test_service_annotation_matches_expected_per_file_partition():
    """The service= value of every tool MUST match the per-file mapping.

    Catches three bug classes:

    1. Tool tagged with wrong service= literal (e.g.
       services/drive/tools.py site decorated with
       ``service="docs"``). Pure copy-paste hazard.
    2. Tool moved to wrong per-service folder (e.g. a docs tool ends
       up in services/drive/tools.py and gets service="drive" by
       Hex-specialist-recommended per-file-constant rule).
    3. ``@gdocs_tool`` deprecation shim accidentally invoked on a
       non-docs tool — it delegates to ``service="docs"``, which is
       only correct if the caller's intent really was docs. Anywhere
       else, the test fires.
    """
    registered = _registered_tools_by_name()
    mismatches: list[str] = []
    for tool_name in EXPECTED_TOOLS:
        expected = _EXPECTED_SERVICE_BY_TOOL[tool_name]
        actual = getattr(registered[tool_name].annotations, "service", None)
        if actual != expected:
            mismatches.append(
                f"{tool_name}: expected service={expected!r}, got {actual!r}"
            )
    assert not mismatches, (
        "service= annotations don't match the per-file expected partition:\n  "
        + "\n  ".join(mismatches)
        + "\nFix the offending @workspace_tool(service=...) call site, or "
        "(if the move was intentional) update _EXPECTED_SERVICE_BY_TOOL "
        "in this file."
    )


def test_no_in_repo_callers_use_deprecated_gdocs_tool_decorator():
    """No in-repo source file MAY still use the deprecated ``@gdocs_tool``.

    M4 ships ``@gdocs_tool`` as a one-release backward-compat shim
    (delegates to ``workspace_tool(service="docs", ...)`` and emits
    a DeprecationWarning at call time). Every in-repo call site has
    been migrated to ``@workspace_tool(service=..., ...)``. This test
    makes the migration completeness explicit so a future regression
    — someone re-adding ``@gdocs_tool`` to a tools.py file — is
    caught instantly.

    Static lint approach (vs. runtime warning-capture): a runtime
    test that does ``importlib.reload`` plus ``warnings.catch_warnings``
    has to nuke the ``sys.modules`` cache for ``google_docs_mcp.*``,
    which then breaks ``test_google_api_client.py`` (and similar)
    that depend on stable module-level singletons set up earlier in
    the test session. The static-grep approach has identical coverage
    for this specific invariant (any file with ``@gdocs_tool(``
    fires the shim when imported) without the cross-test pollution.
    """
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[3] / "src" / "google_docs_mcp"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        # decorators.py legitimately defines ``def gdocs_tool(...)`` as
        # the deprecation shim — that's not a decoration call site.
        # The pattern we're banning is the AT-prefix usage that
        # actually invokes the shim.
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("@gdocs_tool("):
                rel = path.relative_to(src_root.parent)
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        f"Deprecated @gdocs_tool decoration call sites still present "
        f"in src/: {offenders}. M4 (v2.2.0) migrated all 24 call sites "
        f"to @workspace_tool(service=..., ...). Migrate the offender "
        f"and add the required service= kwarg (per-file constant per "
        f"the layout: docs / drive / gas_deploy / admin)."
    )
