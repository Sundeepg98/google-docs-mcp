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

import json

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
from appscriptly.services.apps_script._recipes import (
    RECIPES,
    RecipeSpec,
    render as _render,
    required_param_offenders as _required_param_offenders,
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


def _stored_recipe_params(row: dict) -> dict:
    """Parse a ledger row's recorded install params (the S5 regeneration input).

    Returns the ``params_json`` column decoded to a dict, or ``{}`` when it is
    NULL / empty / unparseable / not an object (the safe "nothing to replay"
    reading). A recipe row minted after S5 always carries its params; an empty
    result signals a pre-S5 or hand-tampered row, which the update tool treats
    as not-regenerable (it asks for a script_body instead).
    """
    raw = row.get("params_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _validate_recipe_regeneration_params(spec: RecipeSpec, params: dict) -> None:
    """Reject a recipe regeneration whose params null / wrong-type a required
    input, with a clean ValueError instead of a cryptic template TypeError.

    A ``params`` override that sets a REQUIRED recipe input to null (or the
    wrong JSON type) would otherwise surface as a raw ``TypeError`` from deep
    inside a generator builder (atomic, but cryptic). Delegates the
    required-present / non-null / type check to the shared registry validator
    (``_recipes.required_param_offenders`` -- the SAME one ``as_install_recipe``
    uses), then raises naming the offending key(s) + the recipe. Optional params
    may legitimately be absent or null, so only required inputs are checked.
    Runs BEFORE render (and thus before any Apps Script push or ledger write):
    a bad override changes nothing.
    """
    offenders = _required_param_offenders(spec, params)
    if offenders:
        raise ValueError(
            f"Cannot regenerate the '{spec.name}' automation: a params "
            f"override left required recipe input(s) invalid: "
            f"{'; '.join(offenders)}. Fix the override, or omit it to reuse "
            f"the recorded install params."
        )


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
    script_body: str | None = None,
    manifest: dict | None = None,
    handler_functions: list[str] | None = None,
    params: dict | None = None,
    allow_restricted_scopes: bool = False,
) -> dict:
    """Update an installed bound automation in place, preserving its consent.

    USE WHEN: you want to roll a codegen fix, a scope correction, or an added
    step out to an automation you installed earlier WITHOUT making the user
    re-do setup. Get ``script_id`` from ``as_list_installed_automations``.

    This re-pushes to the SAME Apps Script project (a new version + deployment
    on the same ``script_id``). It NEVER mints a new project, so the user's
    per-script authorization and any installed trigger are PRESERVED, unlike
    uninstalling and re-installing (which would need a fresh Allow). Idempotent:
    if the new content is identical to what is deployed, the tool returns
    ``status: "unchanged"`` and re-pushes nothing.

    TWO WAYS TO PROVIDE THE NEW CODE - pick by whether the automation came
    from a recipe (most ``as_install_*`` tools install FROM a recipe):

    * RECIPE automations - OMIT ``script_body``. The server REGENERATES the
      ``.gs`` + manifest itself from the recipe at the CURRENT codegen, using
      the install params it recorded when you first installed it. You do NOT
      re-author anything. This is the deterministic way to roll a codegen fix
      out to a fleet: for each installed automation, call
      ``as_update_automation(script_id)`` and the current template (with the
      ``*.currentonly`` data scope from PR-G, the Stream-4 failure reporter,
      etc.) is regenerated and pushed. To CHANGE an input (e.g. a schedule or
      a handler body), pass ``params`` with just the keys to override - they
      are merged over the recorded params and re-stored. The response reports
      ``regenerated_from_recipe: true``.

    * RAW automations (an ``as_generate_bound_script`` script, which has no
      recipe) - PASS the regenerated ``script_body`` (and ``manifest``). The
      server has no recipe to replay for these, so you re-author the current
      code the same way the original call did. The response reports
      ``regenerated_from_recipe: false``. (You may also pass ``script_body``
      for a recipe automation to push one-off custom code; that takes the
      caller-body path too. The one exception is the video-deck renderer,
      which is refused even WITH a body: each install mints a fresh single-use
      upload token that cannot be reproduced or re-authored, so re-install it
      instead.)

    SCOPE-CHANGE / RE-ACTIVATION: if the new manifest declares an OAuth scope
    the deployed version did not carry, the user must Run + Allow once to grant
    it. The response then sets ``needs_reactivation: true`` with ``added_scopes``
    and the shared activation fields (``activation_url`` /
    ``activation_instructions`` / ``activation_function``). A pure content
    change with no new scope needs NO re-Allow. (An update does not itself
    activate a never-activated automation; if the original install still needed
    a one-time activation, that is still true after the update.)

    Args:
        script_id: the automation to update (from
            ``as_list_installed_automations``). Must be a BOUND automation;
            standalone web apps are updated with ``as_deploy_web_app``
            (``on_conflict="replace"``), which handles the new ``/exec`` URL
            + HMAC guard.
        script_body: OMIT for a recipe automation (the server regenerates).
            For a raw ``as_generate_bound_script`` automation, pass the
            regenerated ``.gs`` source. An explicitly-empty string is rejected.
        manifest: OPTIONAL high-level manifest description for the CALLER-BODY
            path (same shape as ``as_generate_bound_script``: ``menu`` /
            ``triggers`` / ``sidebar_html`` / ``oauth_scopes``). The
            restricted-scope guard applies. Ignored when the server regenerates
            from a recipe (the recipe owns the manifest).
        handler_functions: OPTIONAL updated installable-trigger handler names
            for the CALLER-BODY path (for the self-disarm on a later uninstall).
            Omit to keep the recorded ones; the recipe derives its own.
        params: OPTIONAL per-key overrides for a RECIPE regeneration (e.g.
            ``{"schedule": "weekly"}``). Merged over the recorded install
            params and re-stored. Only valid when the server regenerates
            (``script_body`` omitted on a recipe automation). Changing a
            trigger's SCHEDULE regenerates the code, but a trigger already
            installed in the user's account keeps its original schedule until
            they re-run the activation (Run installTrigger + Allow), which
            re-wires it cleanly (installTrigger de-dups, so no duplicate).
            A null or wrong-typed required input here is rejected up front.
        allow_restricted_scopes: OPTIONAL opt-in to permit a Google RESTRICTED
            scope in a CALLER-BODY manifest (default False rejects them), same
            as ``as_generate_bound_script``.

    Returns:
        ``{script_id, status, content_hash_before, content_hash_after,
        deployment_id, needs_reactivation, added_scopes, regenerated_from_recipe,
        message}`` plus the activation fields when ``needs_reactivation`` is
        true. ``status`` is ``updated`` or ``unchanged``.

    Raises:
        ToolError: the automation is not in your inventory, is recorded under
            a different account, is a standalone web app, or is a video-deck
            renderer (which mints a per-install token and must be re-installed
            rather than updated); or any Apps Script API error.
    """
    if not script_id or not script_id.strip():
        raise ValueError(
            "script_id cannot be empty - pass the id of the automation to "
            "update (from as_list_installed_automations)."
        )
    script_id = script_id.strip()

    # An OMITTED script_body means "regenerate from the recipe"; an explicitly
    # blank one is a caller mistake (a different thing) and is rejected.
    if script_body is not None and not script_body.strip():
        raise ValueError(
            "script_body cannot be empty - either omit it entirely to "
            "regenerate this automation from its recipe at the current "
            "codegen, or pass the regenerated .gs source."
        )
    body_supplied = script_body is not None

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

    # Resolve the recorded recipe (S5). A row minted from a registry recipe
    # regenerates server-side; a raw as_generate_bound_script row (recipe NULL)
    # keeps the caller-supplied-body path.
    recipe_name = row.get("recipe")
    spec = RECIPES.get(recipe_name) if recipe_name else None

    # A recipe with an impure per-install pre_mint hook (video_deck: a fresh
    # single-use HMAC upload token) CANNOT be reproduced from stored inputs, and
    # a caller cannot re-author a valid one either - refuse in place and point
    # at a fresh re-install (which mints a new batch + token).
    if spec is not None and spec.pre_mint is not None:
        raise ToolError(
            "That automation is a video-deck renderer. Each install mints a "
            "fresh single-use upload token, so it cannot be updated in place. "
            "Re-install it with as_generate_video_deck (which mints a new "
            "frames batch and token); remove the old one with "
            "as_uninstall_automation."
        )

    if spec is not None and not body_supplied:
        # SERVER-SIDE REGENERATION from the recipe at the CURRENT codegen. No
        # caller re-authoring: render() reruns the recipe's build + manifest
        # plan (threading container_data_scope + the failure reporter) exactly
        # as a fresh install would, through the SAME build_manifest
        # restricted-scope guard. params overrides merge over the recorded
        # install params and are re-stored on success.
        stored_params = _stored_recipe_params(row)
        if not stored_params:
            raise ToolError(
                f"That automation is recorded as the '{recipe_name}' recipe "
                f"but its install params were not stored, so it cannot be "
                f"regenerated automatically. Pass the regenerated script_body "
                f"to update it directly."
            )
        merged_params = {**stored_params, **(params or {})}
        # Reject a null / wrong-typed required input from the params override
        # BEFORE render, so a bad override raises a clean ValueError naming the
        # key + recipe instead of a cryptic TypeError from the template (and
        # touches no Apps Script API and no ledger row).
        _validate_recipe_regeneration_params(spec, merged_params)
        rendered = _render(spec, merged_params)
        effective_body = rendered.script_body
        manifest_dict = rendered.manifest
        handlers = list(rendered.handler_functions)
        recipe_params_to_store: dict | None = merged_params
        regenerated_from_recipe = True
    else:
        # CALLER-SUPPLIED-BODY path: raw (recipe-less) rows, and recipe rows
        # where the caller explicitly passed a body to push one-off code.
        if params is not None:
            raise ValueError(
                "params overrides apply only when the server regenerates from "
                "a recipe (omit script_body). This update is using the "
                "script_body you passed; edit the code there instead."
            )
        if not body_supplied:
            if recipe_name:
                raise ToolError(
                    f"That automation was installed from the '{recipe_name}' "
                    f"recipe, which this server version no longer provides, so "
                    f"it cannot be regenerated automatically. Pass the "
                    f"regenerated script_body to update it directly."
                )
            raise ValueError(
                "script_body is required: this automation was not installed "
                "from a recipe (nothing to regenerate from), so pass the "
                "regenerated .gs source."
            )
        manifest_dict = _build_manifest(
            manifest, allow_restricted_scopes=allow_restricted_scopes
        )
        effective_body = script_body
        handlers = (
            handler_functions
            if handler_functions is not None
            else (row.get("handler_functions") or [])
        )
        recipe_params_to_store = None
        regenerated_from_recipe = False

    result = _update_automation(
        creds,
        script_id,
        script_body=effective_body,
        manifest_dict=manifest_dict,
        handler_functions=handlers,
        row=row,
        recipe_params=recipe_params_to_store,
    )

    response: dict = {
        "script_id": result.script_id,
        "status": result.status,
        "content_hash_before": result.content_hash_before,
        "content_hash_after": result.content_hash_after,
        "deployment_id": result.deployment_id,
        "needs_reactivation": result.needs_reactivation,
        "added_scopes": result.added_scopes,
        "regenerated_from_recipe": regenerated_from_recipe,
    }
    if result.status == "unchanged":
        response["message"] = (
            "No change: the "
            + ("regenerated" if regenerated_from_recipe else "new")
            + " code is identical to what is already deployed, so nothing was "
            "re-pushed."
        )
    elif result.needs_reactivation:
        fn = _reactivation_function(effective_body)
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
