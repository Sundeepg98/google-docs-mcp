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
google-docs-fly — create, edit, read, and manage Google Docs with
native sidebar Tabs (October 2024+ feature). All tools prefixed gdocs_.

START HERE: call ``gdocs_guide()`` for the orientation as a structured
payload, or ``gdocs_server_info()`` for build version + verified CI
test status.

THE 5 CORE WORKFLOWS
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
   Tools: gdocs_get_signed_upload_url(...) -> POST {url} with the
          .docx bytes as multipart upload
   Notes: ``docx_path`` arguments DO NOT WORK from cloud chat — the
   server cannot see the caller's filesystem. Signed-URL upload is
   the only sandbox-bytes path. The POST is equivalent to
   gdocs_tab_existing_doc; use this when the .docx lives in your
   sandbox rather than on Drive.

5. CLEANUP — trash / restore Drive files
   Tools: gdocs_trash_file(file_id), gdocs_untrash_file(file_id)
   Notes: ONLY acts on files this app created. Files created
   elsewhere return app_not_authorized (no recovery — the file
   belongs to its owner). file_id accepts a string or list (batch).

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
gdocs_find_doc_by_title, gdocs_move_to_folder,
gdocs_trash_file, gdocs_untrash_file

INTROSPECTION
=============
gdocs_guide() — this orientation as a structured payload
gdocs_server_info() — version + verified CI test status (digest,
  ci_run_url, mutation_check with stale_patches / imprecise_patches)
gdocs_test_manifest() — full test inventory + per-test outcomes
"""

mcp = FastMCP("google-docs", instructions=_SERVER_INSTRUCTIONS)
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


# M3 POC (v2.1.3): the 12 docs-service tools moved to
# ``services/docs/tools.py``. Importing that module at the bottom of
# this file triggers their @gdocs_tool registration. Tools relocated:
#   gdocs_make_tabbed_doc, gdocs_add_tabs, gdocs_get_doc_outline,
#   gdocs_read_doc, gdocs_append_to_tab, gdocs_tab_existing_doc,
#   gdocs_rename_tab, gdocs_get_tab_url, gdocs_delete_tab,
#   gdocs_replace_all_text, gdocs_set_tab_icons, gdocs_preview_tab_split
#
# The remaining 12 tools (drive, gas_deploy, admin, introspection,
# auth) stay in this file until the next M3 phase. See
# docs/ARCHITECTURE.md §5.1 for the migration plan.
# ---------------------------------------------------------------------
# M3: trigger per-service tool registration.
# ---------------------------------------------------------------------
# Each per-service ``tools.py`` is imported AT THE BOTTOM of server.py
# — AFTER ``mcp`` is built, AFTER ``decorators.register(mcp, ...)``
# wires the @workspace_tool decorator, AND AFTER the remaining
# module-level state in this file has run — so service-tool
# registrations land on the fully-initialised mcp instance. The
# asymmetric import order (services/<svc>/tools.py can
# ``from google_docs_mcp import server`` at module load because by
# then server.py is fully loaded) avoids a circular import.
#
# Side-effect imports: registration happens as a side-effect of
# evaluating each tools.py's module-level @workspace_tool decorations.
#
# Migration history:
#   Phase A (v2.1.3, PR #94):  docs/ (12 tools)
#   Phase B (v2.1.4, PR #97):  drive/ (4 tools + _run_batch helper)
#   Phase C (v2.1.5, PR #103): gas_deploy/ (1 tool)
#   Gap #7  (v2.2.2, this PR): admin/ (7 tools + admin-domain helpers).
#                              After this PR, server.py contains NO
#                              tool definitions — the Hex / SOLID
#                              specialists' ISP-asymmetry finding is
#                              closed.
from .services.docs import tools as _docs_tools  # noqa: F401, E402 — side-effect import
from .services.drive import tools as _drive_tools  # noqa: F401, E402 — side-effect import
from .services.gas_deploy import tools as _gas_deploy_tools  # noqa: F401, E402 — side-effect import
from .services.admin import tools as _admin_tools  # noqa: F401, E402 — side-effect import
# v2.3.1: 2nd new service after Drive sharing — Sheets minimal start
# (range read/write + create). Same registration pattern, no new
# infrastructure required. Empirically validates that Sheets fits
# the per-service-folder template that Drive sharing (PR #117) proved.
from .services.sheets import tools as _sheets_tools  # noqa: F401, E402 — side-effect import


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

