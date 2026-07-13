"""``as_install_edit_trigger`` — reactive ``onEdit`` automation for Sheets.

ROADMAP_SPECS #8 (the reactive/event-trigger half). A *use-case* tool
layered on the PR-Δ7 bound-script generator primitive
(``as_generate_bound_script`` / ``services/apps_script/api.py``). Where
the primitive is generic ("here's a ``.gs`` body + manifest, deploy it
bound to this container") and ``sheet_dashboard`` encodes the *time*-driven
pattern, THIS tool encodes the *event*-driven one: install an
**installable ``onEdit`` trigger** bound to a Google Sheet that runs a
caller-supplied handler whenever a user edits the spreadsheet — validating
input, stamping an audit cell, mirroring a change, etc. — on Google's
infrastructure, with no Claude call in the loop after the install.

**Installable, not simple.** A *simple* trigger is just a function named
``onEdit(e)`` — it runs in a restricted, no-auth context and cannot call
services that need authorization. This tool installs an **installable**
trigger via ``ScriptApp.newTrigger(handler).forSpreadsheet(id).onEdit()
.create()``. Installable triggers run with the installing user's full
authorization, so the handler can do real work (write other ranges, call
other Google services). That power is exactly why an installable trigger
requires the ``script.scriptapp`` oauthScope in the generated script's
manifest (see "Manifest reality" below) — the same scope
``sheet_dashboard``'s time trigger needs.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py``:

  * ``build_manifest`` — to derive the manifest (we hand it the
    ``script.scriptapp`` scope via ``oauth_scopes`` since an installable
    ``onEdit`` trigger needs it; the trigger itself is wired in code, not
    declared in the manifest).
  * ``create_bound_project`` → ``set_project_content`` →
    ``create_deployment`` — the same create/push/deploy sequence the
    primitive orchestrates. We bind directly to the Sheet ID (no Drive
    mimeType round-trip — this tool only ever targets a Sheet).

This module's OWN contribution is the ``.gs`` *script-body synthesis* —
stitching the caller's ``handler_function_body`` together with a generated
``installTrigger()`` that wires a deduplicated ``onEdit`` trigger via
``ScriptApp.newTrigger(handler).forSpreadsheet(sheetId).onEdit().create()``
— plus the handler-name extraction and parameter validation. None of the
REST plumbing is duplicated.

**Manifest reality (inherited from #138).** ``onEdit`` triggers are NOT an
``appsscript.json`` field — they're created in code via
``ScriptApp.newTrigger(handler).forSpreadsheet(id).onEdit().create()``. The
manifest's only job for an installable trigger is to declare the
``script.scriptapp`` oauthScope so the generated code is authorized to
install it. We pass that scope to ``build_manifest`` via ``oauth_scopes``
(the manifest can't declare the trigger itself — that's the #138
manifest-reality finding).

**The trigger-activation caveat — read this (same as sheet_dashboard).**
An *installable* trigger only comes into existence when
``installTrigger()`` actually *runs*. Deploying the script does NOT auto-run
it (the deploy step publishes code; it doesn't execute functions), and the
Apps Script REST API has no endpoint to create an installable trigger
remotely. So this tool *wires* the trigger into the deployed script but
does NOT make it live. The return payload is HONEST: ``trigger_active`` is
``False`` and ``activation_required`` is ``True`` with the one-step
instruction (open the script editor → run ``installTrigger`` once →
authorize). After that single run the ``onEdit`` reaction fires forever on
every edit. We do NOT claim the automation is live on return.
"""
from __future__ import annotations

import re

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._observability import (
    add_mail_scope as _add_mail_scope,
    guard_name_for as _guard_name_for,
    guarded_delegator as _guarded_delegator,
    reporter_helper_source as _reporter_helper_source,
)
from appscriptly.services.apps_script.api import (
    build_manifest as _build_manifest,
    container_data_scope as _container_data_scope,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_EDIT_TRIGGER_OUTPUT_SCHEMA

# Imported for parity with the sibling apps_script tools (sheet_dashboard
# etc.); not used on the happy path (the @workspace_tool(creds=True)
# envelope injects creds and maps HttpError → ToolError). Kept top-level so
# a future error-path addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# An installable trigger created via ScriptApp.newTrigger(...).create()
# runs with the user's full authorization, so the generated script must
# declare this oauthScope in its manifest. Same scope sheet_dashboard's
# time trigger needs (see api.py's _TRIGGER_SCOPE). We pass it to
# build_manifest via oauth_scopes (an onEdit trigger is wired in code; the
# manifest only carries the scope).
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"


def _extract_handler_name(handler_function_body: str) -> str:
    """Pull the function name out of a ``function NAME(...) {...}`` body.

    The caller supplies ``handler_function_body`` as an Apps Script
    function declaration (e.g. ``function onSheetEdit(e) { ... }``). The
    generated ``installTrigger()`` must reference that function by name in
    ``ScriptApp.newTrigger(NAME)``, so we parse the declared name out of
    the body.

    PURE — no I/O, deterministic. Matches the FIRST top-level
    ``function <name>(`` declaration (Apps Script trigger handlers are
    always named function declarations, never arrow functions, since
    ``ScriptApp.newTrigger`` takes the handler name as a string).

    Args:
        handler_function_body: the ``.gs`` source for the edit handler —
            a named ``function`` declaration.

    Returns:
        The handler function's name (e.g. ``"onSheetEdit"``).

    Raises:
        ValueError: no named ``function`` declaration found. An arrow
            function or a bare expression can't be a trigger handler
            (``ScriptApp.newTrigger`` needs a name), so we reject early
            with a message that shows the expected shape.
    """
    match = re.search(
        r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(",
        handler_function_body,
    )
    if match is None:
        raise ValueError(
            "handler_function_body must be a NAMED function declaration "
            "(e.g. `function onSheetEdit(e) { ... }`) — its name is used "
            "as the onEdit-trigger handler in "
            "`ScriptApp.newTrigger(\"<name>\")`. Arrow functions and bare "
            "expressions can't be trigger handlers. Got a body with no "
            "`function <name>(` declaration."
        )
    return match.group(1)


def build_edit_trigger_script_body(
    handler_function_body: str,
    sheet_id: str,
    handler_note: str | None = None,
) -> tuple[str, str]:
    """Synthesize the full ``.gs`` body for an installable ``onEdit`` trigger.

    PURE — assembles, from the caller's ``handler_function_body``, a
    complete script body containing:

      1. an optional banner comment (``handler_note``);
      2. the caller's handler function verbatim (the work that runs on
         each edit — it receives the standard Apps Script edit event
         object ``e``);
      3. a generated ``installTrigger()`` that (a) DELETES any existing
         project triggers whose handler is this function — so re-running
         ``installTrigger`` doesn't stack duplicate triggers (the classic
         Apps Script footgun) — then (b) creates the ``onEdit`` trigger for
         that handler bound to THIS spreadsheet via
         ``ScriptApp.newTrigger(handler).forSpreadsheet(sheetId).onEdit()
         .create()``.

    The dedup-then-create shape is idempotent at the trigger level:
    running ``installTrigger`` N times leaves exactly ONE onEdit trigger
    for the handler, every time.

    Args:
        handler_function_body: the caller's named ``function`` declaration
            (the edit-reaction work). Must declare the handler by name.
        sheet_id: the Drive ID of the spreadsheet the trigger binds to —
            embedded as a JS string literal in the ``forSpreadsheet(...)``
            call so the source can't be broken out of / injected into.
        handler_note: optional human note rendered as a leading comment in
            the generated script (documents intent in the editor).

    Returns:
        ``(script_body, handler_name)`` — the assembled ``.gs`` source and
        the parsed handler function name (echoed so the tool can report
        which function the trigger drives + which one the user runs to
        activate).

    Raises:
        ValueError: ``handler_function_body`` has no named ``function``
            declaration (from ``_extract_handler_name``).
    """
    handler = _extract_handler_name(handler_function_body)
    # The trigger targets a guarded wrapper (not the caller's handler
    # directly) so a failure on an edit that fires while no one is watching
    # is emailed to the owner and then rethrown, instead of only landing in
    # the execution log (gap #5). The caller's handler stays verbatim.
    guard_src, guard_name = _guarded_delegator(handler)

    note_comment = ""
    if handler_note:
        # Keep the note on comment lines so an embedded newline / */ can't
        # break out of the source. Escape any close-comment sequence and
        # prefix every line with `// `.
        safe = handler_note.replace("*/", "* /")
        note_lines = "\n".join(f"// {ln}" for ln in safe.splitlines())
        note_comment = (
            "// ---- appscriptly reactive onEdit trigger ----\n"
            f"{note_lines}\n\n"
        )

    # Embed the sheet ID as a JS string literal (json.dumps-equivalent for
    # a plain ID is just quoting; Drive IDs are [A-Za-z0-9_-] so quoting is
    # safe and unambiguous). We pass the ID to forSpreadsheet so the
    # trigger is explicitly tied to this spreadsheet even though the script
    # is already container-bound.
    sheet_literal = '"' + sheet_id.replace("\\", "\\\\").replace('"', '\\"') + '"'

    install_trigger = f"""\
/**
 * Installs the installable onEdit trigger that runs {handler}(e) whenever
 * this spreadsheet is edited. Run this ONCE to activate the automation
 * (the deploy wires the code but does not run it). Re-running is safe: it
 * removes any prior trigger for this handler before creating a new one, so
 * triggers never stack. The trigger targets a guarded wrapper that emails
 * you if {handler} throws, then rethrows.
 */
function installTrigger() {{
  var handlerName = "{guard_name}";
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {{
    if (existing[i].getHandlerFunction() === handlerName) {{
      ScriptApp.deleteTrigger(existing[i]);
    }}
  }}
  ScriptApp.newTrigger(handlerName)
    .forSpreadsheet({sheet_literal})
    .onEdit()
    .create();
}}
"""

    body = (
        f"{note_comment}"
        f"{handler_function_body.rstrip()}\n\n"
        f"{guard_src}\n\n"
        f"{_reporter_helper_source().rstrip()}\n\n"
        f"{install_trigger}"
    )
    return body, handler


@workspace_tool(
    title="Install a reactive onEdit trigger into a Google Sheet",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the Sheet —
    # re-running produces a duplicate bound script. NOT idempotent (same
    # convention as as_generate_bound_script / as_install_sheet_dashboard).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_EDIT_TRIGGER_OUTPUT_SCHEMA,
)
def as_install_edit_trigger(
    creds,
    sheet_id: str,
    handler_function_body: str,
    handler_note: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a reactive ``onEdit`` automation into a Google Sheet.

    Deploys a *bound* Apps Script into the Sheet that runs your
    ``handler_function_body`` whenever the spreadsheet is edited, via an
    **installable** ``onEdit`` trigger
    (``ScriptApp.newTrigger(handler).forSpreadsheet(id).onEdit().create()``).
    The handler receives the standard Apps Script edit event object ``e``
    (``e.range``, ``e.value``, ``e.oldValue``, ``e.source``, …) so it can
    react to exactly what changed — validate the edit, stamp a
    last-modified cell, mirror the value elsewhere, etc. Once activated it
    runs on Google's infrastructure with NO Claude call in the loop. This
    composes the generic bound-script primitive
    (``as_generate_bound_script``) for one concrete, common pattern.

    USE WHEN: the user wants a Sheet to REACT to edits automatically —
    "timestamp column A whenever a row changes", "validate that the status
    cell is one of the allowed values", "log every edit to an audit tab".
    For a ONE-OFF write, just use ``gsheets_write_range`` (no script
    needed). For a SCHEDULED (time-driven) automation use
    ``as_install_sheet_dashboard``; for a custom ``=FUNCTION()`` use
    ``as_install_custom_function``.

    INSTALLABLE vs SIMPLE trigger: this installs an *installable* trigger,
    which runs with your full authorization — so the handler can call other
    Google services and write other ranges. (A *simple* ``onEdit`` — just a
    function named ``onEdit`` — runs in a restricted no-auth context and
    can't do that.) The power of an installable trigger is why the
    generated script's manifest declares the ``script.scriptapp`` scope.

    IMPORTANT — activation is a required one-time step (same as
    ``as_install_sheet_dashboard``). An *installable* trigger only exists
    once its installer runs, and deploying a script does NOT run it (and the
    Apps Script REST API can't create the trigger remotely). So this tool
    WIRES the trigger into the deployed script but the reaction is NOT live
    yet on return. To activate: open the returned ``project_url``, run the
    ``installTrigger`` function once (the editor's Run button), and approve
    the authorization prompt. After that single run the ``onEdit`` reaction
    fires on every edit. The return payload says so explicitly —
    ``trigger_active`` is ``False`` and ``activation_required`` is ``True``
    with the step spelled out. Do not tell the user their Sheet is already
    reacting to edits until they've run ``installTrigger`` once.

    Args:
        sheet_id: Drive ID of the Google Sheet to install the automation
            into (the ID part of the Sheet's URL). The bound script + the
            ``onEdit`` trigger are attached to THIS Sheet.
        handler_function_body: the ``.gs`` source for the edit handler as a
            NAMED function declaration, e.g.
            ``"function onSheetEdit(e) { /* react to e.range */ }"``. Claude
            authors this — it's the work that runs on each edit. Its
            declared name becomes the trigger handler. Required; empty /
            unnamed bodies are rejected. The function should accept the
            edit event parameter (conventionally ``e``).
        handler_note: OPTIONAL human note rendered as a leading comment in
            the generated script (documents the reaction's intent for
            anyone who opens the editor). Does not affect behavior.
        name: OPTIONAL title for the new Apps Script project. Defaults to a
            generated edit-trigger name.
        on_conflict: what to do when an onEdit automation from THIS tool
            already exists on this Sheet. "new" (the default) always
            installs a fresh one (which can leave duplicate reactions);
            "replace" uninstalls the prior install(s) on this Sheet first
            (no duplicate, no orphan); "skip" returns the existing install
            unchanged instead of adding a duplicate. Keyed by (this tool,
            this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, sheet_id, trigger_type,
        trigger_handler, project_url, trigger_active, activation_required,
        activation_instructions}``. ``trigger_type`` is ``"onEdit"``.
        ``trigger_handler`` is the parsed handler-function name the trigger
        drives. ``project_url`` deep-links to the script editor.
        ``trigger_active`` is ``False`` and ``activation_required`` is
        ``True`` on a successful deploy — the trigger is wired but needs a
        one-time ``installTrigger`` run (see the activation note above);
        ``activation_instructions`` is the literal step.

    Raises:
        ToolError: an empty / unnamed ``handler_function_body`` (rejected
            before any API call), or any Apps Script / Drive API error —
            the standard decorator envelope renders these as user-facing
            ``ToolError``.

    Choreography: get ``sheet_id`` from the user's URL, from a prior
    ``gsheets_create_spreadsheet`` call, or from
    ``gdocs_find_doc_by_title``. After this returns, point the user at
    ``project_url`` to run ``installTrigger`` once — that's the only manual
    step. (The Apps Script scopes are in the baseline grant, so most users
    won't see a second OAuth consent for the deploy itself; the in-editor
    ``installTrigger`` run has its own one-time authorization prompt for the
    trigger scope.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call. These
    #    ValueErrors propagate as ToolError via the decorator envelope.
    if not sheet_id or not sheet_id.strip():
        raise ValueError(
            "sheet_id cannot be empty — pass the Drive ID of the Google "
            "Sheet to install the onEdit trigger into."
        )
    if not handler_function_body or not handler_function_body.strip():
        raise ValueError(
            "handler_function_body cannot be empty — pass the .gs source "
            "for the edit-reaction work as a named function declaration "
            "(e.g. `function onSheetEdit(e) { ... }`)."
        )

    # 2. Synthesize the full .gs body (handler fn + dedup'd installTrigger
    #    wiring the onEdit trigger). _extract_handler_name (inside) also
    #    rejects an unnamed function.
    script_body, handler = build_edit_trigger_script_body(
        handler_function_body, sheet_id, handler_note
    )

    # 3. Build the manifest. An installable onEdit trigger needs the
    #    script.scriptapp oauthScope; we get it by handing build_manifest
    #    that scope via oauth_scopes (the manifest can't declare the
    #    trigger itself — the #138 manifest-reality finding — but it
    #    carries the scope). We also echo the edit-trigger intent under the
    #    plan via the triggers key (type "edit").
    manifest_dict = _build_manifest(
        {
            "triggers": [{"type": "edit"}],
            # _TRIGGER_SCOPE (script.scriptapp) for the installable trigger;
            # container_data_scope("sheets") = spreadsheets.currentonly so the
            # onEdit handler can touch THIS Sheet (an explicit oauthScopes
            # block suppresses auto-detection - N-S3V-1); add_mail_scope adds
            # the failure reporter's send scope. GENERATED manifest only,
            # never appscriptly's consent.
            "oauth_scopes": _add_mail_scope(
                [_TRIGGER_SCOPE, _container_data_scope("sheets")]
            ),
        }
    )

    # 4. Default the project name when not supplied.
    project_name = name or "appscriptly onEdit trigger"

    # 5. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=sheet_id), push the body + manifest, cut a
    #    version + deploy. (We bind directly to the Sheet ID; no Drive
    #    mimeType round-trip — this tool only ever targets a Sheet.)
    result = _mint_bound_automation(
        creds,
        tool="as_install_edit_trigger",
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
        "trigger_type": "onEdit",
        "trigger_handler": handler,
        "project_url": f"https://script.google.com/d/{script_id}/edit",
        # HONEST trigger state: the deploy wires the trigger but does NOT
        # run installTrigger, so the reaction is not live yet. trigger_active
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
                f"authorization prompt. That activates the onEdit reaction "
                f"for `{handler}`; it then runs on Google's infrastructure "
                f"on every edit with no further action."
            ),
        ),
    }
