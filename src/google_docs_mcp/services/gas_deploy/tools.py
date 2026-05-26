"""Apps Script (gas_deploy) MCP tool registrations (M3 Phase C — v2.1.5).

This module defines the ``@gdocs_tool``-decorated tool function(s) for
the Apps Script setup service. Importing this module triggers
registration with the live ``mcp`` instance — ``server.py`` performs
the import at the bottom of its module, AFTER constructing ``mcp``
and AFTER ``decorators.register(mcp, ...)`` wires the ``@gdocs_tool``
decorator.

**Tools registered here** (1 gas_deploy-service tool):

1. ``gdocs_setup_apps_script`` — provision the per-user Apps Script
   Web App needed for ``gdocs_tab_existing_doc``'s lossless retrofit.

**CRITICAL: ``creds=False`` preserved.** Unlike most ``@gdocs_tool``
sites, this tool has its own ``NeedsReauthError`` → structured
response path: on cloud-mode auth failure it returns
``{status: "needs_authorization", auth_url, message}`` rather than
raising ``ToolError``. The standard decorator path (``creds=True``)
would short-circuit at the credential-fetch step and lose that
structured shape. Re-applying the standard envelope here would
silently break the OAuth-first-run UX in cloud chat.

**Import discipline.** Imports the 2 shared helpers
(``_get_credentials``, ``_format_http_error``) directly from
``_tool_helpers`` — no deferred-binding shim, no server.py reach-back.
This is the M3 Phase C 3-consumer extraction trigger landing: docs +
drive + gas_deploy all want the same 2 helpers, so they live in
``_tool_helpers.py`` rather than in ``server.py``. The decorator
itself (``gdocs_tool``) still lives in ``server.py`` because it's
bound to the live ``mcp`` instance; that import path is unchanged.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError

from google_docs_mcp.credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
from google_docs_mcp.oauth_google import resolve_runtime_oauth_config
from google_docs_mcp.server import gdocs_tool
from google_docs_mcp.services.gas_deploy import GAS_DEPLOY_SCOPES
from google_docs_mcp.setup_apps_script import (
    setup_apps_script_auto,
    setup_apps_script_for_user,
)
from google_docs_mcp.tool_schemas import GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA


@gdocs_tool(
    title="Provision per-user Apps Script project",
    readonly=False, destructive=False, idempotent=True, external=True,
    # creds=False: this tool has its own NeedsReauthError → structured
    # response handling (returns status="needs_authorization" with
    # auth_url instead of raising ToolError). The standard decorator
    # path would lose that structured shape.
    creds=False,
    output_schema=GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
)
def gdocs_setup_apps_script() -> dict:
    """One-shot setup of the Apps Script Web App needed for lossless retrofit.

    Run this once per user (cloud) or once per machine (local stdio)
    to enable ``gdocs_tab_existing_doc`` — the path that uses Apps
    Script for lossless content moves (preserving drawings, equations,
    tables, cell shading that no REST request type can re-emit).

    Without this setup, ``gdocs_tab_existing_doc`` fails with "Apps
    Script Web App URL not configured." Other tools
    (``gdocs_make_tabbed_doc``, edit tools, read tools) do not need
    this Apps-Script-specific setup — but, like all tools in this
    server, they DO require the one-time Google OAuth authorization
    grant (Drive + Docs scopes). The OAuth grant happens automatically
    on first tool call: any tool that needs creds returns
    ``status: "needs_authorization"`` with a click-to-authorize URL;
    after consent, all subsequent tools in the session work without
    further prompts. Only ``gdocs_tab_existing_doc``'s lossless
    retrofit path additionally needs THIS tool
    (``gdocs_setup_apps_script``) to have been run once.

    Idempotent: safe to retry if interrupted; resumes from the last
    successful step. The user_store row (cloud) or
    ``~/.google-docs-mcp/setup-state.json`` (local) keeps the ledger.

    Returns ``{status, url, script_id, deployment_id, message}`` on
    success. On cloud-mode auth failure, returns
    ``{status: "needs_authorization", auth_url, message}`` — emit
    the message verbatim so Claude renders the URL as a clickable link.

    Choreography: required ONCE before
    ``gdocs_tab_existing_doc(markers=[...])`` (retrofit path) and the
    Apps-Script-backed retrofit pipeline in general. After successful
    setup, run any retrofit conversion freely.

    NOTE: First call typically returns ``needs_authorization`` with a
    URL the user MUST open in a browser — Google OAuth consent
    cannot be automated. After consent, re-run this tool to complete
    the Web App deploy.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        # Stdio / no-auth-context mode: local CLI behavior.
        # Uses the operator's cached OAuth token at ~/.google-docs-mcp/.
        try:
            deployment = setup_apps_script_auto()
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Apps Script setup failed: {e}") from e
        return {
            "status": "ready",
            "url": deployment.url,
            "script_id": deployment.script_id,
            "deployment_id": deployment.deployment_id,
            "message": (
                "Apps Script Web App is deployed. You can now use "
                "gdocs_tab_existing_doc."
            ),
        }

    # HTTP / multi-tenant mode: per-user creds, per-user user_store ledger.
    try:
        oauth_cfg = resolve_runtime_oauth_config()
    except RuntimeError as e:
        raise ToolError(f"Server OAuth config error: {e}") from e

    try:
        creds = get_credentials_for_user(
            user_id,
            required_scopes=GAS_DEPLOY_SCOPES,
            **oauth_cfg,
        )
    except NeedsReauthError as e:
        return {
            "status": "needs_authorization",
            "auth_url": e.auth_url,
            "message": (
                f"Google API access required to set up your Apps Script "
                f"Web App.\n\n**[Click here to authorize]({e.auth_url})**"
                f"\n\nAfter granting access, re-run this tool."
            ),
        }

    try:
        deployment = setup_apps_script_for_user(creds, user_id)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Apps Script setup failed: {e}") from e

    return {
        "status": "ready",
        "url": deployment.url,
        "script_id": deployment.script_id,
        "deployment_id": deployment.deployment_id,
        "message": (
            "Apps Script Web App is deployed under your Google account. "
            "You can now use gdocs_tab_existing_doc and other tools "
            "that need lossless content moves."
        ),
    }
