"""Google Docs MCP Server with native Tabs support.

Exposes MCP tools for working with native Google Docs tabs:
``gdocs_make_tabbed_doc``, ``gdocs_add_tabs``, ``gdocs_get_doc_outline``,
``gdocs_append_to_tab``, and ``gdocs_tab_existing_doc``.

The same entry point also implements one-off CLI commands for the
Apps Script setup needed by ``gdocs_tab_existing_doc``; see the
``cli`` module for those.
"""
from __future__ import annotations

import hmac
import logging
import os
import sys
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from .auth import default_data_dir, load_credentials
from .crypto import DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, sign_upload_url
from .services.drive.api import (
    find_doc_by_title as _find_doc_by_title,
    move_to_folder as _move_to_folder,
    trash_drive_file as _trash_drive_file,
    untrash_drive_file as _untrash_drive_file,
)
from .errors import friendly_http_error_message
from .tool_schemas import (
    GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
    GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
    GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    GDOCS_GUIDE_OUTPUT_SCHEMA,
    GDOCS_HELP_OUTPUT_SCHEMA,
    GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
    GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    # GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA: now imported by
    # services/gas_deploy/tools.py (M3 Phase C extraction).
    GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
    GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
    GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
)
# v1.1+ multi-tenant cloud auth — imported lazily-via-function so stdio
# users without the OAuth env vars don't trip import-time errors.
from .credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
# GAS_DEPLOY_SCOPES: now imported by services/gas_deploy/tools.py
# (M3 Phase C — gdocs_setup_apps_script moved out of server.py).
from .keys import (
    get_first_call_timestamps,
    get_key,
    get_shim_hit_counters,
    get_total_call_counters,
)
from .oauth_google import resolve_runtime_oauth_config
# setup_apps_script_auto / setup_apps_script_for_user: now imported by
# services/gas_deploy/tools.py (M3 Phase C extraction).

_SERVER_INSTRUCTIONS = """\
appscriptly — Workspace Automation MCP. Generates persistent workflows
(time-driven jobs, custom menus, reactive automations) that live IN
your Google Workspace and run on Google's infrastructure. Also creates,
edits, reads, and manages Google Docs with native sidebar Tabs
(Google's October 2024 feature) plus Sheets, Slides, Drive, and Apps
Script project management.

Tools are prefixed by DOMAIN: ``gdocs_`` (Google Docs / native tabs),
``gdrive_`` (Drive file management), ``gsheets_`` (Sheets), ``gslides_``
(Slides), ``gforms_`` (Forms), ``gcal_`` (Calendar), ``gtasks_``
(Tasks), ``gcontacts_`` (Contacts), ``as_`` (appscriptly-native Apps
Script automation), and ``server_`` / ``admin_`` / ``account_`` for
introspection / admin / auth. NOTE: tools that historically wore a
``gdocs_`` prefix but act on Drive / admin / auth were renamed to the
honest prefix; the old ``gdocs_`` names still work as DEPRECATED ALIASES
(planned removal v3.0) — prefer the canonical name (e.g.
``gdrive_find_file`` not ``gdocs_find_file``, ``server_info`` not
``gdocs_server_info``, ``as_install_automation`` not
``gdocs_install_automation``).

START HERE: call ``server_guide()`` for the orientation as a structured
payload, or ``server_info()`` for build version + verified CI
test status.

THE 6 CORE WORKFLOWS
====================

1. NEW DOC from content composed in chat
   Goal: build a tabbed doc from text you have in the conversation.
   Tools: gdocs_make_tabbed_doc(title, tabs=[{title, content, ...}])
   Notes: ONE call. No file. No upload. DEFAULT for any request like
   "make me a doc with sections X, Y, Z".

2. CONVERT EXISTING DOC with Heading 1 paragraphs
   Goal: take a Google Doc / .docx on Drive that already has H1s and
   turn each H1 section into its own native tab.
   Tools: gdocs_preview_tab_split(drive_file_id=..., split_by="heading_1")
          -> gdocs_tab_existing_doc(drive_file_id=..., split_by="heading_1")
          -> gdocs_get_doc_outline(doc_id=...)   # verify the result
   Notes: Preview first — destructive conversion is one-way.

3. RETROFIT STYLED DOC with NO Heading 1s
   Goal: a styled doc where section breaks aren't H1s (banners in
   styled tables, shaded paragraphs, etc.).
   Tools: gdocs_tab_existing_doc(drive_file_id=...,
              markers=[{marker_text, tab_title}, ...])
   Notes: Same tool as #2; passing ``markers`` triggers RETROFIT mode
   (injects synthetic H1s before each marker block, then converts).
   NEVER rebuild a styled .docx from text — formatting would be lost.
   Use retrofit instead.

4. CONVERT SANDBOX .docx (bytes only, no Drive file)
   Goal: convert a .docx the model has built / has as raw bytes in
   its sandbox (cloud chat scenario).
   Tools: gdrive_get_signed_upload_url(...) -> POST {url} with the
          .docx bytes as multipart upload
   Notes: ``docx_path`` arguments DO NOT WORK from cloud chat — the
   server cannot see the caller's filesystem. Signed-URL upload is
   the only sandbox-bytes path. The POST is equivalent to
   gdocs_tab_existing_doc; use this when the .docx lives in your
   sandbox rather than on Drive.

5. CLEANUP — trash / restore Drive files
   Tools: gdrive_trash_file(file_id), gdrive_untrash_file(file_id)
   Notes: ONLY acts on files this app created. Files created
   elsewhere return app_not_authorized (no recovery — the file
   belongs to its owner). file_id accepts a string or list (batch).

6. INSTALL BOUND AUTOMATION inside a Doc / Sheet / Slides
   Goal: make a specific Doc / Sheet / Slides DO something on its own —
   a custom menu, a sidebar, a daily time-driven job, an onEdit reaction.
   The automation lives IN that file and runs without Claude after one
   deploy.
   Tools: as_generate_bound_script(container_id, script_body,
              manifest={menu?, triggers?, sidebar_html?, oauth_scopes?})
   Notes: This is the generic GENERATOR — Claude writes the .gs
   ``script_body`` that does the work; the tool creates a bound Apps
   Script project (auto-detecting docs/sheets/slides from the
   container), pushes the code + manifest, and deploys it in one call.
   Use for ANYTHING persistent (recurring jobs, re-clickable menus,
   reactions to the user's future edits). For a one-off edit, use the
   direct docs/sheets/slides tools instead. DISTINCT from
   as_install_automation (that installs the standalone runtime;
   this binds a per-file script). Example: "make this Doc refresh from
   the linked Sheet every morning."

NON-OBVIOUS OPERATING RULES
===========================
- Never rebuild a styled .docx from text. Retrofit (workflow #3)
  preserves formatting; rebuilding loses it.
- ``docx_path`` arguments do NOT work from cloud chat — the server
  cannot see the caller's filesystem. Use signed-URL upload
  (workflow #4) or drive_file_id.
- ``placeholder_behavior="rename"`` preserves a title / index page;
  the default "remove" deletes it. Use "rename" when the source has
  a meaningful cover page worth keeping.
- This app can only trash files IT created. Drive returns
  appNotAuthorizedToFile (403) on others; the file belongs to its
  owner and only they can trash it.
- First use requires interactive Google OAuth consent. The client
  must open the consent URL in a browser — it cannot be automated.
  Subsequent calls reuse the cached token until it expires.

EDIT TOOLS (after creating / converting)
========================================
gdocs_rename_tab, gdocs_delete_tab, gdocs_set_tab_icons,
gdocs_replace_all_text, gdocs_add_tabs, gdocs_append_to_tab

READ TOOLS
==========
gdocs_get_doc_outline — structure + icons, no body text (cheap)
gdocs_read_doc(doc_id, tab_id?) — body text, one tab or all
gdocs_get_tab_url(doc_id, tab_id) — direct deep-link to a tab

DRIVE MANAGEMENT
================
gdrive_find_doc_by_title, gdrive_find_file, gdrive_move_to_folder,
gdrive_create_folder, gdrive_trash_file, gdrive_untrash_file,
gdrive_share_file, gdrive_list_permissions, gdrive_revoke_permission,
gdrive_export_file
(the old gdocs_* names for these still work as deprecated aliases)

INTROSPECTION
=============
server_guide() — this orientation as a structured payload
server_info() — version + verified CI test status (digest,
  ci_run_url, mutation_check with stale_patches / imprecise_patches)
server_test_manifest() — full test inventory + per-test outcomes
server_help(error) — structured recovery guidance for an error string
"""

mcp = FastMCP(
    "appscriptly",
    instructions=_SERVER_INSTRUCTIONS,
    # Auto-discovery refactor: fail loud on a duplicate tool name.
    # FastMCP's DEFAULT is warn-and-overwrite — under auto-discovery a
    # double-registration (e.g. two feature files both decorating the
    # same tool name) would otherwise silently clobber. With
    # on_duplicate="error" the dup raises a ValueError at decoration
    # time, the discovery loop's try/except captures it as a failure,
    # and the boot RuntimeError fires — converting a silent surface
    # corruption into a loud boot crash (the prod-critical posture).
    on_duplicate="error",
)
# PR-Δ5.5 (2026-05-27): FastMCP server identity renamed from
# ``"google-docs"`` to ``"appscriptly"``. This is the string that
# appears in MCP client UIs (e.g. claude.ai's connector picker) as
# the server name. The underlying Python module path stays at
# ``appscriptly`` — see pyproject.toml's [project] comment block.
# auth=None at construction so stdio (Claude Desktop / Code) runs
# without auth middleware. HTTP transport sets mcp.auth = GoogleProvider
# at startup via configure_auth_for_http() — see main() and Phase 7.

# v2.1.5 M3 Phase C: ``_get_credentials`` and ``_format_http_error``
# moved to ``_tool_helpers.py`` per the 3-consumer extraction trigger
# (docs + drive + gas_deploy all need them). Re-exported here as
# module-level names so ``server.<helper>`` attribute access still
# works (matters for the few callers that imported them off ``server``
# directly, and for the decorator wiring below).
from ._tool_helpers import _format_http_error, _get_credentials  # noqa: F401


# M4 / v2.2.0: wire @workspace_tool (the canonical decorator post-M4)
# now that the mcp instance and both helpers (_get_credentials,
# _format_http_error) exist. After register(), the per-service
# tools.py files and the 7 stay-in-server tools below can use
# @workspace_tool(service=..., ...) in place of the @mcp.tool +
# ToolAnnotations + try/except boilerplate.
#
# Module alias is `_gdocs_decorators` (unchanged) for git-blame
# continuity with v2.0.6's R28 deferral close. The decorators module
# itself exposes both ``workspace_tool`` (canonical) and ``gdocs_tool``
# (deprecation shim — slated for removal in v2.2.x).
from . import decorators as _gdocs_decorators
_gdocs_decorators.register(mcp, _get_credentials, _format_http_error)
workspace_tool = _gdocs_decorators.workspace_tool
# Backward-compat re-export: a few in-tree call sites and external
# downstream forks still reference ``server.gdocs_tool``. The shim
# emits a DeprecationWarning at call time and delegates to
# ``workspace_tool(service="docs", ...)``. Planned removal in v2.2.x.
gdocs_tool = _gdocs_decorators.gdocs_tool


# v1.3.1: title validation helper. Drive rejects titles with control
# chars (U+0000-001F, U+007F) by surfacing a confusing 400; we fail
# fast with a clear message. >1024 chars is a defensive cap below
# Drive's actual limit so we never surface raw API errors for length.
_TITLE_MAX_CHARS = 1024


def _validate_title(title, *, field: str = "title") -> None:
    """Reject titles that would crash downstream Drive/Docs APIs.

    - Must be a non-empty string
    - ≤ 1024 chars
    - No control chars (U+0000-001F, U+007F)
    """
    if not isinstance(title, str):
        raise ToolError(
            f"{field} must be a string (got {type(title).__name__})"
        )
    if not title:
        raise ToolError(f"{field} cannot be empty")
    if len(title) > _TITLE_MAX_CHARS:
        raise ToolError(
            f"{field} is {len(title)} chars; max is {_TITLE_MAX_CHARS}. "
            f"Truncate before retrying."
        )
    for ch in title:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ToolError(
                f"{field} contains a control character (U+{code:04X}) — "
                f"strip control chars before retrying. Drive rejects "
                f"titles with these and surfaces a confusing API error."
            )


# ---------------------------------------------------------------------
# Auto-discovery: register every service tool by walking services/.
# ---------------------------------------------------------------------
# Replaces the prior 12 hand-maintained ``from .services.X import
# tools as _X`` side-effect imports (the merge-conflict surface every
# feature PR used to touch). Runs AFTER ``mcp`` is built and AFTER
# ``decorators.register(mcp, ...)`` wires @workspace_tool — so every
# discovered module's module-level @workspace_tool decorations land
# on the fully-initialised mcp instance.
#
# A new tool now drops into ``services/X/`` and is picked up
# automatically; a feature PR touches only its own folder (+ that
# service's ``_expected_tools.py`` declaration). No central edit here.
#
# Migration history (pre-auto-discovery, when this was explicit imports):
#   Phase A (v2.1.3, PR #94):  docs/ (12 tools)
#   Phase B (v2.1.4, PR #97):  drive/ (4 tools + sharing)
#   Phase C (v2.1.5, PR #103): gas_deploy/
#   Gap #7  (v2.2.2):          admin/ (7 tools) — server.py held NO
#                              tool definitions from here on.
#   v2.3.1 / v2.3.2:           sheets/ + slides/
#   PR-Δ7..Δ12:                apps_script/ (6 tools across feature files)
#
# Inclusion rule (Option B): import every leaf module under services/
# whose name does NOT start with ``_`` (skips ``__init__``,
# ``_expected_tools``, future private helpers) and is NOT in the
# ``{api, scopes}`` denylist. This is a deliberate harmless SUPERSET
# of the tool modules — it also imports decoration-free helper modules
# (``docs/markdown_render``, ``docs/tab_tree``, ``drive/sharing``).
# Those register zero tools (no @workspace_tool) AND are already on the
# boot import graph today (their service's tools.py imports them
# transitively), so importing them directly is a net-zero change to
# what loads at boot. Exactness of the TOOL SURFACE (not the imported-
# module set) is enforced by the golden snapshot
# (tests/golden/tool_surface.json) + the per-service ``_expected_tools.py``
# witnesses + the independent ``test_tool_schemas.py`` witness.
#
# INVARIANT (load-bearing — enforced by
# tests/unit/services/test_discovery_safety.py ::
# test_every_service_module_imports_without_network_or_creds):
#   Every module under services/ reachable by discovery is imported at
#   boot. It MUST be import-safe — NO network I/O, NO credential load at
#   module-import time. All I/O is deferred to tool invocation. A future
#   Gmail/Calendar tool that loads creds at import would break this and
#   the import-safety test will catch it.
#
# Fail-loud (prod-critical): a broken import or a discovery miss must
# crash at module-load BEFORE mcp.run(), never serve a partial tool
# surface. Three guards: (1) any import exception → RuntimeError here;
# (2) the FastMCP instance is constructed with on_duplicate="error", so
# a double-registration raises (caught here → boot fails); (3) the
# boot-time count FLOOR below.
import importlib as _importlib  # noqa: E402
import pkgutil as _pkgutil  # noqa: E402

from . import services as _services_pkg  # noqa: E402

# Skip leaves matching these (in addition to the underscore-prefix
# rule). ``api`` + ``scopes`` are per-service support modules that
# carry no @workspace_tool decorations.
_DISCOVERY_DENYLIST = frozenset({"api", "scopes"})

# Boot-time floor (Δ2): the known-good registered-tool count. A FLOOR
# (>=), not exact-match, so adding a tool doesn't force a central edit
# here (which would reintroduce the per-PR conflict this refactor
# kills). The floor catches the DANGEROUS direction — tools silently
# DROPPED below known-good (e.g. a discovery miss, a folder that
# stopped importing). The CI golden test enforces exact-match; this
# floor is the runtime backstop. Bump deliberately + rarely.
# v2.4.0: 66 → 73 with the 7-tool Calendar service (services/calendar/);
# then 73 → 79 with the 6-tool Contacts service (services/contacts/);
# then 79 → 86 with the 7-tool Tasks service (services/tasks/);
# then 86 → 93 with the 7-tool Forms service (services/forms/);
# then 93 → 97 with the 4 apps_script service-parity tools
# (as_install_sheet_menu, as_install_slides_menu, as_refresh_linked_slides,
# as_grade_form_responses — per-service GAS analogues of the Docs menu);
# then 97 → 100 with the 3 apps_script GAS-automation tools giving
# Calendar / Tasks / Contacts their second (automation) lever
# (as_install_calendar_sync, as_install_task_rollover,
# as_install_contact_sync — sensitive scope in the GENERATED manifest only);
# then 134 → 136 with the 2026-07 next-wave tools (gdrive_rename_file +
# server_health — both canonical-only, no deprecated aliases).
_MIN_EXPECTED_TOOL_COUNT = 136

_discovery_failures: list[tuple[str, str]] = []


def _on_walk_error(_name: str) -> None:
    # walk_packages swallows errors raised while IMPORTING a sub-package
    # during the walk itself (distinct from our explicit
    # import_module below) unless an onerror handler is given. Capture
    # them into the same failure list so a broken sub-package __init__
    # also fails loud rather than silently truncating the walk.
    import sys as _sys

    _exc = _sys.exc_info()[1]
    _discovery_failures.append((_name, repr(_exc)))


for _modinfo in _pkgutil.walk_packages(
    _services_pkg.__path__,
    prefix=_services_pkg.__name__ + ".",
    onerror=_on_walk_error,
):
    _leaf = _modinfo.name.rsplit(".", 1)[-1]
    if _leaf.startswith("_") or _leaf in _DISCOVERY_DENYLIST:
        continue
    if _modinfo.ispkg:
        # Sub-packages (the service folders themselves) are walked
        # into, not imported as a tool module.
        continue
    try:
        _importlib.import_module(_modinfo.name)
    except Exception as _e:  # noqa: BLE001 — capture ALL, re-raise aggregated
        _discovery_failures.append((_modinfo.name, repr(_e)))

if _discovery_failures:
    raise RuntimeError(
        "Tool discovery FAILED for "
        f"{_discovery_failures}; refusing to boot a partial tool "
        "surface. Fix the failing module(s) before deploy. (This is "
        "the prod-critical fail-loud guard — a broken service module "
        "must crash at boot, not silently drop tools.)"
    )

# Boot-time count floor (Δ2). Runs synchronously at module load so a
# discovery miss fails BEFORE mcp.run(). ``list_tools()`` is the public
# async API; we drive it via ``asyncio.run`` at import time. This is
# safe because server.py is imported during boot BEFORE main() starts
# the event loop (mcp.run() / run_http()) — there is no running loop at
# module-load time. Using the public API (not a private registry attr)
# keeps the floor robust across FastMCP version churn AND counts ONLY
# tools (FastMCP's internal component registry mixes tools + resources
# + prompts; server.py registers resources via resources.py, so a raw
# component count would over-count).
import asyncio as _asyncio  # noqa: E402


def _count_registered_tools() -> int:
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (the expected boot-time case) — safe to run.
        return len(_asyncio.run(mcp.list_tools()))
    # A loop is already running (unexpected at import; defensive). Fall
    # back to a private-attr tools-only count rather than risk a nested-
    # loop crash. Key shape is "tool:<name>@..."; filter to tool keys.
    components = mcp._local_provider._components  # type: ignore[attr-defined]
    return sum(1 for k in components if k.startswith("tool:"))


_registered_count = _count_registered_tools()
if _registered_count < _MIN_EXPECTED_TOOL_COUNT:
    raise RuntimeError(
        f"Tool discovery registered only {_registered_count} tools; "
        f"expected at least {_MIN_EXPECTED_TOOL_COUNT}. A service module "
        "was likely missed by discovery or stopped registering its "
        "tools. Refusing to boot a partial tool surface. (If you "
        "intentionally REMOVED tools, lower _MIN_EXPECTED_TOOL_COUNT "
        "and regenerate tests/golden/tool_surface.json.)"
    )


_CLI_SUBCOMMANDS = {
    "setup-apps-script",
    "setup-apps-script-auto",  # README lines 156 + 191 document this as the recommended setup path
    "configure-webapp",
    "status",
    "help",
    "-h",
    "--help",
}


def main() -> None:
    """Entry point.

    Dispatches in order:
      1. ``google-docs-mcp <cli-subcommand>`` -> route to ``cli.py``
      2. ``MCP_TRANSPORT=http`` env var (or ``--http`` flag) -> run as
         remote HTTP server (Fly.io / cloud chat use case). Listens on
         ``$PORT`` (default 8080). Includes both the FastMCP ``/mcp``
         endpoint AND a simple ``/api/convert`` REST endpoint for
         clients that don't speak MCP protocol (e.g. cloud chat's
         Python sandbox).
      3. Otherwise -> stdio (Claude Code / Claude Desktop).
    """
    if len(sys.argv) > 1 and sys.argv[1] in _CLI_SUBCOMMANDS:
        from .cli import cli_main
        sys.exit(cli_main(sys.argv[1:]))

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if "--http" in sys.argv:
        transport = "http"

    if transport == "http":
        from .http_server import run_http
        from .oauth_google import configure_auth_for_http

        # PR-Δ4: Sentry init runs BEFORE configure_auth_for_http /
        # run_http so any exception thrown during HTTP-mode setup
        # itself is captured. No-op when SENTRY_DSN is unset (stub-
        # but-wired — operator flips by setting the Fly secret).
        from .observability import init_sentry
        init_sentry()

        # PR-Δ-volfix: fail loud at boot if the per-user state DB isn't
        # writable by the runtime user (the SQLITE_READONLY incident).
        # Runs AFTER init_sentry so the crash is captured, and BEFORE we
        # accept traffic so a root-owned-volume mismatch surfaces in the
        # deploy logs instead of silently 500-ing every tool call for
        # hours. entrypoint.sh fixes the ownership; this verifies it
        # actually took (defense in depth — and the only guard on the
        # in-process db_path() if the entrypoint is ever bypassed).
        from . import user_store
        user_store.assert_state_db_writable()

        # v1.1+: wire GoogleProvider so HTTP requests are per-user
        # authenticated. Stdio path below intentionally skips this —
        # local trust model, single user, no auth middleware.
        configure_auth_for_http(mcp)

        port = int(os.environ.get("PORT", "8080"))
        run_http(mcp, port=port)
    else:
        mcp.run()


# ---------------------------------------------------------------------
# v2.2b: LLM_RECOVERY artifacts — additive block, kept at file end to
# minimize merge conflicts with other parallel v2.2 PRs. The import
# below triggers registration of the gdocs://error-recovery resources
# (resources.py decorates module-level functions with @mcp.resource).
# Gap #7 (v2.2.2): the ``_RECOVERY_TABLE`` re-export here was used by
# ``gdocs_help`` which moved to ``services/admin/tools.py``. That
# module now lazy-imports the table directly from ``resources``; the
# re-export was dropped to keep server.py free of admin-tool deps.
# ---------------------------------------------------------------------
from . import resources as _llm_recovery_resources  # noqa: E402,F401


if __name__ == "__main__":
    main()

