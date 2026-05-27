"""Workspace Automation runtime install — MCP tool registrations.

This module defines the ``@workspace_tool``-decorated tool function(s)
that install the Apps-Script-backed Workspace Automation runtime into
the calling user's Google account. Importing this module triggers
registration with the live ``mcp`` instance — ``server.py`` performs
the import at the bottom of its module, AFTER constructing ``mcp``
and AFTER ``decorators.register(mcp, ...)`` wires the decorator.

**Tools registered here** (2 surfaces, 1 underlying implementation):

1. ``gdocs_install_automation`` — CANONICAL (PR-α / v2.3.4+). User-
   facing automation-install tool: provisions the per-user Workspace
   Automation runtime so Claude can build persistent workflows
   (time-driven jobs, custom menus inside docs/sheets/slides,
   reactive automations).

2. ``gdocs_setup_apps_script`` — DEPRECATED ALIAS. Pre-v2.3.4 name.
   Kept registered so existing user prompts / saved automations /
   external integrations don't break. Emits a runtime
   ``DeprecationWarning`` on call and instructs the caller to use
   ``gdocs_install_automation`` instead. Planned removal in v3.0.

Why the rename: the original ``setup_apps_script`` name framed this
as infrastructure plumbing (a "second consent" for an "Apps Script
management" scope users had to trust). PR-α reframes it as the
headline automation feature — installing the runtime is the
load-bearing capability, not a workaround. The user-facing consent
copy now says "Install your Workspace automation runtime" rather
than "Set up your Apps Script Web App," and the success message
explains what was unlocked rather than what was deployed.

**CRITICAL: ``creds=False`` preserved on BOTH registrations.** Both
tools opt out of the standard creds-injection envelope because the
underlying body has its own ``NeedsReauthError`` → structured-
response path: on cloud-mode auth failure it returns
``{status: "needs_authorization", auth_url, message}`` rather than
raising ``ToolError``. The standard decorator path (``creds=True``)
would short-circuit at the credential-fetch step and lose that
structured shape. Re-applying the standard envelope here would
silently break the OAuth-first-run UX in cloud chat.

**Import discipline.** Imports the 2 shared helpers
(``_get_credentials``, ``_format_http_error``) directly from
``_tool_helpers`` — no deferred-binding shim, no server.py reach-back.
The decorator itself (``workspace_tool``) still lives in ``server.py``
because it's bound to the live ``mcp`` instance; that import path
is unchanged.
"""
from __future__ import annotations

import warnings

from fastmcp.exceptions import ToolError

from google_docs_mcp.credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
from google_docs_mcp.oauth_google import resolve_runtime_oauth_config
from google_docs_mcp.server import workspace_tool
from google_docs_mcp.services.gas_deploy import GAS_DEPLOY_SCOPES
from google_docs_mcp.setup_apps_script import (
    setup_apps_script_auto,
    setup_apps_script_for_user,
)
from google_docs_mcp.tool_schemas import GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA


# ---------------------------------------------------------------------
# Core implementation — shared by the canonical name AND the alias
# ---------------------------------------------------------------------


def _install_automation_runtime() -> dict:
    """Underlying installer; both registered tools delegate here.

    Extracted out of the decorated function bodies so the alias
    (``gdocs_setup_apps_script``) can call exactly the same code
    path without duplicating it. Both decorated wrappers do nothing
    but: (a) optionally emit a deprecation warning, (b) call this.

    The reframe (PR-α) is in the user-facing copy this function
    returns — the underlying OAuth dance, Apps Script provisioning,
    and Web App deploy are unchanged from the pre-PR ``setup_apps_script``
    implementation.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        # Stdio / no-auth-context mode: local CLI behavior.
        # Uses the operator's cached OAuth token at ~/.google-docs-mcp/.
        try:
            deployment = setup_apps_script_auto()
        except Exception as e:  # noqa: BLE001
            raise ToolError(
                f"Workspace automation runtime install failed: {e}"
            ) from e
        return {
            "status": "ready",
            "url": deployment.url,
            "script_id": deployment.script_id,
            "deployment_id": deployment.deployment_id,
            "message": (
                "Workflow runtime installed. Claude can now build "
                "custom automations in your Workspace."
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
                f"Install your custom Workspace automation runtime — "
                f"Google will ask you to authorize the workflow "
                f"installer.\n\n"
                f"**[Click here to authorize]({e.auth_url})**\n\n"
                f"After granting access, re-run this tool."
            ),
        }

    try:
        deployment = setup_apps_script_for_user(creds, user_id)
    except Exception as e:  # noqa: BLE001
        raise ToolError(
            f"Workspace automation runtime install failed: {e}"
        ) from e

    return {
        "status": "ready",
        "url": deployment.url,
        "script_id": deployment.script_id,
        "deployment_id": deployment.deployment_id,
        "message": (
            "Workflow runtime installed under your Google account. "
            "Claude can now build custom automations in your "
            "Workspace — time-driven jobs, custom menus inside your "
            "docs / sheets / slides, and reactive workflows that run "
            "when your data changes."
        ),
    }


# ---------------------------------------------------------------------
# 1. gdocs_install_automation — CANONICAL (PR-α / v2.3.4+)
# ---------------------------------------------------------------------


@workspace_tool(
    title="Install Workspace automation runtime",
    service="gas_deploy",
    readonly=False, destructive=False, idempotent=True, external=True,
    # creds=False: this tool has its own NeedsReauthError → structured
    # response handling (returns status="needs_authorization" with
    # auth_url instead of raising ToolError). The standard decorator
    # path would lose that structured shape. See module docstring.
    creds=False,
    output_schema=GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
)
def gdocs_install_automation() -> dict:
    """Install the Workspace Automation runtime into your Google account.

    One-time setup that enables Claude to build persistent workflows
    for you: time-driven jobs that run on a schedule, custom menus
    inside your Google Docs / Sheets / Slides, and reactive
    automations that fire when your data changes. After install, the
    automations Claude creates live IN your Workspace and run on
    Google's infrastructure — Claude doesn't need to be in the loop
    for them to fire.

    USE WHEN: the user asks for any persistent / scheduled / event-
    driven automation in their Workspace, OR when any other tool
    that needs the runtime (currently ``gdocs_tab_existing_doc``'s
    lossless retrofit path) reports it isn't installed yet.

    Other tools — ``gdocs_make_tabbed_doc``, edit tools, read tools,
    Sheets/Slides tools — don't need this runtime to be installed.
    They DO require the one-time Google OAuth grant (Drive + Docs +
    related scopes), but that consent happens automatically on first
    tool call. THIS tool is only needed for the persistent-workflow
    layer (and, transitively, for ``gdocs_tab_existing_doc``'s
    lossless content-move path which uses the runtime internally).

    Consent shape: first call typically returns
    ``status: "needs_authorization"`` with a Google consent URL the
    user must open in a browser — Google OAuth cannot be automated.
    The consent screen will mention "Apps Script" because Apps
    Script IS the runtime Google provides; you're authorizing the
    installer to drop a small Apps Script project into your account
    that Claude can then write workflows into. After consent, re-
    run this tool to complete the install.

    Idempotent: safe to retry if interrupted. Resumes from the last
    successful step. Per-user setup state is tracked in the
    user_store row (cloud) or ``~/.google-docs-mcp/setup-state.json``
    (stdio).

    Returns ``{status, url, script_id, deployment_id, message}`` on
    success. On cloud-mode auth failure, returns
    ``{status: "needs_authorization", auth_url, message}`` — emit
    the message verbatim so the consent URL renders as a clickable
    link.

    Choreography: required ONCE before any persistent-workflow tool
    AND before ``gdocs_tab_existing_doc(markers=[...])``'s retrofit
    mode. After successful install, all workflow + retrofit tools
    run freely without further setup.
    """
    return _install_automation_runtime()


# ---------------------------------------------------------------------
# 2. gdocs_setup_apps_script — DEPRECATED ALIAS (pre-PR-α name)
# ---------------------------------------------------------------------


_SETUP_APPS_SCRIPT_DEPRECATION_MSG = (
    "gdocs_setup_apps_script is deprecated since PR-α; use "
    "gdocs_install_automation instead. The reframe surfaces this "
    "as the headline automation-install feature rather than as "
    "Apps-Script infrastructure plumbing. The underlying behavior "
    "is identical — the rename is a copy change only. The old "
    "name will be removed in v3.0."
)


@workspace_tool(
    title="DEPRECATED — use gdocs_install_automation instead",
    service="gas_deploy",
    readonly=False, destructive=False, idempotent=True, external=True,
    # creds=False: same rationale as the canonical tool above. The
    # alias MUST share this opt-out so the structured needs_authorization
    # response shape is preserved on the deprecated surface too.
    creds=False,
    output_schema=GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
)
def gdocs_setup_apps_script() -> dict:
    """DEPRECATED — use ``gdocs_install_automation`` instead.

    Pre-PR-α name for the Workspace Automation runtime installer.
    Preserved as a deprecation alias so existing user prompts,
    saved automations, and external integrations that reference
    the old name keep working through v2.x.

    Behavior is identical to ``gdocs_install_automation``: same
    underlying OAuth dance, same Apps Script provisioning, same
    Web App deploy, same structured response shape.

    The reframe (PR-α): the original name framed this as an
    "Apps Script setup" obligation; the new name frames it as
    installing the automation capability. Same code path; different
    headline.

    Planned removal in v3.0. Migrate by replacing every call to
    ``gdocs_setup_apps_script()`` with ``gdocs_install_automation()``.
    """
    warnings.warn(
        _SETUP_APPS_SCRIPT_DEPRECATION_MSG,
        DeprecationWarning,
        stacklevel=2,
    )
    return _install_automation_runtime()
