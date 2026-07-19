"""``as_check_activation`` - confirm a deployed automation is live yet.

The verification companion to the ``as_*`` installer families (Stream 3).
Every Class D-H install ends with an ACTIVATION WALL - a one-time
Run + Allow the user performs in the Apps Script editor - and the install
payload now surfaces that as unified ``activation_*`` fields (see
``appscriptly/activation.py``). This tool CLOSES the loop: instead of only
INSTRUCTING the activation, it CONFIRMS whether it actually happened, so an
agent can answer "is it live yet?" without leaving the conversation.

Two mechanisms, auto-selected by whether ``exec_url`` is supplied:

  * Web app (Class H): pass ``exec_url`` (the ``/exec`` URL from
    ``as_deploy_web_app``). The tool GETs it - a 403 is Google's per-script
    consent door (NOT activated); any 200 means the script serves past the
    door (activated). No JSON contract is assumed (an arbitrary user
    endpoint), so the probe keys on the status, not the body.
  * Bound trigger / on-demand action (Classes D-G): omit ``exec_url``. The
    tool reads the project's execution history
    (``processes.listScriptProcesses``) and reports whether the activation
    function (``installTrigger`` / ``renderFrames`` / ``gradeResponses`` /
    ``refreshLinkedSlides``, or a caller-named one) has COMPLETED a run.

``activated`` is tri-state: ``True`` (evidence it is live), ``False``
(evidence it is NOT yet - no run found, consent-gated, or a FAILED
activation run), or ``None`` (indeterminate - a run is in progress, or the
probe was inconclusive). Same ``script.processes`` scope as
``as_list_script_processes`` (SENSITIVE, no CASA); read-only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from appscriptly.activation import activation_editor_url
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script.processes import list_script_processes
from appscriptly.services.apps_script.scopes import GAS_PROCESSES_SCOPE
from appscriptly.setup_apps_script import WebAppHealth, probe_webapp_health
from appscriptly.tool_schemas import AS_CHECK_ACTIVATION_OUTPUT_SCHEMA

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# The activation functions the as_* installer families generate. When the
# caller does not name one, as_check_activation scans the execution history
# for any of these (mirrors the function names build_*_script emit).
_KNOWN_ACTIVATION_FUNCTIONS = frozenset({
    "installTrigger",       # Classes D/E - time + reactive triggers
    "renderFrames",         # Class G - slides-to-video render
    "gradeResponses",       # Class F - grade form responses
    "refreshLinkedSlides",  # Class F - refresh linked slides
})

# Apps Script Process statuses. A COMPLETED run of the activation function
# is the "it is live" signal; RUNNING is in-progress; anything else that
# matched (FAILED / TIMED_OUT / CANCELED) is a not-activated signal.
_COMPLETED = "COMPLETED"
_RUNNING = "RUNNING"

# Cap on how many matched execution rows we echo back (the underlying
# listing is already page-bounded; this keeps the payload compact).
_MAX_MATCHED = 10


def _check_webapp(exec_url: str, script_id: str) -> dict:
    """Probe a web app's ``/exec`` and map the health verdict to an answer.

    ``require_json=False``: an arbitrary user web app may return HTML/text
    or be doPost-only, so any 200 means the script ran (activated) and only
    the 403 consent door means not-activated. See ``probe_webapp_health``.
    """
    activation_url = activation_editor_url(script_id)
    health = probe_webapp_health(exec_url, require_json=False)
    if health is WebAppHealth.HEALTHY:
        return {
            "script_id": script_id,
            "method": "webapp_probe",
            "activated": True,
            "exec_state": "serving",
            "activation_url": activation_url,
            "message": (
                "The web app is serving: the /exec endpoint answered past "
                "Google's consent door, so it is activated."
            ),
        }
    if health is WebAppHealth.CONSENT_GATED:
        return {
            "script_id": script_id,
            "method": "webapp_probe",
            "activated": False,
            "exec_state": "needs_activation",
            "activation_url": activation_url,
            "message": (
                "Not activated yet: the /exec endpoint returns Google's "
                "per-script consent door (403). The deploying user must open "
                "the activation_url, run any function once, and click Allow."
            ),
        }
    if health is WebAppHealth.GONE:
        return {
            "script_id": script_id,
            "method": "webapp_probe",
            "activated": None,
            "exec_state": "gone",
            "activation_url": activation_url,
            "message": (
                "The /exec endpoint returns 404: the deployment is gone "
                "(deleted or archived), so activation state cannot be "
                "determined. Re-deploy the web app."
            ),
        }
    # WebAppHealth.UNKNOWN - transport trouble or a retryable status.
    return {
        "script_id": script_id,
        "method": "webapp_probe",
        "activated": None,
        "exec_state": "unknown",
        "activation_url": activation_url,
        "message": (
            "Could not reach the /exec endpoint (network trouble or a "
            "transient server status). Activation state is unknown; retry."
        ),
    }


def _check_processes(
    creds: Credentials, script_id: str, activation_function: str | None
) -> dict:
    """Read execution history + judge whether the activation function ran.

    Looks for the caller-named ``activation_function`` (or, when omitted,
    any of ``_KNOWN_ACTIVATION_FUNCTIONS``) in the project's recent
    executions. A COMPLETED run means the automation is live.
    """
    listing = list_script_processes(creds, script_id, page_size=50)
    processes = listing.get("processes", []) or []
    candidates = (
        {activation_function}
        if activation_function
        else _KNOWN_ACTIVATION_FUNCTIONS
    )
    matched = [
        p for p in processes if p.get("function_name") in candidates
    ]
    base = {
        "script_id": script_id,
        "method": "process_history",
        "activation_url": activation_editor_url(script_id),
        "matched_processes": matched[:_MAX_MATCHED],
    }

    # The listing is newest-first (per the API), so the first row of a
    # status bucket is the most recent run of that kind.
    completed = [p for p in matched if p.get("process_status") == _COMPLETED]
    running = [p for p in matched if p.get("process_status") == _RUNNING]

    if completed:
        top = completed[0]
        fn = activation_function or top.get("function_name")
        return {
            **base,
            "activated": True,
            "activation_function": fn,
            "last_status": top.get("process_status"),
            "last_run_time": top.get("start_time"),
            "message": (
                f"Activated: `{fn}` has a COMPLETED execution in the script's "
                f"history, so the automation is live."
            ),
        }
    if running:
        top = running[0]
        fn = activation_function or top.get("function_name")
        return {
            **base,
            "activated": None,
            "activation_function": fn,
            "last_status": top.get("process_status"),
            "last_run_time": top.get("start_time"),
            "message": (
                f"In progress: `{fn}` is RUNNING now. Re-check in a moment to "
                f"confirm it completed."
            ),
        }
    if matched:
        # Matched, but neither COMPLETED nor RUNNING (FAILED / TIMED_OUT /
        # CANCELED) - the activation attempt did not succeed.
        top = matched[0]
        fn = activation_function or top.get("function_name")
        return {
            **base,
            "activated": False,
            "activation_function": fn,
            "last_status": top.get("process_status"),
            "last_run_time": top.get("start_time"),
            "message": (
                f"Not activated: the most recent run of `{fn}` ended "
                f"`{top.get('process_status')}`, not COMPLETED. Open the "
                f"activation_url, run it again, and approve the authorization "
                f"prompt."
            ),
        }
    # No run of the activation function in the history at all.
    return {
        **base,
        "activated": False,
        "activation_function": activation_function,
        "last_status": None,
        "last_run_time": None,
        "message": (
            "Not activated yet: no run of the activation function appears in "
            "the script's execution history. Open the activation_url, run "
            "the activation function once, and click Allow. (If your "
            "automation uses a custom function name, pass it as "
            "activation_function.)"
        ),
    }


@workspace_tool(
    title="Check whether a deployed automation is activated yet",
    service="apps_script",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[GAS_PROCESSES_SCOPE],
    output_schema=AS_CHECK_ACTIVATION_OUTPUT_SCHEMA,
)
def as_check_activation(
    creds,
    script_id: str,
    activation_function: str | None = None,
    exec_url: str | None = None,
) -> dict:
    """Confirm whether a deployed as_* automation has been activated yet.

    USE WHEN: you installed an automation via an ``as_install_*`` tool,
    ``as_generate_bound_script``, or ``as_deploy_web_app`` and the payload
    said ``activation_required`` (or ``needs_activation``) - and you want to
    verify the user's one-time Run + Allow actually took effect, rather than
    only telling them to do it. This is the verification half of the
    activation UX.

    Two modes, chosen by ``exec_url``:

    * WEB APP (pass ``exec_url``): probes the ``/exec`` endpoint. A 403 is
      Google's per-script consent door (not activated); any 200 means it
      serves (activated). Use for ``as_deploy_web_app`` deployments.
    * BOUND TRIGGER / ON-DEMAND ACTION (omit ``exec_url``): reads the
      project's execution history and reports whether the activation
      function has a COMPLETED run. Use for the Class D-G installers
      (``as_install_sheet_dashboard``, ``as_install_edit_trigger``,
      ``as_grade_form_responses``, ``as_generate_video_deck``, ...).

    Args:
        script_id: the Apps Script project's scriptId (from the deploy
            tool's result, or the ``/d/{scriptId}/edit`` editor URL).
            Required for both modes.
        activation_function: the exact function whose run proves activation
            (the ``activation_function`` field the installer returned, e.g.
            ``installTrigger`` / ``renderFrames`` / ``gradeResponses``).
            Optional: when omitted, the tool scans the history for any of
            the standard activation functions. Pass it when your automation
            uses a custom name. Ignored in web-app mode.
        exec_url: the web app's ``/exec`` URL (from ``as_deploy_web_app``).
            When supplied, the tool takes the web-app probe path instead of
            reading execution history.

    Returns:
        ``{script_id, activated, method, activation_url, message, ...}``.
        ``activated`` is True / False / null (live / not-yet /
        indeterminate). ``method`` is ``"webapp_probe"`` or
        ``"process_history"``. Web-app mode adds ``exec_state`` (serving /
        needs_activation / gone / unknown); process mode adds
        ``activation_function``, ``last_status``, ``last_run_time``, and the
        ``matched_processes`` rows. ``message`` is a ready-to-relay summary.

    Raises:
        ValueError: empty ``script_id``.
        ToolError: a Google API error from the execution-history read (e.g.
            403 if the Apps Script API is not enabled for the account) -
            rendered by the standard decorator envelope. When that 403 is
            the "Apps Script API not enabled" case, the deploying user must
            open https://script.google.com/home/usersettings, toggle
            "Google Apps Script API" ON, and retry: the API is off by
            default and blocks all script management until it is turned on.

    Choreography: install -> relay the ``activation_instructions`` ->
    (user runs the function + Allows) -> call this with the same
    ``script_id`` (+ ``activation_function``, or ``exec_url`` for a web app)
    to confirm ``activated`` flipped to True.
    """
    if not script_id or not script_id.strip():
        raise ValueError(
            "script_id cannot be empty - pass an Apps Script project's "
            "scriptId (from the deploy tool's result, or the "
            "/d/{scriptId}/edit editor URL)."
        )
    sid = script_id.strip()
    if exec_url and exec_url.strip():
        return _check_webapp(exec_url.strip(), sid)
    return _check_processes(creds, sid, activation_function)
