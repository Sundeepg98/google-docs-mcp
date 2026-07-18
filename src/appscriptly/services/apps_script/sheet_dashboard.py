"""``as_install_sheet_dashboard`` — scheduled dashboard refresh for Sheets.

PR-Δ9. A *use-case* tool layered on top of the PR-Δ7 bound-script
generator primitive (``as_generate_bound_script`` /
``services/apps_script/api.py``). Where the primitive is generic ("here's
a ``.gs`` body + manifest, deploy it bound to this container"), THIS tool
encodes one concrete, high-value pattern: install a time-driven
automation into a Google Sheet that re-runs a caller-supplied refresh
function on a schedule (daily / hourly / weekly) — refreshing a dashboard
tab, recomputing summaries, re-pulling external data, etc. — on Google's
clock, with no Claude call in the loop after the install.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py``:

  * ``build_manifest`` — to derive the ``script.scriptapp`` oauthScope
    (an installable time trigger needs it; see the manifest-reality note
    below).
  * ``create_bound_project`` → ``set_project_content`` →
    ``create_deployment`` — the same create/push/deploy sequence
    ``as_generate_bound_script`` orchestrates.

This module's OWN contribution is the ``.gs`` *script-body synthesis* —
stitching the caller's ``refresh_function_body`` together with a
generated ``installTrigger()`` that wires a deduplicated, schedule-mapped
time trigger — plus the schedule→trigger-builder mapping and the
parameter validation. None of the REST plumbing is duplicated.

**Manifest reality (inherited from #138).** Time triggers are NOT an
``appsscript.json`` field — they're created in code via
``ScriptApp.newTrigger(handler).timeBased()...create()``. The manifest's
only job for a time trigger is to declare the ``script.scriptapp``
oauthScope so the generated code is authorized to install it. We get that
for free by handing ``build_manifest`` a ``triggers: [{"type": "time"}]``
plan — it derives the scope and we don't touch the manifest schema
directly.

**The trigger-activation caveat — read this.** An *installable* time
trigger only comes into existence when ``installTrigger()`` actually
*runs*. Deploying the script does NOT auto-run it (the Apps Script API's
deploy step publishes code; it doesn't execute functions). The Apps
Script REST API also has no endpoint to create an installable trigger
programmatically (``scripts.run`` requires the script to be set up as an
API-executable tied to a standard GCP project + an extra scope — out of
scope for, and unreliable from, this tool). So this tool *wires* the
trigger into the deployed script but does NOT make it live. The return
payload is HONEST about this: ``trigger_active`` is ``False`` and
``activation_required`` is ``True`` with the one-step instruction (open
the script editor → run ``installTrigger`` once → authorize). After that
single run, the schedule fires forever on Google's infrastructure. We do
NOT claim the automation is live on return — that would be a lie that
sends the user away thinking their dashboard refreshes when it doesn't.
"""
from __future__ import annotations

import re

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._observability import (
    guarded_delegator as _guarded_delegator,
    reporter_helper_source as _reporter_helper_source,
)
from appscriptly.services.apps_script._recipes import (
    RECIPES as _RECIPES,
    render as _render,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA

# Imported for parity with the other apps_script tools.py / the sheets
# tools.py; not used on the happy path (the @workspace_tool(creds=True)
# envelope injects creds and maps HttpError → ToolError). Kept top-level
# so a future error-path addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# The container kind is fixed for this tool — it installs into a Google
# Sheet. We pass it explicitly to create_bound_project's downstream so we
# never pay the Drive mimeType-detection round-trip (and so a non-Sheet
# ID surfaces as a clear API error on bind rather than a silent no-op
# dashboard). The primitive auto-detects; this use-case tool knows.
_SHEETS_KIND = "sheets"

# schedule → human description of when it fires. Used only for the
# return payload / docstring clarity; the actual builder is in
# _trigger_builder_expr.
VALID_SCHEDULES: frozenset[str] = frozenset({"daily", "hourly", "weekly"})

# The WeekDay a "weekly" schedule fires on. Monday is the conventional
# start-of-week dashboard refresh; kept as a constant so the generated
# code + the tests reference one source of truth.
_WEEKLY_DAY = "MONDAY"


def _extract_handler_name(refresh_function_body: str) -> str:
    """Pull the function name out of a ``function NAME(...) {...}`` body.

    The caller supplies ``refresh_function_body`` as a JavaScript/Apps
    Script function declaration (e.g. ``function refreshDashboard() {
    ... }``). The generated ``installTrigger()`` must reference that
    function by name in ``ScriptApp.newTrigger(NAME)``, so we parse the
    declared name out of the body.

    PURE — no I/O, deterministic. Matches the FIRST top-level
    ``function <name>(`` declaration (Apps Script trigger handlers are
    always named function declarations, never arrow functions, since
    ``ScriptApp.newTrigger`` takes the handler name as a string).

    Args:
        refresh_function_body: the ``.gs`` source for the refresh
            function — a named ``function`` declaration.

    Returns:
        The handler function's name (e.g. ``"refreshDashboard"``).

    Raises:
        ValueError: no named ``function`` declaration found. An arrow
            function or a bare expression can't be a trigger handler
            (``ScriptApp.newTrigger`` needs a name), so we reject early
            with a message that shows the expected shape.
    """
    # JS identifiers: start with a letter / _ / $, then word chars / $.
    # We match the first `function <name>(` allowing for `async` is not
    # valid for Apps Script trigger handlers, so a plain `function`.
    match = re.search(
        r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(",
        refresh_function_body,
    )
    if match is None:
        raise ValueError(
            "refresh_function_body must be a NAMED function declaration "
            "(e.g. `function refreshDashboard() { ... }`) — its name is "
            "used as the time-trigger handler in "
            "`ScriptApp.newTrigger(\"<name>\")`. Arrow functions and bare "
            "expressions can't be trigger handlers. Got a body with no "
            "`function <name>(` declaration."
        )
    return match.group(1)


def _trigger_builder_expr(schedule: str, hour: int) -> str:
    """Map a ``schedule`` (+ ``hour``) to the Apps Script trigger-builder tail.

    PURE — returns the ``.timeBased()...`` method-chain suffix that turns
    a ``ScriptApp.newTrigger(handler)`` into the right recurring time
    trigger. Validation of ``schedule`` / ``hour`` happens in the tool
    body (so the ValueError → caller mapping is uniform); this helper
    trusts its inputs but is defensive enough to raise on an unknown
    schedule (belt-and-suspenders for direct unit-test callers).

    Mapping:

      * ``daily``  → ``.timeBased().everyDays(1).atHour(<hour>).create()``
      * ``hourly`` → ``.timeBased().everyHours(1).create()`` (``hour`` is
        ignored — an hourly trigger has no single hour-of-day)
      * ``weekly`` → ``.timeBased().onWeekDay(ScriptApp.WeekDay.MONDAY)
        .atHour(<hour>).create()``

    Args:
        schedule: one of ``VALID_SCHEDULES``.
        hour: hour-of-day 0-23 (applied to daily / weekly; ignored for
            hourly).

    Returns:
        The trigger-builder method-chain suffix as a string, beginning
        with ``.timeBased()`` and ending with ``.create();``.

    Raises:
        ValueError: ``schedule`` is not one of ``VALID_SCHEDULES``.
    """
    if schedule == "daily":
        return f".timeBased().everyDays(1).atHour({hour}).create();"
    if schedule == "hourly":
        # Hourly triggers fire every hour; atHour() is meaningless here.
        return ".timeBased().everyHours(1).create();"
    if schedule == "weekly":
        return (
            f".timeBased().onWeekDay(ScriptApp.WeekDay.{_WEEKLY_DAY})"
            f".atHour({hour}).create();"
        )
    raise ValueError(
        f"schedule must be one of {sorted(VALID_SCHEDULES)}, got "
        f"{schedule!r}."
    )


def build_dashboard_script_body(
    refresh_function_body: str,
    schedule: str,
    hour: int,
    dashboard_note: str | None = None,
) -> tuple[str, str]:
    """Synthesize the full ``.gs`` body for a scheduled dashboard refresh.

    PURE — assembles, from the caller's ``refresh_function_body``, a
    complete script body containing:

      1. an optional banner comment (``dashboard_note``);
      2. the caller's refresh function verbatim (the work that runs on
         each tick — e.g. rebuilding a dashboard tab);
      3. a generated ``installTrigger()`` that (a) DELETES any existing
         project triggers whose handler is this refresh function — so
         re-running ``installTrigger`` doesn't stack duplicate triggers
         (the classic Apps Script footgun) — then (b) creates the
         schedule-mapped time trigger for that handler.

    The dedup-then-create shape is idempotent at the trigger level:
    running ``installTrigger`` N times leaves exactly ONE trigger for the
    handler, every time.

    Args:
        refresh_function_body: the caller's named ``function`` declaration
            (the refresh work). Must declare the handler by name.
        schedule: one of ``VALID_SCHEDULES`` (validated by the caller).
        hour: hour-of-day 0-23 (validated by the caller).
        dashboard_note: optional human note rendered as a leading comment
            in the generated script (documents intent in the editor).

    Returns:
        ``(script_body, handler_name)`` — the assembled ``.gs`` source and
        the parsed handler function name (echoed so the tool can report
        which function the trigger drives + which one the user runs to
        activate).

    Raises:
        ValueError: ``refresh_function_body`` has no named ``function``
            declaration (from ``_extract_handler_name``), or ``schedule``
            is unknown (from ``_trigger_builder_expr``).
    """
    handler = _extract_handler_name(refresh_function_body)
    builder_tail = _trigger_builder_expr(schedule, hour)
    # The trigger targets a guarded wrapper (not the caller's handler
    # directly) so an unattended failure is emailed to the owner and then
    # rethrown, instead of only landing in the execution log (gap #5). The
    # caller's handler function stays verbatim.
    guard_src, guard_name = _guarded_delegator(handler)

    note_comment = ""
    if dashboard_note:
        # Keep the note on comment lines so an embedded newline / */ can't
        # break out of the source. Escape any close-comment sequence and
        # prefix every line with `// `.
        safe = dashboard_note.replace("*/", "* /")
        note_lines = "\n".join(f"// {ln}" for ln in safe.splitlines())
        note_comment = (
            "// ---- appscriptly scheduled dashboard refresh ----\n"
            f"{note_lines}\n\n"
        )

    # installTrigger(): dedup existing handlers, then install the new one.
    # ScriptApp.getProjectTriggers() lists installable triggers; we delete
    # the ones whose handler function matches OUR guarded wrapper so a
    # re-run is idempotent (one trigger, not a growing stack).
    install_trigger = f"""\
/**
 * Installs the time-driven trigger that runs {handler}() on the
 * configured schedule ({schedule}). Run this ONCE to activate the
 * automation (the deploy wires the code but does not run it). Re-running
 * is safe: it removes any prior trigger for this handler before creating
 * a new one, so triggers never stack. The trigger targets a guarded
 * wrapper that emails you if {handler} throws, then rethrows.
 */
function installTrigger() {{
  var handlerName = "{guard_name}";
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {{
    if (existing[i].getHandlerFunction() === handlerName) {{
      ScriptApp.deleteTrigger(existing[i]);
    }}
  }}
  ScriptApp.newTrigger(handlerName){builder_tail}
}}
"""

    body = (
        f"{note_comment}"
        f"{refresh_function_body.rstrip()}\n\n"
        f"{guard_src}\n\n"
        f"{_reporter_helper_source().rstrip()}\n\n"
        f"{install_trigger}"
    )
    return body, handler


@workspace_tool(
    title="Install a scheduled dashboard refresh into a Google Sheet",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the Sheet —
    # re-running produces a duplicate bound script. NOT idempotent (same
    # convention as as_generate_bound_script / gsheets_create_spreadsheet).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA,
)
def as_install_sheet_dashboard(
    creds,
    sheet_id: str,
    refresh_function_body: str,
    schedule: str = "daily",
    hour: int = 6,
    dashboard_note: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a time-driven dashboard-refresh automation into a Google Sheet.

    Deploys a *bound* Apps Script into the Sheet that runs your
    ``refresh_function_body`` on a recurring schedule (daily / hourly /
    weekly) via an installable time trigger — re-building a dashboard tab,
    recomputing summary cells, re-pulling external data, etc. Once
    activated it runs on Google's clock with NO Claude call in the loop.
    This composes the generic bound-script primitive
    (``as_generate_bound_script``) for one concrete, common pattern.

    USE WHEN: the user wants a Sheet to keep itself up to date on a
    schedule — "refresh my KPI dashboard every morning", "recompute the
    rollup tab hourly", "rebuild the weekly report every Monday". For a
    ONE-OFF recompute, just write the cells directly with
    ``gsheets_write_range`` (no script needed). For a non-time automation
    (a custom menu, an onEdit reaction) use ``as_generate_bound_script``
    directly with the appropriate manifest.

    IMPORTANT — activation is a required one-time step. An *installable*
    time trigger only exists once its installer runs, and deploying a
    script does NOT run it (and the Apps Script REST API can't create the
    trigger remotely). So this tool WIRES the trigger into the deployed
    script but the schedule is NOT live yet on return. To activate: open
    the returned ``project_url``, run the ``installTrigger`` function once
    (the editor's Run button), and approve the authorization prompt.
    After that single run the schedule fires forever. The return payload
    says so explicitly — ``trigger_active`` is ``False`` and
    ``activation_required`` is ``True`` with the step spelled out. Do not
    tell the user their dashboard is already refreshing until they've run
    ``installTrigger`` once. (This is honest by design — the bound script
    is deployed and correct; only the trigger needs that one manual run.)

    Args:
        sheet_id: Drive ID of the Google Sheet to install the automation
            into (the ID part of the Sheet's URL). The bound script is
            attached to THIS Sheet.
        refresh_function_body: the ``.gs`` source for the refresh
            function as a NAMED function declaration, e.g.
            ``"function refreshDashboard() { /* rebuild the tab */ }"``.
            Claude authors this — it's the work that runs on each tick.
            Its declared name becomes the trigger handler. Required;
            empty / unnamed bodies are rejected.
        schedule: how often the refresh runs — ``"daily"`` (default),
            ``"hourly"``, or ``"weekly"``. ``daily``/``weekly`` fire at
            ``hour``; ``weekly`` fires on Monday; ``hourly`` fires every
            hour (``hour`` ignored).
        hour: hour-of-day (0-23, server/script time zone) the
            daily/weekly trigger fires. Default ``6`` (early-morning
            refresh). Ignored for ``hourly``.
        dashboard_note: OPTIONAL human note rendered as a leading comment
            in the generated script (documents the dashboard's intent for
            anyone who opens the editor). Does not affect behavior.
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated dashboard-automation name.
        on_conflict: what to do when a dashboard automation from THIS tool
            already exists on this Sheet. "new" (the default) always
            installs a fresh one (which can leave duplicate schedules);
            "replace" uninstalls the prior install(s) on this Sheet first
            (no duplicate, no orphan); "skip" returns the existing install
            unchanged instead of adding a duplicate. The match is keyed by
            (this tool, this container) via appscriptly's automation ledger.
            The response echoes ``on_conflict`` and adds ``reused_existing``
            / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, sheet_id, schedule, trigger_handler,
        project_url, trigger_active, activation_required,
        activation_instructions}``. ``trigger_handler`` is the parsed
        refresh-function name the trigger drives. ``project_url``
        deep-links to the script editor. ``trigger_active`` is ``False``
        and ``activation_required`` is ``True`` on a successful deploy —
        the trigger is wired but needs a one-time ``installTrigger`` run
        (see the activation note above); ``activation_instructions`` is
        the literal step.

    Raises:
        ToolError: an invalid ``schedule`` / ``hour`` / unnamed
            ``refresh_function_body`` (rejected before any API call), or
            any Apps Script / Drive API error — the standard decorator
            envelope renders these as user-facing ``ToolError``.

    Choreography: get ``sheet_id`` from the user's URL, from a prior
    ``gsheets_create_spreadsheet`` call, or from
    ``gdocs_find_doc_by_title``. After this returns, point the user at
    ``project_url`` to run ``installTrigger`` once — that's the only
    manual step. (The Apps Script scopes are in the baseline grant, so
    most users won't see a second OAuth consent for the deploy itself;
    the in-editor ``installTrigger`` run has its own one-time
    authorization prompt for the trigger scope.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call. These
    #    ValueErrors propagate as ToolError via the decorator envelope.
    if schedule not in VALID_SCHEDULES:
        raise ValueError(
            f"schedule must be one of {sorted(VALID_SCHEDULES)} "
            f"('daily' / 'hourly' / 'weekly'), got {schedule!r}."
        )
    if not isinstance(hour, int) or isinstance(hour, bool) or not (0 <= hour <= 23):
        raise ValueError(
            f"hour must be an integer 0-23 (hour-of-day for the daily/"
            f"weekly trigger), got {hour!r}."
        )
    if not refresh_function_body or not refresh_function_body.strip():
        raise ValueError(
            "refresh_function_body cannot be empty — pass the .gs source "
            "for the refresh work as a named function declaration (e.g. "
            "`function refreshDashboard() { ... }`)."
        )

    # 2. Codegen via the recipe registry (_recipes.py) — the SINGLE source
    #    for this tool's .gs body + manifest + recorded handler. render() runs
    #    the same build_dashboard_script_body and threads the same
    #    time-trigger manifest plan (script.scriptapp derived from the trigger
    #    + container_data_scope("sheets") + add_mail_scope for the failure
    #    reporter); the byte-identity pins guarantee the output is unchanged.
    #    We still parse the RAW handler name here for the return payload +
    #    activation instructions — render records the GUARD-wrapped name in
    #    handler_functions (the ledger target), not the display name.
    #    _extract_handler_name rejects an unnamed function, exactly as before
    #    (render's handler_derivation parses the same regex).
    handler = _extract_handler_name(refresh_function_body)
    spec = _RECIPES["as_install_sheet_dashboard"]
    params = {
        "sheet_id": sheet_id,
        "refresh_function_body": refresh_function_body,
        "schedule": schedule,
        "hour": hour,
        "dashboard_note": dashboard_note,
        "name": name,
    }
    rendered = _render(spec, params)
    project_name = spec.project_name(params)

    # 3. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=sheet_id), push the body + manifest, cut
    #    a version + deploy. (We bind directly to the Sheet ID; no Drive
    #    mimeType round-trip — this tool only ever targets a Sheet.)
    result = _mint_bound_automation(
        creds,
        tool=spec.name,
        container_id=sheet_id,
        container_kind=spec.container_kind,
        project_name=project_name,
        script_body=rendered.script_body,
        manifest_dict=rendered.manifest,
        on_conflict=on_conflict,
        # render's handler_derivation records the GUARD wrapper name (what
        # installTrigger actually targets), matching the prior inline
        # [_guard_name_for(handler)]: uninstall's self-disarm reaper
        # redefines exactly this name, so the ledger value must equal the
        # wired trigger target.
        handler_functions=rendered.handler_functions,
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
                f"authorization prompt. That activates the {schedule} "
                f"schedule for `{handler}`; it then runs on Google's clock "
                f"with no further action."
            ),
        ),
    }
