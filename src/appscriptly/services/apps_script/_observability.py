"""Generated-code failure observability (gap #5 — no more 3am black holes).

Every appscriptly-generated automation that runs a caller-authored handler
UNATTENDED — a time trigger firing at 3am, a reactive onEdit / onFormSubmit,
a menu action, the video renderer, a web-app handler — used to fail
SILENTLY: the only trace was a ``FAILED`` row in the Apps Script execution
log that the user had to think to poll (with a script_id they rarely still
have). This module wraps those handlers so a failure REPORTS ITSELF to the
account owner instead of vanishing.

**Mechanism = MailApp mail-to-self.** Of the three candidates (an error-log
sheet, mail-to-self, a POST-back to a server endpoint) mail is the only one
that PUSHES to where the user already looks — their inbox — with zero
polling, which is exactly the contract's ask ("VISIBLE without polling").
A sheet requires opening the sheet; a server POST-back requires a retrieval
tool (polling again) and centralizes the user's error data on our server
during an open OAuth review. Mail also has the cheapest consent story: the
send-only ``script.send_mail`` scope ("Send email as you") is NOT one of
Google's RESTRICTED scopes (that set is full-Gmail / broad-Drive — see
``api.py::_RESTRICTED_SCOPES``), so the restricted-scope guard stays intact
and the automation stays no-CASA; and because a headless trigger can never
prompt for consent at 3am, the scope MUST be granted up front at the ONE
activation Run+Allow the user already performs — so it bundles into an
existing consent as a single extra line, adding no new ritual.

**Scope boundary (load-bearing).** ``MAIL_SCOPE`` is added ONLY to the
GENERATED per-script manifest (a separate OAuth principal). It is NEVER
added to the connector's own consent (``auth.WORKSPACE_SCOPES``) or to
``GAS_BOUND_SCOPES`` — the verify-LAST / scope-neutrality guarantee is
preserved.

**Never swallows.** Every wrapper reports best-effort (the report itself is
wrapped so a missing scope / send quota can't mask anything) and then
RETHROWS the original error, so Apps Script's own failure accounting and
``as_list_script_processes`` still record the run as FAILED. The reporter is
observability ON TOP of the existing failure surface, not a replacement.

**Carve-outs (deliberate).** Two classes are NOT wrapped here:

  * ``as_install_custom_function`` — a Sheets custom function runs in a
    sandbox that FORBIDS MailApp / UrlFetchApp, and its errors are already
    visible synchronously in the cell (``#ERROR!`` with the thrown message
    on hover). No async reporting is possible or needed.
  * ``as_generate_bound_script`` — the generic primitive deploys an entire
    caller-authored ``.gs`` verbatim (the caller wires their own onOpen /
    installTrigger / handlers). There is no single generated handler to
    wrap without parsing arbitrary JS, which we deliberately do not do.
"""
from __future__ import annotations

import json

# MailApp.sendEmail's OAuth scope: SEND-ONLY ("Send email as you"). NOT a
# Google RESTRICTED scope (see api.py::_RESTRICTED_SCOPES), so it passes
# build_manifest's guard and keeps the generated automation no-CASA. Added
# ONLY to generated per-script manifests, never to the connector's consent.
MAIL_SCOPE = "https://www.googleapis.com/auth/script.send_mail"

# The injected reporter function's name — one source of truth for the
# templates that call it and the tests that assert it. Bracketed in double
# underscores so it cannot collide with a caller-authored identifier.
REPORTER_FUNCTION = "__appscriptlyReportError__"

# The catch-variable name every wrapper uses. Namespaced so it cannot shadow
# an identifier the wrapped body references.
_ERR_VAR = "__appscriptlyErr__"

# The reporter helper's ``.gs`` source. A plain (non-f) string so the JS
# string-literal escapes (``\n``) stay literal in the emitted source. The
# function name is spelled out here and cross-checked against
# REPORTER_FUNCTION by the unit tests. Best-effort: the whole body is inside
# a try/catch so a reporting failure (no mail scope, daily send quota, a
# consumer account that hides the address) is swallowed and can NEVER mask
# the original error the caller wrapper rethrows.
_REPORTER_HELPER_SRC = """\
/**
 * appscriptly failure reporter (auto-injected). Emails the account owner
 * when a generated automation throws, so an unattended failure is visible
 * without polling. Best-effort: it never throws, and the caller wrapper
 * always rethrows the original error so Apps Script still records the run
 * as failed.
 */
function __appscriptlyReportError__(context, err) {
  try {
    var recipient = Session.getEffectiveUser().getEmail();
    if (!recipient) {
      return;
    }
    var detail = (err && err.stack) ? err.stack : String(err);
    MailApp.sendEmail(
      recipient,
      '[appscriptly] Automation error in ' + context,
      'An automation appscriptly installed in your Google account just failed.\\n\\n'
        + 'Function: ' + context + '\\n'
        + 'Time: ' + (new Date()) + '\\n'
        + 'Error: ' + detail + '\\n\\n'
        + 'The automation is still installed. Open its Apps Script project to review or fix it.'
    );
  } catch (reportErr) {
    // Best-effort only: a reporting failure (missing mail scope, send
    // quota, a hidden address) must never mask the original error, which
    // the caller wrapper rethrows.
  }
}
"""


def reporter_helper_source() -> str:
    """Return the ``.gs`` source for the injected failure reporter (PURE).

    Deterministic. Include EXACTLY ONCE per generated script (every wrapper
    in that script calls the same ``__appscriptlyReportError__``). Emits no
    trailing newline beyond the one the source ends with.
    """
    return _REPORTER_HELPER_SRC


def add_mail_scope(scopes: list[str] | None) -> list[str]:
    """Return ``scopes`` with ``MAIL_SCOPE`` appended (order-stable, deduped).

    The single, documented way a template opts its generated manifest into
    the failure-reporter's send scope. Pass the tool's existing
    ``oauth_scopes`` (or ``None``) straight through to ``build_manifest``::

        _build_manifest({..., "oauth_scopes": add_mail_scope([_TRIGGER_SCOPE])})

    MAIL_SCOPE is appended only if not already present so a re-call / a
    manifest that already lists it stays a no-op.
    """
    out = list(scopes or [])
    if MAIL_SCOPE not in out:
        out.append(MAIL_SCOPE)
    return out


def guarded_function_block(name: str, body: str, *, params: str = "") -> str:
    """Emit a full guarded ``function`` declaration (PURE).

    For a handler appscriptly fully generates around a caller-authored body
    (menu-item handlers): the caller's ``body`` statements run inside a
    ``try``; on throw the failure is reported under ``name`` then RETHROWN.

    Args:
        name: the function name (a validated JS identifier).
        body: the caller-authored statements that go inside the function.
            Inserted verbatim inside the ``try`` (may be empty — a no-op
            handler is legal).
        params: the parameter list (default none).

    Returns:
        ``function name(params) { try { <body> } catch (e) { report; throw e } }``
        as a string (no trailing newline).
    """
    label = json.dumps(name)
    inner = _indent_block(body, "    ")
    return (
        f"function {name}({params}) {{\n"
        f"  try {{\n"
        f"{inner}"
        f"  }} catch ({_ERR_VAR}) {{\n"
        f"    {REPORTER_FUNCTION}({label}, {_ERR_VAR});\n"
        f"    throw {_ERR_VAR};\n"
        f"  }}\n"
        f"}}"
    )


def wrap_generated_body(context_label: str, body: str) -> str:
    """Wrap a generated function BODY in try / report / rethrow (PURE).

    For functions whose body appscriptly fully authors (the grade / refresh
    / render actions, a web-app handler): returns the block that goes
    BETWEEN the caller template's ``function f() {`` and its closing ``}``,
    so the template keeps its own JSDoc + signature. ``body`` is inserted
    verbatim inside the ``try`` (its own indentation is preserved); a throw
    is reported under ``context_label`` then RETHROWN.

    Returns the block including a leading + trailing newline.
    """
    label = json.dumps(context_label)
    inner = body.strip("\n")
    return (
        f"  try {{\n"
        f"{inner}\n"
        f"  }} catch ({_ERR_VAR}) {{\n"
        f"    {REPORTER_FUNCTION}({label}, {_ERR_VAR});\n"
        f"    throw {_ERR_VAR};\n"
        f"  }}\n"
    )


def _delegating_guard(entry_name: str, target_name: str, context_label: str) -> str:
    """Emit ``function entry_name(e) { try { return target_name(e) } catch ... }``.

    The shared core behind ``guarded_delegator`` and ``guarded_entry_point``:
    a function that runs ``target_name(e)`` (event passed through, return
    value forwarded) inside a try, reports a throw under ``context_label``,
    then RETHROWS. No trailing newline.
    """
    label = json.dumps(context_label)
    return (
        f"function {entry_name}(e) {{\n"
        f"  try {{\n"
        f"    return {target_name}(e);\n"
        f"  }} catch ({_ERR_VAR}) {{\n"
        f"    {REPORTER_FUNCTION}({label}, {_ERR_VAR});\n"
        f"    throw {_ERR_VAR};\n"
        f"  }}\n"
        f"}}"
    )


def guard_name_for(handler_name: str) -> str:
    """The guard-wrapper function name for a trigger handler (one source of truth).

    ``guarded_delegator`` generates a wrapper named this, and the generated
    ``installTrigger()`` targets it - so it is the ACTUAL function the
    installable trigger fires. Installers must record THIS (not the caller's
    semantic name) in the S2 automation ledger's ``handler_functions``: the
    self-disarm reaper (``_lifecycle.build_disarm_script``) redefines exactly
    this name to delete all project triggers on the next fire, so it has to
    match what the trigger wires or the automation never self-disarms on
    uninstall.
    """
    return f"__appscriptlyGuarded_{handler_name}__"


def guarded_delegator(handler_name: str) -> tuple[str, str]:
    """Return ``(guard_source, guard_name)`` for a trigger handler (PURE).

    For the reactive / time-driven trigger classes the caller supplies a
    WHOLE named ``function`` declaration that generated code references by
    name (the trigger targets it). We do not touch that caller source;
    instead the generated ``installTrigger()`` targets this GUARD, which
    delegates to the caller's handler inside a try / report / rethrow. The
    event object ``e`` is passed straight through so the handler still sees
    the trigger event.

    Args:
        handler_name: the caller's handler function name (a validated JS
            identifier, from the module's ``_extract_handler_name``).

    Returns:
        ``(source, guard_name)`` — the guard's ``.gs`` source (no trailing
        newline) and its function name (``__appscriptlyGuarded_<handler>__``,
        derived from ``handler_name`` so it cannot collide with it).
    """
    guard_name = guard_name_for(handler_name)
    jsdoc = (
        f"/**\n"
        f" * appscriptly guarded entry point (auto-injected). The trigger\n"
        f" * targets THIS function; it runs {handler_name}(e), emails you if\n"
        f" * it throws (best-effort), and rethrows so the failure is still\n"
        f" * recorded in the execution log. {handler_name} stays your code.\n"
        f" */\n"
    )
    return jsdoc + _delegating_guard(guard_name, handler_name, handler_name), guard_name


def guarded_entry_point(entry_name: str, target_name: str) -> str:
    """Emit a guarded web-app entry point that KEEPS its exact name (PURE).

    For ``doGet`` / ``doPost``: Apps Script invokes these by their exact
    names, so we cannot rename the ENTRY. The caller's original is renamed
    to ``target_name`` and this generated ``entry_name`` delegates to it
    inside a try / report / rethrow (reported under ``entry_name``). The
    request event ``e`` and the ``ContentService`` / ``HtmlService`` return
    value are both forwarded, so the HTTP response is unchanged on success;
    on a throw the owner is emailed and the error rethrown (a 500, as today).

    Returns the guard source (no trailing newline).
    """
    return _delegating_guard(entry_name, target_name, entry_name)


def _indent_block(body: str, prefix: str) -> str:
    """Indent each non-empty line of ``body`` by ``prefix`` (PURE).

    Blank lines stay blank (no trailing whitespace). Always returns a
    trailing newline so the block drops cleanly between wrapper lines. An
    empty body yields ``""`` (a no-op handler produces an empty try).
    """
    if not body.strip():
        return ""
    lines = body.strip("\n").splitlines()
    out = "\n".join(f"{prefix}{ln}" if ln.strip() else "" for ln in lines)
    return out + "\n"
