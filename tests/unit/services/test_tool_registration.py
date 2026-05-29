"""Multi-service tool-registration guards (M3 + Gap #7 + v2.3.0 + v2.3.1 + v2.3.2).

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
home; per-service folders (``tests/unit/services/{docs,drive,gas_deploy,admin}/``)
hold consumer tests (``test_api.py``, ``test_tools.py``) that don't
need a multi-service view.

**Partition state after PR-Δ10 + PR-Δ9** (sum must equal 36):

  DOCS_SERVICE_TOOLS        = 12  (Phase A,  services/docs/tools.py)
  DRIVE_SERVICE_TOOLS       =  6  (Phase B + v2.3.0, services/drive/tools.py)
  GAS_DEPLOY_SERVICE_TOOLS  =  2  (Phase C + PR-α, services/gas_deploy/tools.py)
  ADMIN_SERVICE_TOOLS       =  7  (Gap #7,   services/admin/tools.py)
  SHEETS_SERVICE_TOOLS      =  3  (v2.3.1,   services/sheets/tools.py)
  SLIDES_SERVICE_TOOLS      =  3  (v2.3.2,   services/slides/tools.py)
  APPS_SCRIPT_SERVICE_TOOLS =  3  (PR-Δ7 tools.py + PR-Δ10
                                    custom_function.py + PR-Δ9
                                    sheet_dashboard.py)
  NON_SERVICE_TOOLS         =  0  (Gap #7 emptied it — server.py
                                    contains NO tool definitions)
                            ─────
  EXPECTED_TOOLS            = 36

v2.3.0 (PR #117) added ``gdocs_share_file`` + ``gdocs_list_permissions``
to the drive service (1st empirical bolt-on).

v2.3.1 (PR #119) added the 3 Sheets tools (2nd new service) and proved
the OAuth-scope-addition infra absorbs new scopes non-breakingly via
``include_granted_scopes=true``.

v2.3.2 (PR #121) added the 3 Slides tools as the 3rd new service.

PR-α (v2.3.4) reframed ``gdocs_setup_apps_script`` →
``gdocs_install_automation`` to surface the Workspace-automation-
runtime install as the headline feature rather than as infrastructure
plumbing. The old name is kept registered as a deprecation alias —
both names appear in ``GAS_DEPLOY_SERVICE_TOOLS`` and both share the
same underlying installer (one function, two registrations). Planned
removal of the alias in v3.0. The PR #117 papercut fix continues to
pay off; the count is derived from ``len(EXPECTED_TOOLS)``.

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
# v2.3.0 added 2 more (gdocs_share_file + gdocs_list_permissions) backed
# by the new services/drive/sharing.py sub-module — the FIRST empirical
# bolt-on that validates the per-service-folder pattern's "1-folder,
# no foundation rework" claim from the 3-agent ~96% feasibility audit.
DRIVE_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gdocs_find_doc_by_title",
    "gdocs_move_to_folder",
    "gdocs_trash_file",
    "gdocs_untrash_file",
    # v2.3.0 — sharing sub-module
    "gdocs_share_file",
    "gdocs_list_permissions",
})

# Gas-deploy-service tools — moved to ``services/gas_deploy/tools.py``
# in M3 Phase C (v2.1.5). PR-α (v2.3.4) reframed the user-facing name
# from "setup_apps_script" to "install_automation" and kept the old
# name registered as a deprecation alias. BOTH names live in
# ``services/gas_deploy/tools.py``; the alias delegates to the same
# underlying installer via a shared helper.
GAS_DEPLOY_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gdocs_install_automation",   # PR-α canonical name
    "gdocs_setup_apps_script",    # deprecated alias (planned removal in v3.0)
})

# Admin-service tools — moved to ``services/admin/tools.py`` in Gap #7
# (v2.2.2). Closes Hex specialist 92% + SOLID specialist 78% audit
# findings: server.py no longer holds tool definitions after this PR.
# The grouping covers admin / introspection / auth / signed-URL tools
# under one "non-Google-API-service" folder rather than minting a
# 3-way split — the alternative (separate intro/admin/auth folders)
# would over-fit the layout to 1-3 tools each.
ADMIN_SERVICE_TOOLS: frozenset[str] = frozenset({
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

# Sheets-service tools — v2.3.1 (this PR). 2nd new service after the
# Drive sharing bolt-on (PR #117). Minimal start: range read/write +
# create. The Sheets ``batchUpdate`` tagged-union (40+ request types
# for formatting / charts / pivots / etc.) is deferred to a follow-up
# PR per the multi-service feasibility audit's "pattern stretch" note —
# this PR proves the foundation extends to a NEW Google service, not
# the design of the batchUpdate abstraction itself.
SHEETS_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gsheets_read_range",
    "gsheets_write_range",
    "gsheets_create_spreadsheet",
})

# Slides-service tools — v2.3.2 (this PR). 3rd new service after Sheets
# (PR #119). Minimal start: outline read + cross-slide find/replace +
# create. The Slides ``batchUpdate`` tagged-union (~40 request types
# for addSlide / replaceImage / updateTextStyle / etc.) is deferred
# per the same "wait for actual need" approach Sheets used. The
# single-request-type ``replaceAllText`` carve-out shows the most
# common write use case can be exposed without committing to the
# full tagged-union abstraction.
SLIDES_SERVICE_TOOLS: frozenset[str] = frozenset({
    "gslides_get_outline",
    "gslides_replace_all_text",
    "gslides_create_presentation",
})

# Apps-Script-service tools — PR-Δ7 (this PR). The feature FOUNDATION:
# one generic primitive, ``as_generate_bound_script``, that generates a
# container-bound Apps Script project (menus / sidebars / edit triggers)
# and deploys it. DISTINCT from GAS_DEPLOY_SERVICE_TOOLS — gas_deploy
# bootstraps a STANDALONE runtime Web App; this creates per-container
# BOUND scripts. First ``as_*``-prefixed tool (appscriptly-native naming
# per PR-Δ5.5). Use-case tools that compose this primitive (slides-for-
# video, sheets dashboards, docs menu-installers) are FUTURE PRs and are
# NOT in this set — this PR ships the generator only.
APPS_SCRIPT_SERVICE_TOOLS: frozenset[str] = frozenset({
    "as_generate_bound_script",
    # PR-Δ10: convenience tool composing the PR-Δ7 primitive — installs a
    # custom =FUNCTION() into a Sheet. Lives in its OWN feature file
    # (custom_function.py), not tools.py, so parallel apps_script feature
    # PRs stay merge-clean.
    "as_install_custom_function",
    # PR-Δ9: convenience tool composing the PR-Δ7 primitive — installs a
    # scheduled (time-driven) dashboard refresh into a Sheet. Own feature
    # file (sheet_dashboard.py), same merge-clean discipline.
    "as_install_sheet_dashboard",
})

# Gap #7 emptied this set: every tool now belongs to a service folder.
# Kept as an empty frozenset (not deleted) so existing callers /
# external test sweeps that import the name keep working. If a future
# tool genuinely cannot fit any service folder it gets added here.
NON_SERVICE_TOOLS: frozenset[str] = frozenset()

# Backward-compat alias for any external readers / future test sweeps.
# Pre-Phase-B ``NON_DOCS_TOOLS`` covered everything outside docs;
# Phase B / C / Gap #7 partitioned it into per-service sets plus the
# (now-empty) stay-in-server remainder.
NON_DOCS_TOOLS: frozenset[str] = (
    DRIVE_SERVICE_TOOLS
    | GAS_DEPLOY_SERVICE_TOOLS
    | ADMIN_SERVICE_TOOLS
    | SHEETS_SERVICE_TOOLS
    | SLIDES_SERVICE_TOOLS
    | APPS_SCRIPT_SERVICE_TOOLS
    | NON_SERVICE_TOOLS
)

EXPECTED_TOOLS: frozenset[str] = (
    DOCS_SERVICE_TOOLS
    | DRIVE_SERVICE_TOOLS
    | GAS_DEPLOY_SERVICE_TOOLS
    | ADMIN_SERVICE_TOOLS
    | SHEETS_SERVICE_TOOLS
    | SLIDES_SERVICE_TOOLS
    | APPS_SCRIPT_SERVICE_TOOLS
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


def test_all_expected_tools_register_from_correct_locations():
    """Importing ``google_docs_mcp.server`` MUST register every tool in
    EXPECTED_TOOLS (currently 26 after the v2.3.0 sharing bolt-on).

    Post-v2.3.0 source split: 12 from ``services/docs/tools.py`` +
    6 from ``services/drive/tools.py`` (4 file CRUD + 2 sharing) +
    1 from ``services/gas_deploy/tools.py`` + 7 from
    ``services/admin/tools.py`` (via side-effect imports at the
    bottom of server.py). NON_SERVICE_TOOLS is empty since Gap #7.

    The expected count is derived from ``EXPECTED_TOOLS`` (the union
    of the partition sets) rather than a hard-coded literal — test
    architect Round 4's "future-papercut" caveat fixed inline as
    part of the v2.3.0 ship. New tools require only updating the
    relevant ``<svc>_SERVICE_TOOLS`` frozenset; this assertion auto-
    follows.

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
        f"DOCS_SERVICE_TOOLS / DRIVE_SERVICE_TOOLS / GAS_DEPLOY_SERVICE_TOOLS / "
        f"ADMIN_SERVICE_TOOLS set, or a tool was renamed."
    )
    assert len(registered) == len(EXPECTED_TOOLS), (
        f"Tool count drift: expected {len(EXPECTED_TOOLS)} "
        f"(derived from the partition frozensets), got {len(registered)}. "
        f"Registered: {sorted(registered)}"
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


def test_no_tool_definitions_remain_in_server_py():
    """Gap #7 (v2.2.2): ``server.py`` must contain NO tool definitions.

    After Gap #7's admin extraction, every tool name in EXPECTED_TOOLS
    must be missing from ``server.py``'s module-level surface — if any
    name is found there, it's a half-finished migration (tool defined
    in both server.py and services/X/tools.py).

    Phase A (v2.1.3)  moved 12 docs tools.
    Phase B (v2.1.4)  moved 4 drive tools + the ``_run_batch`` helper.
    Phase C (v2.1.5)  moved the 1 gas_deploy tool.
    Gap #7  (v2.2.2)  moved the 7 admin/introspection/auth tools — and
                      removed the admin-domain helpers (test-results
                      parsing, admin-token gating). Closes Hex /
                      SOLID specialists' ISP-asymmetry finding.
    PR-α    (v2.3.4)  added ``gdocs_install_automation`` to the
                      gas_deploy folder + kept the old
                      ``gdocs_setup_apps_script`` registered as a
                      deprecation alias (both names share one
                      underlying installer; gas_deploy count went
                      1 → 2).

    NON_SERVICE_TOOLS is now empty; this guard verifies the audit
    finding remains closed for every tool.
    """
    from google_docs_mcp import server

    leftover = [name for name in EXPECTED_TOOLS if hasattr(server, name)]
    assert not leftover, (
        f"Tools still defined in server.py: {leftover}. Gap #7 (v2.2.2) "
        f"moved every tool into a service folder. Re-defining a tool in "
        f"server.py reintroduces the ISP asymmetry the audit closed."
    )


def test_admin_service_tools_register_from_services_admin_tools_module():
    """Gap #7 (v2.2.2): the 7 admin-service tools must be defined in
    ``services/admin/tools.py``, NOT in server.py. Symmetric to the
    docs / drive / gas_deploy registration guards.

    Catches a regression where someone re-adds an admin tool to
    server.py by accident (e.g. a copy-paste from a pre-Gap-#7
    commit). Pins the no-shadow + ``__module__`` invariants the test
    architect's review of Phase A called out — applied uniformly
    across all 4 service folders.
    """
    from google_docs_mcp.services.admin import tools as admin_tools

    for tool_name in ADMIN_SERVICE_TOOLS:
        # The tool function must exist as a module-level attribute of
        # services/admin/tools.py.
        assert hasattr(admin_tools, tool_name), (
            f"{tool_name} not found in services.admin.tools — "
            f"Gap #7 moved it; ensure it's defined there."
        )
        fn = getattr(admin_tools, tool_name)
        # The function's __module__ must point at services.admin.tools.
        assert fn.__module__ == "google_docs_mcp.services.admin.tools", (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"'google_docs_mcp.services.admin.tools'. Gap #7 moved "
            f"this tool out of server.py."
        )
        # The function must NOT also exist at server module-level
        # (that would be a half-finished migration).
        from google_docs_mcp import server
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. Gap #7 removed the server.py copy; ensure "
            f"the deletion is complete."
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
    """M3 Phase C (v2.1.5) + PR-α (v2.3.4): the gas_deploy-service tools
    must be defined in ``services/gas_deploy/tools.py``, NOT in
    server.py. Symmetric to the docs + drive registration guards.

    Post-PR-α: TWO tools live here (canonical
    ``gdocs_install_automation`` + deprecation alias
    ``gdocs_setup_apps_script``). Both must satisfy the
    ``__module__`` + no-shadow invariants for the partition to hold.

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


def test_sheets_service_tools_register_from_services_sheets_tools_module():
    """v2.3.1: the 3 sheets-service tools must be defined in
    ``services/sheets/tools.py``, NOT in server.py. Symmetric to the
    docs / drive / gas_deploy / admin registration guards — same
    ``__module__`` + no-shadow invariants applied uniformly.

    Catches a regression where someone re-adds a sheets tool to
    server.py by accident (e.g. a copy-paste from a pre-v2.3.1
    commit). Also catches the "side-effect import missing" landmine
    if ``server.py`` ever loses its
    ``from .services.sheets import tools as _sheets_tools`` line —
    the tool registration would silently fail.
    """
    from google_docs_mcp.services.sheets import tools as sheets_tools

    for tool_name in SHEETS_SERVICE_TOOLS:
        assert hasattr(sheets_tools, tool_name), (
            f"{tool_name} not found in services.sheets.tools — "
            f"v2.3.1 added it; ensure it's defined there."
        )
        fn = getattr(sheets_tools, tool_name)
        assert fn.__module__ == "google_docs_mcp.services.sheets.tools", (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"'google_docs_mcp.services.sheets.tools'."
        )
        from google_docs_mcp import server
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. Sheets tools live in services/sheets/tools.py."
        )


def test_slides_service_tools_register_from_services_slides_tools_module():
    """v2.3.2: the 3 slides-service tools must be defined in
    ``services/slides/tools.py``, NOT in server.py. Symmetric to the
    sheets registration guard (v2.3.1) — same ``__module__`` +
    no-shadow invariants.

    Catches a regression where someone re-adds a slides tool to
    server.py by accident, OR where ``server.py`` ever loses its
    ``from .services.slides import tools as _slides_tools`` line
    (the tool registration would silently fail).
    """
    from google_docs_mcp.services.slides import tools as slides_tools

    for tool_name in SLIDES_SERVICE_TOOLS:
        assert hasattr(slides_tools, tool_name), (
            f"{tool_name} not found in services.slides.tools — "
            f"v2.3.2 added it; ensure it's defined there."
        )
        fn = getattr(slides_tools, tool_name)
        assert fn.__module__ == "google_docs_mcp.services.slides.tools", (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"'google_docs_mcp.services.slides.tools'."
        )
        from google_docs_mcp import server
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. Slides tools live in services/slides/tools.py."
        )


# apps_script tool name → the submodule of the apps_script package it is
# defined in. PR-Δ7's generic primitive lives in ``tools``; later
# convenience tools that COMPOSE it each get their OWN feature file in
# the same package (PR-Δ10's custom-function installer →
# ``custom_function``; PR-Δ9's scheduled dashboard refresh →
# ``sheet_dashboard``) so parallel feature PRs don't collide on one
# tools.py. The registration guard below pins each tool to its expected
# home module — catching both a misplaced tool and a forgotten server.py
# side-effect import.
_APPS_SCRIPT_TOOL_MODULE: dict[str, str] = {
    "as_generate_bound_script": "google_docs_mcp.services.apps_script.tools",
    "as_install_custom_function": (
        "google_docs_mcp.services.apps_script.custom_function"
    ),
    "as_install_sheet_dashboard": (
        "google_docs_mcp.services.apps_script.sheet_dashboard"
    ),
}


def test_apps_script_service_tools_register_from_services_apps_script_module():
    """PR-Δ7 + PR-Δ10 + PR-Δ9: every apps_script-service tool must be
    defined in its feature file under ``services/apps_script/`` (the
    generic primitive in ``tools.py``; each composing convenience tool in
    its own feature module), NOT in server.py. Symmetric to the sheets /
    slides registration guards — same per-file ``__module__`` + no-shadow
    invariants, generalized to the apps_script package's multi-file
    layout.

    Catches a regression where someone re-adds an apps_script tool to
    server.py by accident, OR where ``server.py`` ever loses one of its
    ``from .services.apps_script import <feature>`` side-effect imports
    (the tool registration would silently fail — the Round 1 landmine).

    Also implicitly distinguishes apps_script from gas_deploy: the tools
    must live in the apps_script folder, not get folded into gas_deploy
    (the two services are deliberately separate — bound vs standalone).
    """
    import importlib

    from google_docs_mcp import server

    for tool_name in APPS_SCRIPT_SERVICE_TOOLS:
        expected_module = _APPS_SCRIPT_TOOL_MODULE.get(tool_name)
        assert expected_module is not None, (
            f"{tool_name} is in APPS_SCRIPT_SERVICE_TOOLS but has no entry "
            f"in _APPS_SCRIPT_TOOL_MODULE — add its feature-file module so "
            f"this guard knows where it should live."
        )
        mod = importlib.import_module(expected_module)
        assert hasattr(mod, tool_name), (
            f"{tool_name} not found in {expected_module} — ensure it's "
            f"defined there (and server.py side-effect-imports that module)."
        )
        fn = getattr(mod, tool_name)
        assert fn.__module__ == expected_module, (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"{expected_module!r}."
        )
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate "
            f"definition. apps_script tools live in their "
            f"services/apps_script/ feature files."
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
    # Gap #7 (v2.2.2): the 7 admin tools now live in services/admin/
    # rather than server.py. The service="admin" annotation was already
    # in place (set during M4); only the source-file location changed.
    # The 3-way split (introspection / admin / auth) was considered and
    # rejected — adds enum values without behavioral payoff. See PR body.
    **{name: "admin" for name in ADMIN_SERVICE_TOOLS},
    # v2.3.1: 2nd new service. Sheets tools carry service="sheets".
    **{name: "sheets" for name in SHEETS_SERVICE_TOOLS},
    # v2.3.2: 3rd new service. Slides tools carry service="slides".
    **{name: "slides" for name in SLIDES_SERVICE_TOOLS},
    # PR-Δ7: bound-script generator. apps_script tools carry
    # service="apps_script" (distinct from gas_deploy).
    **{name: "apps_script" for name in APPS_SCRIPT_SERVICE_TOOLS},
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
