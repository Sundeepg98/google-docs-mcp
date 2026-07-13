"""``as_install_form_handler`` — reactive ``onFormSubmit`` automation for Forms.

ROADMAP_SPECS #8 (the reactive/event-trigger half — the Forms path). A
*use-case* tool layered on the PR-Δ7 bound-script generator primitive
(``as_generate_bound_script`` / ``services/apps_script/api.py``). It
installs an **installable ``onFormSubmit`` trigger** bound to a Google Form
that runs a caller-supplied handler whenever the form is submitted —
routing the response, sending a confirmation, writing to a tracker, etc. —
on Google's infrastructure, with no Claude call in the loop after the
install.

**Lifting the Forms hard-rejection — read this.** The generic primitive's
``auto_detect_container_kind`` (api.py) deliberately REJECTS Forms: its
menu/sidebar/onEdit surfaces are meaningless on a Form (a Form has no
``Ui`` menu and is never *edited* like a Sheet). But a Form has exactly ONE
reactive surface that DOES make sense — the **form-submit** trigger
(``ScriptApp.newTrigger(fn).forForm(id).onFormSubmit().create()``). THIS
tool is the Forms-specialized composition that unlocks that one surface. It
does so the SAME way ``sheet_dashboard`` / ``doc_menu`` bypass auto-detect:
it binds DIRECTLY to the Form ID (``create_bound_project(creds, form_id,
...)``) without calling ``auto_detect_container_kind`` at all. So the Forms
rejection is "lifted" only on this purpose-built path; the generic
primitive's ``auto_detect_container_kind`` stays Forms-rejecting for every
other (menu/sidebar/edit) use, where a Form genuinely doesn't fit.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py``:

  * ``build_manifest`` — to derive the manifest (we hand it the
    ``script.scriptapp`` scope via ``oauth_scopes`` since an installable
    ``onFormSubmit`` trigger needs it; the trigger is wired in code, not
    declared in the manifest).
  * ``create_bound_project`` → ``set_project_content`` →
    ``create_deployment`` — the same create/push/deploy sequence the
    primitive orchestrates. We bind directly to the Form ID (no Drive
    mimeType round-trip — and crucially, no ``auto_detect_container_kind``,
    which would reject the Form).

This module's OWN contribution is the ``.gs`` *script-body synthesis* —
stitching the caller's ``handler_function_body`` together with a generated
``installTrigger()`` that wires a deduplicated ``onFormSubmit`` trigger via
``ScriptApp.newTrigger(handler).forForm(formId).onFormSubmit().create()`` —
plus the handler-name extraction and parameter validation.

**Manifest reality (inherited from #138).** ``onFormSubmit`` triggers are
NOT an ``appsscript.json`` field — they're created in code. The manifest's
only job for an installable trigger is to declare the ``script.scriptapp``
oauthScope so the generated code is authorized to install it. We pass that
scope to ``build_manifest`` via ``oauth_scopes``.

**The trigger-activation caveat — read this (same as sheet_dashboard /
edit_trigger).** An *installable* trigger only comes into existence when
``installTrigger()`` actually *runs*. Deploying the script does NOT auto-run
it, and the Apps Script REST API has no endpoint to create an installable
trigger remotely. So this tool *wires* the trigger into the deployed script
but does NOT make it live. The return payload is HONEST: ``trigger_active``
is ``False`` and ``activation_required`` is ``True`` with the one-step
instruction (open the script editor → run ``installTrigger`` once →
authorize). After that single run the handler fires on every submission. We
do NOT claim the automation is live on return.
"""
from __future__ import annotations

import re

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script.api import build_manifest as _build_manifest
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_FORM_HANDLER_OUTPUT_SCHEMA

# Imported for parity with the sibling apps_script tools; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# An installable trigger created via ScriptApp.newTrigger(...).create()
# runs with the user's full authorization, so the generated script must
# declare this oauthScope in its manifest. Same scope sheet_dashboard's
# time trigger + edit_trigger's onEdit trigger need (api.py's
# _TRIGGER_SCOPE). We pass it to build_manifest via oauth_scopes (the
# onFormSubmit trigger is wired in code; the manifest only carries it).
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"


def _extract_handler_name(handler_function_body: str) -> str:
    """Pull the function name out of a ``function NAME(...) {...}`` body.

    The caller supplies ``handler_function_body`` as an Apps Script
    function declaration (e.g. ``function onSubmit(e) { ... }``). The
    generated ``installTrigger()`` must reference that function by name in
    ``ScriptApp.newTrigger(NAME)``, so we parse the declared name out of
    the body.

    PURE — no I/O, deterministic. Matches the FIRST top-level
    ``function <name>(`` declaration (Apps Script trigger handlers are
    always named function declarations, never arrow functions, since
    ``ScriptApp.newTrigger`` takes the handler name as a string).

    Args:
        handler_function_body: the ``.gs`` source for the submit handler —
            a named ``function`` declaration.

    Returns:
        The handler function's name (e.g. ``"onSubmit"``).

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
            "(e.g. `function onSubmit(e) { ... }`) — its name is used as "
            "the onFormSubmit-trigger handler in "
            "`ScriptApp.newTrigger(\"<name>\")`. Arrow functions and bare "
            "expressions can't be trigger handlers. Got a body with no "
            "`function <name>(` declaration."
        )
    return match.group(1)


def build_form_handler_script_body(
    handler_function_body: str,
    form_id: str,
    handler_note: str | None = None,
) -> tuple[str, str]:
    """Synthesize the full ``.gs`` body for an installable ``onFormSubmit``.

    PURE — assembles, from the caller's ``handler_function_body``, a
    complete script body containing:

      1. an optional banner comment (``handler_note``);
      2. the caller's handler function verbatim (the work that runs on each
         submission — it receives the standard Apps Script form-submit
         event object ``e``, e.g. ``e.response`` for a Form-bound script);
      3. a generated ``installTrigger()`` that (a) DELETES any existing
         project triggers whose handler is this function — so re-running
         ``installTrigger`` doesn't stack duplicate triggers — then (b)
         creates the ``onFormSubmit`` trigger for that handler bound to THIS
         form via ``ScriptApp.newTrigger(handler).forForm(formId)
         .onFormSubmit().create()``.

    The dedup-then-create shape is idempotent at the trigger level:
    running ``installTrigger`` N times leaves exactly ONE onFormSubmit
    trigger for the handler, every time.

    Args:
        handler_function_body: the caller's named ``function`` declaration
            (the submit-reaction work). Must declare the handler by name.
        form_id: the Drive ID of the Form the trigger binds to — embedded
            as a JS string literal in the ``forForm(...)`` call so the
            source can't be broken out of / injected into.
        handler_note: optional human note rendered as a leading comment in
            the generated script (documents intent in the editor).

    Returns:
        ``(script_body, handler_name)`` — the assembled ``.gs`` source and
        the parsed handler function name.

    Raises:
        ValueError: ``handler_function_body`` has no named ``function``
            declaration (from ``_extract_handler_name``).
    """
    handler = _extract_handler_name(handler_function_body)

    note_comment = ""
    if handler_note:
        # Keep the note on comment lines so an embedded newline / */ can't
        # break out of the source. Escape any close-comment sequence and
        # prefix every line with `// `.
        safe = handler_note.replace("*/", "* /")
        note_lines = "\n".join(f"// {ln}" for ln in safe.splitlines())
        note_comment = (
            "// ---- appscriptly reactive onFormSubmit trigger ----\n"
            f"{note_lines}\n\n"
        )

    # Embed the form ID as a JS string literal. Drive IDs are [A-Za-z0-9_-]
    # so quoting is safe; we still escape backslash / quote defensively so
    # an unexpected ID can't break out of the literal.
    form_literal = '"' + form_id.replace("\\", "\\\\").replace('"', '\\"') + '"'

    install_trigger = f"""\
/**
 * Installs the installable onFormSubmit trigger that runs {handler}(e)
 * whenever this form is submitted. Run this ONCE to activate the
 * automation (the deploy wires the code but does not run it). Re-running
 * is safe: it removes any prior {handler} trigger before creating a new
 * one, so triggers never stack.
 */
function installTrigger() {{
  var handlerName = "{handler}";
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {{
    if (existing[i].getHandlerFunction() === handlerName) {{
      ScriptApp.deleteTrigger(existing[i]);
    }}
  }}
  ScriptApp.newTrigger(handlerName)
    .forForm({form_literal})
    .onFormSubmit()
    .create();
}}
"""

    body = (
        f"{note_comment}"
        f"{handler_function_body.rstrip()}\n\n"
        f"{install_trigger}"
    )
    return body, handler


@workspace_tool(
    title="Install a reactive onFormSubmit handler into a Google Form",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the Form —
    # re-running produces a duplicate bound script. NOT idempotent (same
    # convention as as_generate_bound_script / as_install_sheet_dashboard).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_FORM_HANDLER_OUTPUT_SCHEMA,
)
def as_install_form_handler(
    creds,
    form_id: str,
    handler_function_body: str,
    handler_note: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a reactive ``onFormSubmit`` automation into a Google Form.

    Deploys a *bound* Apps Script into the Form that runs your
    ``handler_function_body`` whenever the form is submitted, via an
    **installable** ``onFormSubmit`` trigger
    (``ScriptApp.newTrigger(handler).forForm(id).onFormSubmit().create()``).
    The handler receives the standard Apps Script form-submit event object
    ``e`` (for a Form-bound script, ``e.response`` is the
    ``FormResponse`` — ``e.response.getItemResponses()``, etc.) so it can
    act on the submission — route it, email a confirmation, append it to a
    tracker Sheet, call another service. Once activated it runs on Google's
    infrastructure with NO Claude call in the loop. This composes the
    generic bound-script primitive (``as_generate_bound_script``) for the
    one reactive surface a Form has.

    USE WHEN: the user wants a Form to DO something on every submission —
    "email me when someone submits", "append each response to my CRM
    Sheet", "send the submitter a confirmation". For READING existing
    responses, use the Forms/Sheets read tools instead (no script needed).

    WHY THIS EXISTS (Forms are otherwise rejected): the generic bound-script
    tools reject Forms because menus / sidebars / onEdit don't apply to a
    Form. But a Form's ONE reactive surface — the submit trigger — is real
    and useful, so this purpose-built tool unlocks exactly that, binding
    directly to the Form. (The other apps_script tools still reject Forms.)

    INSTALLABLE vs SIMPLE trigger: this installs an *installable* trigger,
    which runs with your full authorization — so the handler can email,
    call other Google services, and write other files. (A *simple*
    ``onFormSubmit`` runs in a restricted no-auth context and can't.) That
    power is why the generated script's manifest declares the
    ``script.scriptapp`` scope.

    IMPORTANT — activation is a required one-time step (same as
    ``as_install_sheet_dashboard`` / ``as_install_edit_trigger``). An
    *installable* trigger only exists once its installer runs, and deploying
    a script does NOT run it (and the Apps Script REST API can't create the
    trigger remotely). So this tool WIRES the trigger into the deployed
    script but the handler is NOT live yet on return. To activate: open the
    returned ``project_url``, run the ``installTrigger`` function once (the
    editor's Run button), and approve the authorization prompt. After that
    single run the handler fires on every submission. The return payload
    says so explicitly — ``trigger_active`` is ``False`` and
    ``activation_required`` is ``True`` with the step spelled out. Do not
    tell the user their Form is already handling submissions until they've
    run ``installTrigger`` once.

    Args:
        form_id: Drive ID of the Google Form to install the automation into
            (the ID part of the Form's edit URL). The bound script + the
            ``onFormSubmit`` trigger are attached to THIS Form.
        handler_function_body: the ``.gs`` source for the submit handler as
            a NAMED function declaration, e.g.
            ``"function onSubmit(e) { /* act on e.response */ }"``. Claude
            authors this — it's the work that runs on each submission. Its
            declared name becomes the trigger handler. Required; empty /
            unnamed bodies are rejected. The function should accept the
            form-submit event parameter (conventionally ``e``).
        handler_note: OPTIONAL human note rendered as a leading comment in
            the generated script (documents the handler's intent for anyone
            who opens the editor). Does not affect behavior.
        name: OPTIONAL title for the new Apps Script project. Defaults to a
            generated form-handler name.
        on_conflict: what to do when an onFormSubmit automation from THIS
            tool already exists on this Form. "new" (the default) always
            installs a fresh one (which can leave duplicate handlers);
            "replace" uninstalls the prior install(s) on this Form first
            (no duplicate, no orphan); "skip" returns the existing install
            unchanged instead of adding a duplicate. Keyed by (this tool,
            this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, form_id, trigger_type,
        trigger_handler, project_url, trigger_active, activation_required,
        activation_instructions}``. ``trigger_type`` is ``"onFormSubmit"``.
        ``trigger_handler`` is the parsed handler-function name the trigger
        drives. ``project_url`` deep-links to the script editor.
        ``trigger_active`` is ``False`` and ``activation_required`` is
        ``True`` on a successful deploy — the trigger is wired but needs a
        one-time ``installTrigger`` run; ``activation_instructions`` is the
        literal step.

    Raises:
        ToolError: an empty / unnamed ``handler_function_body`` (rejected
            before any API call), or any Apps Script / Drive API error —
            the standard decorator envelope renders these as user-facing
            ``ToolError``.

    Choreography: get ``form_id`` from the user's Form edit URL. After this
    returns, point the user at ``project_url`` to run ``installTrigger``
    once — that's the only manual step. (The Apps Script scopes are in the
    baseline grant, so most users won't see a second OAuth consent for the
    deploy itself; the in-editor ``installTrigger`` run has its own one-time
    authorization prompt for the trigger scope.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call. These
    #    ValueErrors propagate as ToolError via the decorator envelope.
    if not form_id or not form_id.strip():
        raise ValueError(
            "form_id cannot be empty — pass the Drive ID of the Google "
            "Form to install the onFormSubmit handler into."
        )
    if not handler_function_body or not handler_function_body.strip():
        raise ValueError(
            "handler_function_body cannot be empty — pass the .gs source "
            "for the submit-reaction work as a named function declaration "
            "(e.g. `function onSubmit(e) { ... }`)."
        )

    # 2. Synthesize the full .gs body (handler fn + dedup'd installTrigger
    #    wiring the onFormSubmit trigger). _extract_handler_name (inside)
    #    also rejects an unnamed function.
    script_body, handler = build_form_handler_script_body(
        handler_function_body, form_id, handler_note
    )

    # 3. Build the manifest. An installable onFormSubmit trigger needs the
    #    script.scriptapp oauthScope; we get it by handing build_manifest
    #    that scope via oauth_scopes (the manifest can't declare the
    #    trigger itself — the #138 manifest-reality finding). We do NOT
    #    pass a triggers entry: build_manifest's _validate_triggers only
    #    knows "time"/"edit", and a form-submit trigger needs no plan echo
    #    beyond the scope it requires (which we supply directly).
    manifest_dict = _build_manifest({"oauth_scopes": [_TRIGGER_SCOPE]})

    # 4. Default the project name when not supplied.
    project_name = name or "appscriptly onFormSubmit handler"

    # 5. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=form_id), push the body + manifest, cut a
    #    version + deploy. We bind DIRECTLY to the Form ID and never call
    #    auto_detect_container_kind — that is how the Forms hard-rejection
    #    is lifted for this purpose-built path (the generic primitive's
    #    auto-detect stays Forms-rejecting for menu/sidebar/edit use).
    result = _mint_bound_automation(
        creds,
        tool="as_install_form_handler",
        container_id=form_id,
        container_kind="forms",
        project_name=project_name,
        script_body=script_body,
        manifest_dict=manifest_dict,
        on_conflict=on_conflict,
        handler_functions=[handler],
    )
    script_id = result.script_id
    deployment_id = result.deployment_id

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "form_id": form_id,
        "trigger_type": "onFormSubmit",
        "trigger_handler": handler,
        "project_url": f"https://script.google.com/d/{script_id}/edit",
        # HONEST trigger state: the deploy wires the trigger but does NOT
        # run installTrigger, so the handler is not live yet. trigger_active
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
                f"authorization prompt. That activates the onFormSubmit "
                f"handler `{handler}`; it then runs on Google's "
                f"infrastructure on every submission with no further action."
            ),
        ),
    }
