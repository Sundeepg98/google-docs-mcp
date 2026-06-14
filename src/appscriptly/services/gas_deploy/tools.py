"""Workspace Automation runtime install — MCP tool registrations.

This module defines the ``@workspace_tool``-decorated tool function(s)
that install the Apps-Script-backed Workspace Automation runtime into
the calling user's Google account. Importing this module triggers
registration with the live ``mcp`` instance — ``server.py`` performs
the import at the bottom of its module, AFTER constructing ``mcp``
and AFTER ``decorators.register(mcp, ...)`` wires the decorator.

**Tools registered here** (3 tools). The first two share one
underlying implementation (install/alias); the third is a distinct
surface with its own implementation:

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

3. ``as_deploy_web_app`` — deploy a caller-supplied doGet/doPost
   project as an Apps Script Web App / webhook (ROADMAP 59). A
   separate surface with its own ``_deploy_web_app_project``
   implementation, layered on the existing ``AppsScriptClient``
   machinery (NOT the install path's implementation).

(Authoritative declaration: ``services/gas_deploy/_expected_tools.py``.)

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

from appscriptly.apps_script_hmac import generate_hmac_key
from appscriptly.credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
from appscriptly.oauth_google import resolve_runtime_oauth_config
from appscriptly.server import workspace_tool
from appscriptly.services.gas_deploy import GAS_DEPLOY_SCOPES
from appscriptly.services.gas_deploy.api import (
    deploy_web_app_project as _deploy_web_app_project,
    inject_webapp_hmac_guard as _inject_webapp_hmac_guard,
)
from appscriptly.setup_apps_script import (
    setup_apps_script_auto,
    setup_apps_script_for_user,
)
from appscriptly.tool_schemas import (
    AS_DEPLOY_WEB_APP_OUTPUT_SCHEMA,
    GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
)

# Apps Script scopes this service's deploy tool exercises (both already
# in baseline auth.SCOPES — declared on the tool for honest per-tool
# scope surfacing, a no-op for the consent flow). Mirrors
# services/apps_script/scopes.py::GAS_BOUND_SCOPES.
_WEB_APP_DEPLOY_SCOPES = [
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
]


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


# ---------------------------------------------------------------------
# 3. as_deploy_web_app — deploy a doGet/doPost project as a Web App
#    (ROADMAP 59). Extends gas_deploy with the user-facing endpoint/
#    webhook deploy on top of the existing AppsScriptClient machinery.
# ---------------------------------------------------------------------


@workspace_tool(
    title="Deploy an Apps Script web app / webhook (doGet/doPost)",
    service="gas_deploy",
    readonly=False,
    destructive=False,
    # Each call creates a NEW standalone project + deployment — re-running
    # produces ANOTHER project with its own /exec URL. NOT idempotent
    # (same convention as as_generate_bound_script / create tools).
    idempotent=False,
    external=True,
    # creds=True: unlike the install-automation tools above, this tool has
    # NO NeedsReauthError structured-response path — it deploys caller-
    # supplied code directly, so the standard creds-injection envelope is
    # correct (HttpError → ToolError).
    creds=True,
    scopes=_WEB_APP_DEPLOY_SCOPES,
    output_schema=AS_DEPLOY_WEB_APP_OUTPUT_SCHEMA,
)
def as_deploy_web_app(
    creds,
    script_body: str,
    title: str,
    execute_as: str = "USER_DEPLOYING",
    access: str = "ANYONE_ANONYMOUS",
) -> dict:
    """Deploy an Apps Script Web App — expose automation as an HTTP endpoint.

    USE WHEN: the user wants their OWN automation reachable as an inbound
    HTTP endpoint / webhook — a URL that Slack, Stripe, GitHub, an
    external cron, or a form can POST to (or GET), which then runs their
    Apps Script logic on Google's infrastructure. This is the "give me a
    webhook URL" tool. (For automation that lives INSIDE a specific Doc /
    Sheet / Slides — menus, triggers, custom functions — use
    ``as_generate_bound_script`` instead; that binds to a container, this
    stands alone and is HTTP-reachable.)

    Creates a NEW standalone Apps Script project from the ``.gs`` body you
    supply, pushes it with a Web App manifest entry point, cuts a version,
    and deploys it — returning the live ``/exec`` URL. Uses the existing
    deploy machinery (``create_project`` → ``push_files`` →
    ``create_version`` → ``deploy_webapp``); no new scope (the baseline
    ``script.projects`` + ``script.deployments`` cover it).

    Args:
        script_body: the Apps Script ``.gs`` source. MUST define
            ``doGet(e)`` and/or ``doPost(e)`` — these are the Web App
            entry points Apps Script invokes on an incoming GET / POST.
            Each receives the request event ``e`` (``e.parameter`` for
            query/form params, ``e.postData.contents`` for a raw POST
            body) and should ``return ContentService.createTextOutput(...)``
            (optionally ``.setMimeType(ContentService.MimeType.JSON)``).
            Claude authors this. A body with neither handler is rejected.
        title: title for the new Apps Script project (also its Drive
            filename) — e.g. ``"Stripe webhook receiver"``.
        execute_as: whose authority the endpoint runs with —
            ``"USER_DEPLOYING"`` (default; runs as you, so it can touch
            your Workspace data) or ``"USER_ACCESSING"`` (runs as each
            invoking Google user; requires callers to be signed in).
        access: who may invoke the ``/exec`` URL —
            ``"ANYONE_ANONYMOUS"`` (default; the webhook case — no Google
            sign-in, so an external service can POST), ``"ANYONE"`` (any
            Google user), ``"DOMAIN"`` (your Workspace domain only), or
            ``"MYSELF"`` (only you). For a public webhook keep the
            default; tighten it if the endpoint shouldn't be world-callable.

    Returns:
        ``{script_id, deployment_id, version, exec_url, execute_as,
        access, project_url}`` — plus ``hmac_key`` + ``hmac_instructions``
        WHEN ``access="ANYONE_ANONYMOUS"`` (see SECURITY NOTE). ``exec_url``
        is the live endpoint; ``project_url`` deep-links to the editor so
        the user can inspect / tweak the code.

    Raises:
        ToolError: blank / handler-less ``script_body``, blank ``title``,
            an invalid ``execute_as`` / ``access``, an ANYONE_ANONYMOUS
            deploy whose ``script_body`` has no guardable top-level
            ``doPost`` declaration, or any Apps Script API error — the
            standard decorator envelope renders these.

    Choreography: Claude writes the ``doGet`` / ``doPost`` body for the
    integration, calls this once, and hands the returned ``exec_url`` to
    the user to paste into the external service's webhook configuration.
    Re-running creates a SEPARATE deployment with a NEW URL (not an
    in-place update) — deploy once, reuse the URL.

    SECURITY NOTE: ``access="ANYONE_ANONYMOUS"`` makes ``/exec`` world-
    reachable. Because Apps Script can't put auth in front of an anonymous
    Web App, this tool now AUTO-INJECTS an HMAC request guard into the
    deployed code for the anonymous case: it wraps your ``doPost`` so every
    request must carry a valid ``X-MCP-Signature`` /  ``X-MCP-Timestamp``
    (HMAC-SHA256 over ``"<timestamp>.<body>"`` with a freshly generated
    per-deploy key) before your handler runs; unsigned/forged/stale requests
    are rejected with ``stage:'auth'``. The generated key is returned as
    ``hmac_key`` (shown ONCE) along with ``hmac_instructions`` describing the
    header scheme — give them to whoever calls the webhook. If a guard can't
    be injected (no top-level ``doPost``), the deploy is refused rather than
    shipped unprotected; deploy with ``DOMAIN`` / ``MYSELF`` for a non-
    public endpoint instead, or for a public GET-only/unauthenticated webhook
    do your own in-handler check and use ``access="ANYONE"``.
    """
    hmac_key: str | None = None
    effective_body = script_body
    if access == "ANYONE_ANONYMOUS":
        # World-reachable: don't ship the handler unguarded. Generate a
        # per-deploy key and wrap doPost with an HMAC verify gate. Injection
        # raises (→ ToolError) if there's no guardable doPost, so we never
        # silently deploy an unauthenticated public endpoint.
        hmac_key = generate_hmac_key()
        effective_body = _inject_webapp_hmac_guard(script_body, hmac_key)

    deployment = _deploy_web_app_project(
        creds,
        script_body=effective_body,
        title=title,
        execute_as=execute_as,
        access=access,
    )
    result = {
        "script_id": deployment.script_id,
        "deployment_id": deployment.deployment_id,
        "version": deployment.version,
        "exec_url": deployment.url,
        "execute_as": execute_as,
        "access": access,
        "project_url": (
            f"https://script.google.com/d/{deployment.script_id}/edit"
        ),
    }
    if hmac_key is not None:
        result["hmac_key"] = hmac_key
        result["hmac_instructions"] = (
            "This endpoint is public, so it is protected by an HMAC request "
            "guard. Each request must include headers: "
            "X-MCP-Timestamp: <current unix seconds>, and "
            "X-MCP-Signature: lowercase hex of "
            "HMAC_SHA256(hmac_key, timestamp + '.' + raw_request_body). "
            "Requests without a valid, fresh (within 5 minutes) signature are "
            "rejected. Store hmac_key as a secret; it is shown only once."
        )
    return result
