"""``as_install_contact_sync`` — create/sync a contact from each form submission.

GAS service-parity (Contacts). A *use-case* tool layered on the PR-Δ7
bound-script generator primitive (``as_generate_bound_script`` /
``services/apps_script/api.py``). It is a Contacts-specialized sibling of
``as_install_form_handler``: install a **reactive** installable
``onFormSubmit`` trigger bound to a Google Form whose handler uses
``ContactsApp`` to create or update a Google contact from each submission -
turning a "contact us" / lead-capture / signup Form into a contact book
that fills itself, on Google's infrastructure, with no Claude call in the
loop after the install.

**Why this needs Apps Script (the REST gap).** The Contacts REST tools
(``gcontacts_create`` etc.) create ONE contact per call, in the
conversation, with Claude in the loop. They cannot install a *reactive*
job that fires on every future Form submission. That submit-driven contact
creation is exactly what a Form-bound script with an ``onFormSubmit``
trigger does - so this tool is the second (automation) lever for Contacts,
complementing the native REST lever.

**Lifts the Forms hard-rejection (same as form_handler / grade).** The
generic primitive's ``auto_detect_container_kind`` REJECTS Forms (a Form
has no Ui menu / onEdit surface). But a Form's ONE reactive surface - the
submit trigger - is real and useful, so this purpose-built tool binds
DIRECTLY to the Form ID (``create_bound_project(creds, form_id, ...)``)
without calling ``auto_detect_container_kind`` at all. The generic
primitive's auto-detect stays Forms-rejecting for every other use.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py`` and the ``.gs`` body
synthesis (caller's handler + a dedup-then-create ``installTrigger()`` wired
to ``ScriptApp.newTrigger(h).forForm(id).onFormSubmit().create()``) is
reused from ``form_handler.build_form_handler_script_body``. This module's
OWN contribution is the Contacts scope wiring + the Contacts-specialized
contract.

**⚠️ Scope (the load-bearing part - verify-LAST).** ``ContactsApp`` needs
the full ``https://www.googleapis.com/auth/contacts`` scope to create/update
contacts. That scope lives ONLY in the GENERATED bound script's manifest
(declared via ``build_manifest``'s ``oauth_scopes``) - authorized by the
user the first time they run ``installTrigger`` in the editor. This tool
DECLARES only ``GAS_BOUND_SCOPES`` (``script.projects`` +
``script.deployments``) for appscriptly's OWN consent - both already
baseline-granted. So this tool adds NO new scope to appscriptly's own
consent / OAuth-verification set (same model as ``grade_form_responses`` /
``form_handler``). An installable ``onFormSubmit`` trigger ALSO needs
``script.scriptapp`` in the generated manifest; we supply that via
``oauth_scopes`` too.

**The trigger-activation caveat (same as form_handler).** An *installable*
trigger only comes into existence when ``installTrigger`` actually *runs*;
the deploy wires it but does NOT make it live (and the Apps Script REST API
can't create the trigger remotely). The return payload is HONEST:
``trigger_active`` is ``False`` / ``activation_required`` is ``True`` with
the one-step instruction.
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
from appscriptly.services.apps_script.form_handler import (
    build_form_handler_script_body as _build_form_handler_script_body,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_CONTACT_SYNC_OUTPUT_SCHEMA

# Imported for parity with the sibling apps_script tools; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# ContactsApp needs the FULL contacts scope to create/update contacts. This
# is declared in the GENERATED manifest only - NOT added to appscriptly's
# own consent (see the module docstring's scope note).
_CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"

# An installable onFormSubmit trigger created via ScriptApp.newTrigger(...)
# .create() runs with the user's full authorization, so the generated
# manifest must also declare this scope. Same scope form_handler's
# onFormSubmit trigger needs. We pass both scopes to build_manifest via
# oauth_scopes (the trigger is wired in code; the manifest carries the
# scopes).
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"


@workspace_tool(
    title="Install a reactive contact-sync onFormSubmit handler into a Form",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the Form -
    # re-running produces a duplicate bound script. NOT idempotent (same
    # convention as as_install_form_handler / as_generate_bound_script).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_CONTACT_SYNC_OUTPUT_SCHEMA,
)
def as_install_contact_sync(
    creds,
    form_id: str,
    handler_function_body: str,
    handler_note: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a reactive contact-sync automation into a Google Form.

    Deploys a *bound* Apps Script into the Form that runs your
    ``handler_function_body`` whenever the form is submitted, via an
    **installable** ``onFormSubmit`` trigger
    (``ScriptApp.newTrigger(handler).forForm(id).onFormSubmit().create()``).
    The handler receives the standard Apps Script form-submit event object
    ``e`` (``e.response`` is the ``FormResponse``) and uses ``ContactsApp``
    to create or update a Google contact from the submission - turning a
    lead-capture / signup / "contact us" Form into a self-filling contact
    book. Once activated it runs on Google's infrastructure with NO Claude
    call in the loop. This composes the generic bound-script primitive
    (``as_generate_bound_script``) for the Contacts automation pattern, and
    is the Contacts-specialized sibling of ``as_install_form_handler``.

    USE WHEN: the user wants every Form submission to land in their Google
    Contacts - "add a contact whenever someone fills out my signup form",
    "sync each lead from this form into my contacts". For a ONE-OFF contact,
    use ``gcontacts_create`` (no script needed). For a generic (non-contact)
    submit reaction, use ``as_install_form_handler``.

    YOU AUTHOR THE SYNC; THE TOOL OWNS THE CHOREOGRAPHY. Supply
    ``handler_function_body`` - a NAMED function that reads the submission
    and creates/updates the contact via ``ContactsApp``, e.g.::

        function onSubmit(e) {
          var items = e.response.getItemResponses();
          var name = items[0].getResponse();
          var email = items[1].getResponse();
          // Avoid duplicates: reuse an existing contact for this email.
          var existing = ContactsApp.getContactsByEmailAddress(email);
          if (existing && existing.length) { return; }
          ContactsApp.createContact(name, '', email);
        }

    Its declared name becomes the trigger handler. The generated
    ``installTrigger()`` wires it to ``onFormSubmit`` (with dedup-then-create
    so re-running never stacks duplicate triggers); you decide how the
    submission maps to a contact. Claude authors the body - same trust model
    as the other apps_script generators.

    IMPORTANT - activation is a required one-time step (same as
    ``as_install_form_handler``). An *installable* trigger only exists once
    its installer runs, and deploying a script does NOT run it (and the Apps
    Script REST API can't create the trigger remotely). So this tool WIRES
    the trigger into the deployed script but the handler is NOT live yet on
    return. To activate: open the returned ``project_url``, run the
    ``installTrigger`` function once (the editor's Run button), and approve
    the authorization prompt (which includes the ``contacts`` scope - see
    the scope note below). After that single run the handler fires on every
    submission. The return payload says so explicitly - ``trigger_active``
    is ``False`` and ``activation_required`` is ``True`` with the step
    spelled out. Do not tell the user their Form is already creating
    contacts until they've run ``installTrigger`` once.

    SCOPE NOTE: creating/updating contacts needs the full ``contacts``
    scope, which lives ONLY in the generated bound script's manifest
    (authorized when the user runs the trigger). This tool itself adds NO
    new scope to appscriptly's consent - it only uses the baseline Apps
    Script management scopes for the deploy. ``manifest_scope`` in the
    return reports the scope the generated script declares (for
    transparency).

    Args:
        form_id: Drive ID of the Google Form to install the automation into
            (the ID part of the Form's edit URL). The bound script + the
            ``onFormSubmit`` trigger are attached to THIS Form.
        handler_function_body: the ``.gs`` source for the submit handler as
            a NAMED function declaration, e.g.
            ``"function onSubmit(e) { /* ContactsApp.createContact(...) */ }"``.
            Claude authors this - it's the work that runs on each
            submission. Its declared name becomes the trigger handler.
            Required; empty / unnamed bodies are rejected. The function
            should accept the form-submit event parameter (conventionally
            ``e``).
        handler_note: OPTIONAL human note rendered as a leading comment in
            the generated script (documents intent for anyone who opens the
            editor). Does not affect behavior.
        name: OPTIONAL title for the new Apps Script project. Defaults to a
            generated contact-sync name.
        on_conflict: what to do when a contact-sync automation from THIS
            tool already exists on this Form. "new" (the default) always
            installs a fresh one (which can leave duplicate handlers);
            "replace" uninstalls the prior install(s) on this Form first
            (no duplicate, no orphan); "skip" returns the existing install
            unchanged instead of adding a duplicate. Keyed by (this tool,
            this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, form_id, trigger_type, trigger_handler,
        project_url, trigger_active, activation_required,
        activation_instructions, manifest_scope}``. ``trigger_type`` is
        ``"onFormSubmit"``. ``trigger_handler`` is the parsed handler name
        the trigger drives. ``trigger_active`` is ``False`` /
        ``activation_required`` is ``True`` on a successful deploy (the
        trigger is wired but needs a one-time ``installTrigger`` run).
        ``manifest_scope`` is the full ``contacts`` scope the GENERATED
        script declares (transparency - it's the bound script's scope, not
        appscriptly's consent).

    Raises:
        ValueError: an empty ``form_id``, or an empty / unnamed
            ``handler_function_body`` (rejected before any API call).
        ToolError: any Apps Script / Drive API error - the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``.

    Choreography: get ``form_id`` from the user's Form edit URL or a prior
    ``gforms_create_form`` call. After this returns, point the user at
    ``project_url`` to run ``installTrigger`` once - that's the only manual
    step. (The Apps Script scopes are baseline-granted, so most users won't
    see a second OAuth consent for the deploy itself; the in-editor
    ``installTrigger`` run has its own one-time authorization for the
    ``contacts`` scope.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call. These
    #    ValueErrors propagate as ToolError via the decorator envelope.
    if not form_id or not form_id.strip():
        raise ValueError(
            "form_id cannot be empty - pass the Drive ID of the Google "
            "Form to install the contact-sync handler into."
        )
    if not handler_function_body or not handler_function_body.strip():
        raise ValueError(
            "handler_function_body cannot be empty - pass the .gs source "
            "for the submit-reaction work as a named function declaration "
            "(e.g. `function onSubmit(e) { ... }`)."
        )

    # 2. Synthesize the full .gs body (handler fn + dedup'd installTrigger
    #    wiring the onFormSubmit trigger). Reuses form_handler's body
    #    builder verbatim - it does exactly the right thing (named-function
    #    extraction + dedup-then-create onFormSubmit trigger bound to this
    #    Form). _extract_handler_name (inside) rejects an unnamed function.
    script_body, handler = _build_form_handler_script_body(
        handler_function_body, form_id, handler_note
    )

    # 3. Build the manifest. An installable onFormSubmit trigger needs
    #    script.scriptapp, and ContactsApp needs the full contacts scope -
    #    both supplied via oauth_scopes. BOTH land in the GENERATED manifest
    #    only - never in appscriptly's own consent (the load-bearing
    #    verify-LAST guarantee). We do NOT pass a triggers entry:
    #    build_manifest's _validate_triggers only knows "time"/"edit", and a
    #    form-submit trigger needs no plan echo beyond the scopes it
    #    requires (which we supply directly).
    #    add_mail_scope adds script.send_mail so the injected failure
    #    reporter can email the owner if a submission handler throws (gap
    #    #5); GENERATED manifest only, never appscriptly's consent.
    manifest_dict = _build_manifest(
        {"oauth_scopes": _add_mail_scope([_TRIGGER_SCOPE, _CONTACTS_SCOPE])}
    )

    # 4. Default the project name when not supplied.
    project_name = name or "appscriptly contact sync"

    # 5. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=form_id), push the body + manifest, cut a
    #    version + deploy. We bind DIRECTLY to the Form ID and never call
    #    auto_detect_container_kind (which rejects Forms) - same
    #    Forms-rejection lift as form_handler / grade_form_responses.
    result = _mint_bound_automation(
        creds,
        tool="as_install_contact_sync",
        container_id=form_id,
        container_kind="forms",
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
                f"authorization prompt (it includes the `contacts` scope the "
                f"handler needs). That activates the onFormSubmit handler "
                f"`{handler}`; it then runs on Google's infrastructure on "
                f"every submission with no further action."
            ),
        ),
        # Transparency: the scope the GENERATED bound script declares to
        # create/update contacts. It is the bound script's manifest scope,
        # NOT a scope added to appscriptly's own OAuth consent.
        "manifest_scope": _CONTACTS_SCOPE,
    }
