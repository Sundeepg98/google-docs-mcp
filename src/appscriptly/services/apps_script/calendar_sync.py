"""``as_install_calendar_sync`` — time-driven Calendar automation from a Sheet.

GAS service-parity (Calendar). A *use-case* tool layered on the PR-Δ7
bound-script generator primitive (``as_generate_bound_script`` /
``services/apps_script/api.py``). It is the Calendar analogue of
``as_install_sheet_dashboard``: install a **time-driven** automation into a
Google Sheet that runs a caller-supplied sync function on a schedule
(daily / hourly / weekly) via an installable time trigger. The function
uses ``CalendarApp`` to create / update Calendar events from the Sheet's
rows — turning a spreadsheet into a recurring "rows in, calendar events
out" pipeline that runs on Google's clock with no Claude call in the loop
after the install.

**Why this needs Apps Script (the REST gap).** The Calendar REST tools
(``gcal_create_event`` etc.) create ONE event per call, in the
conversation, with Claude in the loop. They cannot install a *recurring,
self-running* job that re-reads a Sheet and reconciles events on a
schedule. That recurring Sheet -> Calendar reconciliation is exactly what a
bound script with a time trigger does — so this tool is the second
(automation) lever for Calendar, complementing the native REST lever.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py`` (``create_bound_project`` →
``set_project_content`` → ``create_deployment``) and the schedule→trigger
mapping mirrors ``sheet_dashboard``. This module's OWN contribution is the
``.gs`` body synthesis (the caller's sync function + a generated
dedup-then-create ``installTrigger()`` wired to the schedule) and the
Calendar-specific scope wiring.

**⚠️ Scope (the load-bearing part — verify-LAST).** ``CalendarApp`` needs
the full ``https://www.googleapis.com/auth/calendar`` scope to WRITE
events. That scope lives ONLY in the GENERATED bound script's manifest
(declared via ``build_manifest``'s ``oauth_scopes``) — it is authorized by
the user the first time they run ``installTrigger`` in the editor (the
bound script's OWN one-time consent). This tool DECLARES only
``GAS_BOUND_SCOPES`` (``script.projects`` + ``script.deployments``) for
appscriptly's OWN consent — both already baseline-granted. So this tool
adds NO new scope to appscriptly's own consent / OAuth-verification scope
set. This is exactly how ``grade_form_responses`` lands the full ``forms``
scope in the generated manifest without touching ``auth.WORKSPACE_SCOPES``.
(The ``calendar`` scope already happens to be in appscriptly's baseline for
the native REST tools, but we still declare it in the generated manifest —
the GAS path's contract is "the user authorizes their OWN script's scopes",
independent of what appscriptly already holds.)

**The trigger-activation caveat (same as sheet_dashboard).** An
*installable* time trigger only comes into existence when ``installTrigger``
actually *runs*; deploying the script does NOT run it, and the Apps Script
REST API has no endpoint to create an installable trigger remotely. So this
tool *wires* the trigger but does NOT make it live. The return payload is
HONEST: ``trigger_active`` is ``False`` and ``activation_required`` is
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
from appscriptly.services.apps_script.api import build_manifest as _build_manifest
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.services.apps_script.sheet_dashboard import (
    VALID_SCHEDULES,
    build_dashboard_script_body as _build_time_trigger_script_body,
)
from appscriptly.tool_schemas import AS_INSTALL_CALENDAR_SYNC_OUTPUT_SCHEMA

# Imported for parity with the sibling apps_script tools; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# CalendarApp needs the FULL calendar scope to create/update events. This is
# declared in the GENERATED manifest only — NOT added to appscriptly's own
# consent (see the module docstring's scope note).
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


@workspace_tool(
    title="Install a scheduled Sheet-to-Calendar sync into a Google Sheet",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the Sheet —
    # re-running produces a duplicate bound script. NOT idempotent (same
    # convention as as_install_sheet_dashboard / as_generate_bound_script).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_CALENDAR_SYNC_OUTPUT_SCHEMA,
)
def as_install_calendar_sync(
    creds,
    sheet_id: str,
    sync_function_body: str,
    schedule: str = "daily",
    hour: int = 6,
    sync_note: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a time-driven Sheet-to-Calendar sync automation into a Sheet.

    Deploys a *bound* Apps Script into the Sheet that runs your
    ``sync_function_body`` on a recurring schedule (daily / hourly /
    weekly) via an installable time trigger. The function uses
    ``CalendarApp`` to create or update Google Calendar events from the
    Sheet's rows — e.g. read a "schedule" tab and ensure a calendar event
    exists for every row. Once activated it runs on Google's clock with NO
    Claude call in the loop. This composes the generic bound-script
    primitive (``as_generate_bound_script``) for the Calendar automation
    pattern, and is the Calendar analogue of ``as_install_sheet_dashboard``.

    USE WHEN: the user wants a Sheet to keep their Calendar in sync on a
    schedule, with no one watching — "every morning, create calendar
    events for any new rows in my events sheet", "hourly, push my roster
    Sheet into Calendar". For a ONE-OFF event, use ``gcal_create_event``
    (no script needed). For a non-time (reactive) Calendar automation,
    compose ``as_generate_bound_script`` with the appropriate trigger.

    YOU AUTHOR THE SYNC; THE TOOL OWNS THE CHOREOGRAPHY. Supply
    ``sync_function_body`` — a NAMED function that does the Sheet -> Calendar
    work, e.g.::

        function syncEvents() {
          var sheet = SpreadsheetApp.getActiveSpreadsheet()
            .getSheetByName('Schedule');
          var cal = CalendarApp.getDefaultCalendar();
          var rows = sheet.getDataRange().getValues();
          for (var i = 1; i < rows.length; i++) {  // skip header
            var title = rows[i][0];
            var start = new Date(rows[i][1]);
            var end = new Date(rows[i][2]);
            cal.createEvent(title, start, end);
          }
        }

    Its declared name becomes the trigger handler. The generated
    ``installTrigger()`` wires it to the schedule (with dedup-then-create so
    re-running never stacks duplicate triggers); you decide what the sync
    does. Claude authors the body — same trust model as the other
    apps_script generators.

    IMPORTANT - activation is a required one-time step (same as
    ``as_install_sheet_dashboard``). An *installable* time trigger only
    exists once its installer runs, and deploying a script does NOT run it
    (and the Apps Script REST API can't create the trigger remotely). So
    this tool WIRES the trigger into the deployed script but the schedule is
    NOT live yet on return. To activate: open the returned ``project_url``,
    run the ``installTrigger`` function once (the editor's Run button), and
    approve the authorization prompt (which includes the ``calendar`` scope
    the sync needs - see the scope note below). After that single run the
    schedule fires forever. The return payload says so explicitly -
    ``trigger_active`` is ``False`` and ``activation_required`` is ``True``
    with the step spelled out. Do not tell the user their Calendar is
    already syncing until they've run ``installTrigger`` once.

    SCOPE NOTE: writing Calendar events needs the full ``calendar`` scope,
    which lives ONLY in the generated bound script's manifest (authorized
    when the user runs the trigger). This tool itself adds NO new scope to
    appscriptly's consent - it only uses the baseline Apps Script
    management scopes for the deploy. ``manifest_scope`` in the return
    reports the scope the generated script declares (for transparency).

    Args:
        sheet_id: Drive ID of the Google Sheet to install the automation
            into (the ID part of the Sheet's URL). The bound script + the
            time trigger are attached to THIS Sheet, and the sync reads
            this Sheet's rows.
        sync_function_body: the ``.gs`` source for the Sheet -> Calendar
            sync as a NAMED function declaration, e.g.
            ``"function syncEvents() { /* CalendarApp...createEvent */ }"``.
            Claude authors this. Its declared name becomes the trigger
            handler. Required; empty / unnamed bodies are rejected.
        schedule: how often the sync runs - ``"daily"`` (default),
            ``"hourly"``, or ``"weekly"``. ``daily``/``weekly`` fire at
            ``hour``; ``weekly`` fires on Monday; ``hourly`` fires every
            hour (``hour`` ignored).
        hour: hour-of-day (0-23, script time zone) the daily/weekly trigger
            fires. Default ``6``. Ignored for ``hourly``.
        sync_note: OPTIONAL human note rendered as a leading comment in the
            generated script (documents the sync's intent for anyone who
            opens the editor). Does not affect behavior.
        name: OPTIONAL title for the new Apps Script project. Defaults to a
            generated calendar-sync name.
        on_conflict: what to do when a sync automation from THIS tool
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
        activation_instructions, manifest_scope}``. ``trigger_handler`` is
        the parsed sync-function name the trigger drives. ``project_url``
        deep-links to the script editor. ``trigger_active`` is ``False`` and
        ``activation_required`` is ``True`` on a successful deploy (the
        trigger is wired but needs a one-time ``installTrigger`` run);
        ``activation_instructions`` is the literal step. ``manifest_scope``
        is the full ``calendar`` scope the GENERATED script declares
        (reported for transparency - it's the bound script's scope, not
        appscriptly's consent).

    Raises:
        ValueError: an invalid ``schedule`` / ``hour``, or an empty /
            unnamed ``sync_function_body`` (rejected before any API call).
        ToolError: any Apps Script / Drive API error - the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``.

    Choreography: get ``sheet_id`` from the user's URL or a prior
    ``gsheets_create_spreadsheet`` call. After this returns, point the user
    at ``project_url`` to run ``installTrigger`` once - that's the only
    manual step. (The Apps Script scopes are baseline-granted, so most
    users won't see a second OAuth consent for the deploy itself; the
    in-editor ``installTrigger`` run has its own one-time authorization for
    the ``calendar`` scope.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call. These
    #    ValueErrors propagate as ToolError via the decorator envelope.
    if not sheet_id or not sheet_id.strip():
        raise ValueError(
            "sheet_id cannot be empty - pass the Drive ID of the Google "
            "Sheet to install the Calendar-sync automation into."
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
    if not sync_function_body or not sync_function_body.strip():
        raise ValueError(
            "sync_function_body cannot be empty - pass the .gs source for "
            "the Sheet -> Calendar sync as a named function declaration "
            "(e.g. `function syncEvents() { ... }`)."
        )

    # 2. Synthesize the full .gs body (caller's sync fn + dedup'd
    #    installTrigger wiring the schedule). We reuse sheet_dashboard's
    #    time-trigger body builder verbatim - it does exactly the right
    #    thing (named-function extraction + dedup-then-create time trigger);
    #    the only Calendar-specific piece is the manifest scope, added next.
    #    _extract_handler_name (inside the builder) rejects an unnamed fn.
    script_body, handler = _build_time_trigger_script_body(
        sync_function_body, schedule, hour, sync_note
    )

    # 3. Build the manifest. A time trigger needs script.scriptapp (derived
    #    from the triggers plan), and CalendarApp needs the full calendar
    #    scope (supplied via oauth_scopes). BOTH land in the GENERATED
    #    manifest only - never in appscriptly's own consent (the
    #    load-bearing verify-LAST guarantee).
    manifest_dict = _build_manifest(
        {
            "triggers": [{"type": "time", "schedule": schedule}],
            # add_mail_scope adds script.send_mail so the injected failure
            # reporter can email the owner if a scheduled sync throws (gap
            # #5); GENERATED manifest only, never appscriptly's consent.
            "oauth_scopes": _add_mail_scope([_CALENDAR_SCOPE]),
        }
    )

    # 4. Default the project name from the schedule when not supplied.
    project_name = name or f"appscriptly calendar sync ({schedule})"

    # 5. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=sheet_id), push the body + manifest, cut a
    #    version + deploy. (We bind directly to the Sheet ID; no Drive
    #    mimeType round-trip - this tool only ever targets a Sheet.)
    result = _mint_bound_automation(
        creds,
        tool="as_install_calendar_sync",
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
                f"authorization prompt (it includes the `calendar` scope the "
                f"sync needs). That activates the {schedule} schedule for "
                f"`{handler}`; it then runs on Google's clock with no "
                f"further action."
            ),
        ),
        # Transparency: the scope the GENERATED bound script declares to
        # write Calendar events. It is the bound script's manifest scope,
        # NOT a scope added to appscriptly's own OAuth consent.
        "manifest_scope": _CALENDAR_SCOPE,
    }
