"""Automation lifecycle MCP tools: inventory + uninstall + update.

Three tools that close the "install-only, no lifecycle" gap (inventory gaps
#1/#2/#6; Stream-0 findings S0-1..S0-4). All are ``as_*`` (appscriptly-native)
and register via auto-discovery (this module is a non-underscore leaf under
``services/apps_script/``; the orchestration logic lives in the
discovery-skipped ``_lifecycle.py``).

- ``as_list_installed_automations`` — the forward-only inventory. Reads the
  per-user automation ledger (the ONLY discovery surface: minted projects
  are invisible to ``drive.file``, S0-1). Pure-local read, no Google API
  call, no creds — so ``creds=False`` / ``external=False``.

- ``as_uninstall_automation(script_id)`` — undeploy + disarm + forget, with
  an HONEST response about what lingers (the project file; S0-4). Touches
  the Apps Script API, so ``creds=True`` with the baseline
  ``GAS_BOUND_SCOPES``.

- ``as_update_automation(script_id, script_body, ...)`` — re-push CURRENT
  codegen to the EXISTING project (consent-preserving: same script_id, new
  content + version + deployment; never a new project). Closes gap #6
  (stale generated-code drift). Detects a scope addition and surfaces
  ``needs_reactivation`` + the shared activation fields.

All feed the observability tool the ledger was designed to unblock:
``as_list_script_processes`` needs a ``script_id`` the user must already
hold (S0-2); the inventory is where those ids now come from.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError

from appscriptly import automation_ledger
from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    _ledger_user_id,
    _reactivation_function,
    uninstall_automation as _uninstall_automation,
    update_automation as _update_automation,
)
from appscriptly.services.apps_script.api import build_manifest as _build_manifest
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import (
    AS_LIST_INSTALLED_AUTOMATIONS_OUTPUT_SCHEMA,
    AS_UNINSTALL_AUTOMATION_OUTPUT_SCHEMA,
    AS_UPDATE_AUTOMATION_OUTPUT_SCHEMA,
)

# Imported for parity with the sibling apps_script tools; not used on the
# happy path (as_list is creds=False; as_uninstall's @workspace_tool
# envelope injects creds + maps HttpError). Kept top-level so a future
# error-path addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# Static, activation-relevant class per installer tool — surfaced in the
# inventory so a caller knows what it takes to make each automation live
# WITHOUT a live probe (the live activation UX is a separate concern):
#   scheduled_trigger / reactive_trigger — needs a one-time installTrigger
#       Run + Allow in the editor before it ever fires.
#   menu_action — an on-demand menu item; runs (and authorizes) on first click.
#   menu — a custom menu; appears on file open, per-item auth on first click.
#   custom_function — a =FUNCTION(); resolves after a one-time reload.
#   web_app — a /exec endpoint; gated by Google's one-time per-script consent.
#   generic — a raw as_generate_bound_script; depends on the caller's code.
_ACTIVATION_MODEL: dict[str, str] = {
    "as_install_sheet_dashboard": "scheduled_trigger",
    "as_install_calendar_sync": "scheduled_trigger",
    "as_install_task_rollover": "scheduled_trigger",
    "as_install_edit_trigger": "reactive_trigger",
    "as_install_form_handler": "reactive_trigger",
    "as_install_contact_sync": "reactive_trigger",
    "as_install_doc_menu": "menu",
    "as_install_sheet_menu": "menu",
    "as_install_slides_menu": "menu",
    "as_grade_form_responses": "menu_action",
    "as_refresh_linked_slides": "menu_action",
    "as_generate_video_deck": "menu_action",
    "as_install_custom_function": "custom_function",
    "as_generate_bound_script": "generic",
    "as_deploy_web_app": "web_app",
}


def _to_inventory_entry(row: dict) -> dict:
    """Project one ledger row to the inventory's public shape.

    Drops internal columns (``user_id``, ``updated_at``) and adds the
    static ``activation_model`` label. ``handler_functions`` is already a
    list (parsed by the ledger layer).
    """
    return {
        "script_id": row["script_id"],
        "tool": row["tool"],
        "container_id": row.get("container_id"),
        "container_kind": row.get("container_kind"),
        "deployment_id": row.get("deployment_id"),
        "project_url": row.get("project_url"),
        "exec_url": row.get("exec_url"),
        "content_hash": row.get("content_hash"),
        "created_at": row.get("created_at"),
        "activation_model": _ACTIVATION_MODEL.get(row["tool"], "unknown"),
        "handler_functions": row.get("handler_functions") or [],
    }


@workspace_tool(
    title="List the automations appscriptly installed for you",
    service="apps_script",
    readonly=True,
    destructive=False,
    idempotent=True,
    # Pure-local ledger read — no Google API call (like server_guide /
    # gdocs_help). openWorldHint=False.
    external=False,
    # creds=False: reads only the local per-user automation ledger. The
    # caller is identified from the auth context (current_user_id_or_none),
    # which does not require injected Google credentials.
    creds=False,
    output_schema=AS_LIST_INSTALLED_AUTOMATIONS_OUTPUT_SCHEMA,
)
def as_list_installed_automations() -> dict:
    """List the persistent automations appscriptly has installed for you.

    Every ``as_install_*`` / ``as_generate_bound_script`` / ``as_deploy_web_app``
    call mints an Apps Script project in your Google account, but those
    projects are INVISIBLE to normal Drive listing (Apps Script projects
    are created through the Apps Script API, not Drive, so they never enter
    this connector's per-file view). This tool is therefore the ONLY way to
    re-find what you have installed once the original install messages have
    scrolled out of the conversation.

    USE WHEN: the user asks "what automations / scripts have you set up for
    me?", wants to clean up duplicates, needs the ``script_id`` of an earlier
    install to check its run history with ``as_list_script_processes``, or
    is about to uninstall something with ``as_uninstall_automation``.

    Returns ``{automations, count}``. Each entry carries: ``script_id`` (the
    id the other lifecycle/observability tools take), ``tool`` (which
    installer created it), ``container_id`` / ``container_kind`` (the Doc /
    Sheet / Slides it is bound to, or null for a standalone web app),
    ``project_url`` (deep-link to the script editor), ``created_at`` (unix
    seconds), and ``activation_model`` — a hint at what it takes to make
    that automation live (``scheduled_trigger`` / ``reactive_trigger`` need
    a one-time in-editor activation; ``menu`` appears on file open;
    ``custom_function`` needs a reload; ``web_app`` is gated by Google's
    one-time consent). ``handler_functions`` names the trigger handler(s)
    for the trigger classes.

    Inventory is FORWARD-ONLY: it lists automations installed AFTER this
    feature shipped (nothing can reconstruct pre-existing installs, since
    Drive cannot enumerate minted script projects). An empty list means no
    automations have been recorded for you yet, not that none exist.
    """
    user_id = _ledger_user_id()
    rows = automation_ledger.list_automations(user_id)
    automations = [_to_inventory_entry(r) for r in rows]
    return {"automations": automations, "count": len(automations)}


@workspace_tool(
    title="Uninstall an automation appscriptly installed",
    service="apps_script",
    readonly=False,
    # Removes deployments + overwrites the script with an inert stub — a
    # state-removing operation.
    destructive=True,
    # Re-uninstalling an already-uninstalled (or already-forgotten)
    # automation is a safe no-op that reports the current truth.
    idempotent=True,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_UNINSTALL_AUTOMATION_OUTPUT_SCHEMA,
)
def as_uninstall_automation(creds, script_id: str) -> dict:
    """Uninstall an automation appscriptly installed (undeploy + disarm).

    USE WHEN: the user wants to stop / remove an automation you set up — a
    scheduled dashboard refresh, a custom menu, an onEdit / form handler, a
    web app, etc. Get the ``script_id`` from ``as_list_installed_automations``.

    IMPORTANT - uninstall is HONESTLY PARTIAL. Google gives the connector
    no way to fully delete a bound Apps Script project, so this does the
    most it can and tells you exactly what it did and what remains:

      * UNDEPLOYS every deployment (removes web-app ``/exec`` endpoints and
        published versions).
      * DISARMS the code: replaces it with an inert stub, so menus stop
        appearing, custom functions stop resolving, and any scheduled /
        reactive trigger deletes itself the next time it would have fired
        (the stub's handlers self-remove all project triggers on fire).
      * FORGETS it from your appscriptly inventory (it stops appearing in
        ``as_list_installed_automations``).

    What it CANNOT do: delete the Apps Script PROJECT FILE itself — there is
    no API for that and this connector cannot trash a script project. The
    response returns the editor ``project_url`` so you can remove the file
    manually (File > Move to trash) if you want it fully gone; you can also
    delete any leftover trigger there under the Triggers (clock) panel.

    Args:
        script_id: the id of the automation to uninstall (from
            ``as_list_installed_automations``).

    Returns:
        ``{script_id, status, undeployed_count, undeploy_errors,
        content_disarmed, ledger_forgotten, project_file_removed,
        project_url, message}``. ``status`` is ``uninstalled`` normally, or
        ``already_gone`` if the project no longer exists. ``message`` is a
        user-ready summary — surface it verbatim so the user knows the
        project file lingers.

    Raises:
        ToolError: the automation is recorded under a DIFFERENT account, or
            any Apps Script API error (rendered by the standard envelope).
    """
    if not script_id or not script_id.strip():
        raise ValueError(
            "script_id cannot be empty - pass the id of the automation to "
            "uninstall (from as_list_installed_automations)."
        )
    script_id = script_id.strip()

    row = automation_ledger.get_automation(script_id)
    me = _ledger_user_id()
    if row is not None and row.get("user_id") != me:
        # Defense in depth: the creds are the caller's, so they could only
        # touch their own project anyway, but refuse loudly rather than act
        # on another tenant's recorded automation.
        raise ToolError(
            "That automation is recorded under a different account and "
            "cannot be uninstalled from here."
        )

    result = _uninstall_automation(
        creds,
        script_id,
        handler_functions=(row or {}).get("handler_functions"),
        forget=True,
    )
    if row is None:
        # Uninstalling by an id we never recorded is allowed (it is the
        # caller's own account), but flag that it was not in the inventory
        # so a typo'd id is not silently treated as a success.
        result["note"] = (
            "This script was not in your appscriptly inventory; it was "
            "uninstalled by id anyway (undeploy + disarm attempted)."
        )
    return result


@workspace_tool(
    title="Update an installed automation in place (re-push current codegen)",
    service="apps_script",
    readonly=False,
    # Replaces the automation's code with a NEW version rather than removing
    # it; an update is not a delete, so it is not marked destructive (same
    # posture as as_generate_bound_script / gsheets_write_range).
    destructive=False,
    # Re-running with identical content returns status="unchanged" and
    # re-pushes nothing.
    idempotent=True,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_UPDATE_AUTOMATION_OUTPUT_SCHEMA,
)
def as_update_automation(
    creds,
    script_id: str,
    script_body: str,
    manifest: dict | None = None,
    handler_functions: list[str] | None = None,
    allow_restricted_scopes: bool = False,
) -> dict:
    """Update an installed bound automation in place, preserving its consent.

    USE WHEN: you have improved the generated code of an automation you
    installed earlier (a codegen fix, a scope correction, an added step) and
    want to roll it out to the EXISTING automation WITHOUT making the user
    re-do setup. Get ``script_id`` from ``as_list_installed_automations``.

    This re-pushes your regenerated ``.gs`` + manifest to the SAME Apps
    Script project (a new version + deployment on the same ``script_id``). It
    NEVER mints a new project, so the user's per-script authorization and any
    installed trigger are PRESERVED, unlike uninstalling and re-installing
    (which would create a fresh project and require a fresh Allow).

    You regenerate and pass the CURRENT code: the server does not store the
    original inputs, so Claude re-authors ``script_body`` (and ``manifest``)
    the same way the original installer does TODAY, at the current codegen.
    This is how an update FIXES an old automation: re-running the current
    installer logic now threads the container data scope
    (``*.currentonly``, the N-S3V-1 / PR-G fix) into the manifest and wraps
    the handler body with the failure reporter (Stream 4), so passing that
    regenerated content refreshes a stale script to the corrected manifest.
    Idempotent: if the regenerated content is identical to what is deployed,
    the tool returns ``status: "unchanged"`` and re-pushes nothing.

    SCOPE-CHANGE / RE-ACTIVATION: if the new manifest declares an OAuth scope
    the deployed version did not carry, the user must Run + Allow once to
    grant it. The response then sets ``needs_reactivation: true`` with
    ``added_scopes`` and the shared activation fields
    (``activation_url`` / ``activation_instructions`` / ``activation_function``).
    A pure content change with no new scope needs NO re-Allow. (Note: an
    update does not itself activate a never-activated automation; if the
    original install still needed a one-time activation, that is still true
    after the update.)

    Args:
        script_id: the automation to update (from
            ``as_list_installed_automations``). Must be a BOUND automation;
            standalone web apps are updated with ``as_deploy_web_app``
            (``on_conflict="replace"``), which handles the new ``/exec`` URL
            + HMAC guard.
        script_body: the regenerated ``.gs`` source (the current codegen).
            Claude authors it. Required.
        manifest: OPTIONAL high-level manifest description (same shape as
            ``as_generate_bound_script``: ``menu`` / ``triggers`` /
            ``sidebar_html`` / ``oauth_scopes``). Omit for a bare manifest.
            The restricted-scope guard applies.
        handler_functions: OPTIONAL updated installable-trigger handler names
            (for the self-disarm on a later uninstall). Omit to keep the
            recorded ones.
        allow_restricted_scopes: OPTIONAL opt-in to permit a Google RESTRICTED
            scope in the manifest (default False rejects them), same as
            ``as_generate_bound_script``.

    Returns:
        ``{script_id, status, content_hash_before, content_hash_after,
        deployment_id, needs_reactivation, added_scopes, message}`` plus the
        activation fields when ``needs_reactivation`` is true. ``status`` is
        ``updated`` or ``unchanged``.

    Raises:
        ToolError: the automation is not in your inventory, is recorded under
            a different account, or is a standalone web app; or any Apps
            Script API error.
    """
    if not script_id or not script_id.strip():
        raise ValueError(
            "script_id cannot be empty - pass the id of the automation to "
            "update (from as_list_installed_automations)."
        )
    script_id = script_id.strip()
    if not script_body or not script_body.strip():
        raise ValueError(
            "script_body cannot be empty - pass the regenerated .gs source "
            "for the automation."
        )

    row = automation_ledger.get_automation(script_id)
    me = _ledger_user_id()
    if row is None:
        raise ToolError(
            "That automation is not in your appscriptly inventory, so there "
            "is nothing to update. Create one with an installer "
            "(as_generate_bound_script / as_install_*) first; updates apply "
            "to automations this connector recorded."
        )
    if row.get("user_id") != me:
        raise ToolError(
            "That automation is recorded under a different account and "
            "cannot be updated from here."
        )
    if row.get("container_kind") == "webapp" or row.get("tool") == "as_deploy_web_app":
        raise ToolError(
            "That automation is a standalone web app. Update it with "
            "as_deploy_web_app (on_conflict='replace'), which handles the new "
            "/exec URL and the HMAC guard. as_update_automation updates bound "
            "(container) automations."
        )

    # Build the manifest (reuses the restricted-scope guard).
    manifest_dict = _build_manifest(
        manifest, allow_restricted_scopes=allow_restricted_scopes
    )
    handlers = (
        handler_functions
        if handler_functions is not None
        else (row.get("handler_functions") or [])
    )

    result = _update_automation(
        creds,
        script_id,
        script_body=script_body,
        manifest_dict=manifest_dict,
        handler_functions=handlers,
        row=row,
    )

    response: dict = {
        "script_id": result.script_id,
        "status": result.status,
        "content_hash_before": result.content_hash_before,
        "content_hash_after": result.content_hash_after,
        "deployment_id": result.deployment_id,
        "needs_reactivation": result.needs_reactivation,
        "added_scopes": result.added_scopes,
    }
    if result.status == "unchanged":
        response["message"] = (
            "No change: the regenerated code is identical to what is already "
            "deployed, so nothing was re-pushed."
        )
    elif result.needs_reactivation:
        fn = _reactivation_function(script_body)
        instructions = (
            f"This update added OAuth scope(s) {result.added_scopes} that the "
            f"deployed version did not carry, so it needs a one-time re-Allow. "
            f"Open the activation URL as the Google user who owns the script, "
            f"select `{fn}` in the editor's function dropdown, click Run once, "
            f"then click Allow. The updated code is already deployed; it can "
            f"use the new scope(s) only after that one-time grant."
        )
        response["message"] = (
            "Automation updated. One step remains: a newly added OAuth scope "
            "needs a one-time re-Allow (see activation_instructions)."
        )
        response.update(build_activation_fields(script_id, fn, instructions))
    else:
        response["message"] = (
            "Automation updated in place: new code and deployment on the same "
            "project, so your existing authorization and any installed trigger "
            "are preserved. No re-Allow is needed."
        )
    return response
