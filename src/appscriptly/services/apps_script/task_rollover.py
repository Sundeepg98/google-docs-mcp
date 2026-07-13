"""``as_install_task_rollover`` — time-driven Tasks automation via Apps Script.

GAS service-parity (Tasks). A *use-case* tool layered on the PR-Δ7
bound-script generator primitive (``as_generate_bound_script`` /
``services/apps_script/api.py``). It is a Tasks analogue of
``as_install_sheet_dashboard``: install a **time-driven** automation into a
Google Sheet that runs a caller-supplied task function on a schedule
(daily / hourly / weekly) via an installable time trigger. The function
uses the Tasks **advanced service** (``Tasks.Tasks`` / ``Tasks.Tasklists``)
to do recurring task orchestration that the one-shot REST tools can't
express on their own - the canonical example being "roll over" incomplete
tasks: each morning, move every still-open task whose due date has passed
to today (or create today's tasks from a Sheet's rows). It runs on Google's
clock with no Claude call in the loop after the install.

**Why this needs Apps Script (the REST gap).** The Tasks REST tools
(``gtasks_create_task`` etc.) each perform ONE mutation, in the
conversation, with Claude in the loop. A *recurring, self-running* job that
re-scans the task list and reconciles it on a schedule is not something the
native CRUD tools express; a bound script with a time trigger is. So this
tool is the second (automation) lever for Tasks, complementing the native
REST lever.

**The Tasks ADVANCED service (not ``CalendarApp``-style built-in).** Unlike
Calendar/Contacts, Apps Script has NO built-in ``TasksApp`` - Tasks is only
reachable via the **advanced service** (``Tasks.Tasks.list(...)`` etc.).
An advanced service must be declared in the script manifest's
``dependencies.enabledAdvancedServices`` (``userSymbol: "Tasks"``,
``serviceId: "tasks"``, ``version: "v1"``) for the ``Tasks`` global to
resolve at runtime. ``build_manifest`` (the shared primitive) only emits
``timeZone`` / ``runtimeVersion`` / ``oauthScopes``, so this module merges
the ``dependencies`` block into the manifest dict AFTER ``build_manifest``
and BEFORE ``set_project_content`` (which serializes every manifest key
except the internal ``__plan__`` echo). The shared primitive is left
untouched.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py`` and the schedule→trigger
mapping + body synthesis are reused from ``sheet_dashboard``. This module's
OWN contribution is the Tasks-specific manifest wiring (the advanced-service
dependency + the ``tasks`` scope).

**⚠️ Scope (the load-bearing part - verify-LAST).** The Tasks advanced
service needs the full ``https://www.googleapis.com/auth/tasks`` scope to
read/write tasks. That scope lives ONLY in the GENERATED bound script's
manifest (declared via ``build_manifest``'s ``oauth_scopes``) - authorized
by the user the first time they run ``installTrigger`` in the editor. This
tool DECLARES only ``GAS_BOUND_SCOPES`` (``script.projects`` +
``script.deployments``) for appscriptly's OWN consent - both already
baseline-granted. So this tool adds NO new scope to appscriptly's own
consent / OAuth-verification set (same model as ``grade_form_responses``).

**The trigger-activation caveat (same as sheet_dashboard).** An
*installable* time trigger only comes into existence when ``installTrigger``
actually *runs*; the deploy wires it but does NOT make it live (and the
Apps Script REST API can't create the trigger remotely). The return payload
is HONEST: ``trigger_active`` is ``False`` / ``activation_required`` is
``True`` with the one-step instruction.
"""
from __future__ import annotations

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._observability import (
    add_mail_scope as _add_mail_scope,
    guard_name_for as _guard_name_for,
)
from appscriptly.services.apps_script.api import (
    build_manifest as _build_manifest,
    container_data_scope as _container_data_scope,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.services.apps_script.sheet_dashboard import (
    VALID_SCHEDULES,
    build_dashboard_script_body as _build_time_trigger_script_body,
)
from appscriptly.tool_schemas import AS_INSTALL_TASK_ROLLOVER_OUTPUT_SCHEMA

# Imported for parity with the sibling apps_script tools; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# The Tasks advanced service needs the full tasks scope to read/write tasks.
# Declared in the GENERATED manifest only - NOT added to appscriptly's own
# consent (see the module docstring's scope note).
_TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"

# The advanced-service identifier the generated manifest must enable for the
# `Tasks` global to resolve at runtime. This is the documented Apps Script
# enabledAdvancedServices shape for Google Tasks API v1.
_TASKS_ADVANCED_SERVICE = {
    "userSymbol": "Tasks",
    "serviceId": "tasks",
    "version": "v1",
}


def _with_tasks_advanced_service(manifest_dict: dict) -> dict:
    """Return a copy of ``manifest_dict`` with the Tasks advanced service
    enabled under ``dependencies.enabledAdvancedServices`` (PURE).

    ``build_manifest`` emits only timeZone / runtimeVersion / oauthScopes,
    so the advanced-service dependency is merged here. We merge (rather than
    overwrite) any existing ``dependencies`` so this stays composable, and
    de-dup by ``serviceId`` so calling it twice is idempotent.

    The merged dict is what ``set_project_content`` serializes into the
    generated ``appsscript.json`` (it strips only the internal ``__plan__``
    echo, passing every real key through - including ``dependencies``).
    """
    merged = dict(manifest_dict)
    deps = dict(merged.get("dependencies") or {})
    services = list(deps.get("enabledAdvancedServices") or [])
    if not any(
        s.get("serviceId") == _TASKS_ADVANCED_SERVICE["serviceId"]
        for s in services
    ):
        services.append(dict(_TASKS_ADVANCED_SERVICE))
    deps["enabledAdvancedServices"] = services
    merged["dependencies"] = deps
    return merged


@workspace_tool(
    title="Install a scheduled Google Tasks automation into a Google Sheet",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the Sheet -
    # re-running produces a duplicate bound script. NOT idempotent (same
    # convention as as_install_sheet_dashboard / as_generate_bound_script).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_TASK_ROLLOVER_OUTPUT_SCHEMA,
)
def as_install_task_rollover(
    creds,
    sheet_id: str,
    task_function_body: str,
    schedule: str = "daily",
    hour: int = 6,
    task_note: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a time-driven Google Tasks automation into a Sheet.

    Deploys a *bound* Apps Script into the Sheet that runs your
    ``task_function_body`` on a recurring schedule (daily / hourly /
    weekly) via an installable time trigger. The function uses the Tasks
    **advanced service** (``Tasks.Tasks`` / ``Tasks.Tasklists``) to do
    recurring task orchestration - the classic case being to "roll over"
    incomplete tasks (move every still-open, past-due task to today) or to
    create today's tasks from the Sheet's rows. Once activated it runs on
    Google's clock with NO Claude call in the loop. This composes the
    generic bound-script primitive (``as_generate_bound_script``) for the
    Tasks automation pattern.

    USE WHEN: the user wants their Google Tasks maintained on a schedule
    with no one watching - "every morning, roll any overdue tasks forward
    to today", "daily, create tasks from new rows in my action-items
    Sheet". For a ONE-OFF task mutation, use the ``gtasks_*`` REST tools
    (no script needed).

    YOU AUTHOR THE LOGIC; THE TOOL OWNS THE CHOREOGRAPHY. Supply
    ``task_function_body`` - a NAMED function that does the work via the
    Tasks advanced service, e.g.::

        function rollOverTasks() {
          var lists = Tasks.Tasklists.list().items || [];
          var todayIso = new Date().toISOString();
          for (var l = 0; l < lists.length; l++) {
            var listId = lists[l].id;
            var tasks = Tasks.Tasks.list(listId, {showCompleted: false})
              .items || [];
            for (var t = 0; t < tasks.length; t++) {
              var task = tasks[t];
              if (task.due && task.due < todayIso) {
                task.due = todayIso;                 // roll forward
                Tasks.Tasks.update(task, listId, task.id);
              }
            }
          }
        }

    Its declared name becomes the trigger handler. The generated
    ``installTrigger()`` wires it to the schedule (with dedup-then-create so
    re-running never stacks duplicate triggers); you decide what the task
    logic does. Claude authors the body - same trust model as the other
    apps_script generators. The generated manifest enables the Tasks
    advanced service so ``Tasks.*`` resolves.

    IMPORTANT - activation is a required one-time step (same as
    ``as_install_sheet_dashboard``). An *installable* time trigger only
    exists once its installer runs, and deploying a script does NOT run it
    (and the Apps Script REST API can't create the trigger remotely). So
    this tool WIRES the trigger into the deployed script but the schedule is
    NOT live yet on return. To activate: open the returned ``project_url``,
    run the ``installTrigger`` function once (the editor's Run button), and
    approve the authorization prompt (which includes the ``tasks`` scope -
    see the scope note below). After that single run the schedule fires
    forever. The return payload says so explicitly - ``trigger_active`` is
    ``False`` and ``activation_required`` is ``True`` with the step spelled
    out. Do not tell the user their Tasks are already being maintained until
    they've run ``installTrigger`` once.

    SCOPE NOTE: reading/writing Tasks needs the full ``tasks`` scope, which
    lives ONLY in the generated bound script's manifest (authorized when the
    user runs the trigger). This tool itself adds NO new scope to
    appscriptly's consent - it only uses the baseline Apps Script management
    scopes for the deploy. ``manifest_scope`` in the return reports the
    scope the generated script declares (for transparency).

    Args:
        sheet_id: Drive ID of the Google Sheet to install the automation
            into (the ID part of the Sheet's URL). The bound script + the
            time trigger are attached to THIS Sheet (the Sheet is the
            automation's home; the task logic may also read its rows).
        task_function_body: the ``.gs`` source for the task logic as a
            NAMED function declaration, e.g.
            ``"function rollOverTasks() { /* Tasks.Tasks... */ }"``. Claude
            authors this. Its declared name becomes the trigger handler.
            Required; empty / unnamed bodies are rejected.
        schedule: how often the automation runs - ``"daily"`` (default),
            ``"hourly"``, or ``"weekly"``. ``daily``/``weekly`` fire at
            ``hour``; ``weekly`` fires on Monday; ``hourly`` fires every
            hour (``hour`` ignored).
        hour: hour-of-day (0-23, script time zone) the daily/weekly trigger
            fires. Default ``6``. Ignored for ``hourly``.
        task_note: OPTIONAL human note rendered as a leading comment in the
            generated script (documents intent for anyone who opens the
            editor). Does not affect behavior.
        name: OPTIONAL title for the new Apps Script project. Defaults to a
            generated task-automation name.
        on_conflict: what to do when a task automation from THIS tool
            already exists on this Sheet. "new" (the default) always
            installs a fresh one (which can leave duplicate schedules);
            "replace" uninstalls the prior install(s) on this Sheet first
            (no duplicate, no orphan); "skip" returns the existing install
            unchanged instead of adding a duplicate. Keyed by (this tool,
            this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, sheet_id, schedule, trigger_handler,
        project_url, trigger_active, activation_required,
        activation_instructions, manifest_scope, advanced_service}``.
        ``trigger_handler`` is the parsed task-function name the trigger
        drives. ``trigger_active`` is ``False`` / ``activation_required`` is
        ``True`` on a successful deploy (the trigger is wired but needs a
        one-time ``installTrigger`` run). ``manifest_scope`` is the full
        ``tasks`` scope the GENERATED script declares (transparency - it's
        the bound script's scope, not appscriptly's consent).
        ``advanced_service`` is ``"tasks"`` (the advanced service the
        generated manifest enables so ``Tasks.*`` resolves).

    Raises:
        ValueError: an invalid ``schedule`` / ``hour``, or an empty /
            unnamed ``task_function_body`` (rejected before any API call).
        ToolError: any Apps Script / Drive API error - the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``.

    Choreography: get ``sheet_id`` from the user's URL or a prior
    ``gsheets_create_spreadsheet`` call. After this returns, point the user
    at ``project_url`` to run ``installTrigger`` once - that's the only
    manual step. (The Apps Script scopes are baseline-granted, so most
    users won't see a second OAuth consent for the deploy itself; the
    in-editor ``installTrigger`` run has its own one-time authorization for
    the ``tasks`` scope.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call. These
    #    ValueErrors propagate as ToolError via the decorator envelope.
    if not sheet_id or not sheet_id.strip():
        raise ValueError(
            "sheet_id cannot be empty - pass the Drive ID of the Google "
            "Sheet to install the Tasks automation into."
        )
    if schedule not in VALID_SCHEDULES:
        raise ValueError(
            f"schedule must be one of {sorted(VALID_SCHEDULES)} "
            f"('daily' / 'hourly' / 'weekly'), got {schedule!r}."
        )
    if (
        not isinstance(hour, int)
        or isinstance(hour, bool)
        or not (0 <= hour <= 23)
    ):
        raise ValueError(
            f"hour must be an integer 0-23 (hour-of-day for the daily/"
            f"weekly trigger), got {hour!r}."
        )
    if not task_function_body or not task_function_body.strip():
        raise ValueError(
            "task_function_body cannot be empty - pass the .gs source for "
            "the task logic as a named function declaration (e.g. "
            "`function rollOverTasks() { ... }`)."
        )

    # 2. Synthesize the full .gs body (caller's task fn + dedup'd
    #    installTrigger wiring the schedule). Reuses sheet_dashboard's
    #    time-trigger body builder verbatim; _extract_handler_name (inside)
    #    rejects an unnamed function.
    script_body, handler = _build_time_trigger_script_body(
        task_function_body, schedule, hour, task_note
    )

    # 3. Build the manifest. A time trigger needs script.scriptapp (derived
    #    from the triggers plan), and the Tasks advanced service needs the
    #    full tasks scope (via oauth_scopes). THEN enable the Tasks advanced
    #    service under dependencies so `Tasks.*` resolves at runtime. BOTH
    #    the scope and the dependency land in the GENERATED manifest only -
    #    never in appscriptly's own consent (the verify-LAST guarantee).
    manifest_dict = _build_manifest(
        {
            "triggers": [{"type": "time", "schedule": schedule}],
            # _TASKS_SCOPE (full tasks) is required for the Tasks ADVANCED
            # service (.currentonly is NOT honored for advanced services);
            # container_data_scope("sheets") = spreadsheets.currentonly so the
            # handler can read the bound Sheet's rows via SpreadsheetApp (an
            # explicit oauthScopes block suppresses auto-detection - N-S3V-1);
            # add_mail_scope adds the failure reporter's send scope. GENERATED
            # manifest only, never appscriptly's consent.
            "oauth_scopes": _add_mail_scope(
                [_TASKS_SCOPE, _container_data_scope("sheets")]
            ),
        }
    )
    manifest_dict = _with_tasks_advanced_service(manifest_dict)

    # 4. Default the project name from the schedule when not supplied.
    project_name = name or f"appscriptly tasks automation ({schedule})"

    # 5. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=sheet_id), push the body + manifest, cut a
    #    version + deploy. (We bind directly to the Sheet ID; no Drive
    #    mimeType round-trip - this tool only ever targets a Sheet.)
    result = _mint_bound_automation(
        creds,
        tool="as_install_task_rollover",
        container_id=sheet_id,
        container_kind="sheets",
        project_name=project_name,
        script_body=script_body,
        manifest_dict=manifest_dict,
        on_conflict=on_conflict,
        # Record the GUARD name (installTrigger's actual target) so
        # uninstall's self-disarm reaper redefines the right function.
        handler_functions=[_guard_name_for(handler)],
    )
    script_id = result.script_id
    deployment_id = result.deployment_id

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "sheet_id": sheet_id,
        "schedule": schedule,
        "trigger_handler": handler,
        "project_url": f"https://script.google.com/d/{script_id}/edit",
        # HONEST trigger state: the deploy wires the trigger but does NOT
        # run installTrigger, so the schedule is not live yet. trigger_active
        # is the legacy alias; the unified activation_* fields carry the
        # canonical shape (build_activation_fields).
        "trigger_active": False,
        **build_activation_fields(
            script_id,
            "installTrigger",
            (
                f"Open the script editor ({project_name}) at the "
                f"activation_url, select `installTrigger` in the function "
                f"dropdown and click Run once, then approve the "
                f"authorization prompt (it includes the `tasks` scope the "
                f"automation needs). That activates the {schedule} schedule "
                f"for `{handler}`; it then runs on Google's clock with no "
                f"further action."
            ),
        ),
        # Transparency: the scope the GENERATED bound script declares to
        # read/write Tasks. It is the bound script's manifest scope, NOT a
        # scope added to appscriptly's own OAuth consent.
        "manifest_scope": _TASKS_SCOPE,
        # The advanced service the GENERATED manifest enables so Tasks.*
        # resolves at runtime (dependencies.enabledAdvancedServices).
        "advanced_service": _TASKS_ADVANCED_SERVICE["serviceId"],
    }
