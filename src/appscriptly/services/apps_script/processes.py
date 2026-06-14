"""``as_list_script_processes`` — read a script project's execution history.

CASA-free scope growth. The OBSERVABILITY companion to the apps_script
create+deploy levers (``as_generate_bound_script`` /
``as_deploy_web_app``): given a script project's ``script_id``, this lists
that project's recent executions (the "Executions" view in the Apps Script
editor) via the Apps Script API.

**What it is.** A pure READ tool over the Apps Script API
``processes.listScriptProcesses`` endpoint. After you deploy an automation
(a bound menu, a time-driven dashboard refresh, a webhook web app), this
lets the agent answer "did it run?", "did the last run fail?", "what
ran and when?" without leaving the conversation — closing the loop on the
generate → deploy → run lifecycle.

**Scope — ``script.processes`` (SENSITIVE, not restricted → no CASA).**
Google's classification text: "View Google Apps Script processes." It is
read-only (no execution, no project mutation). The execute-a-function
scope (``script.scriptapp`` / the deprecated ``scripts.run`` path) is NOT
requested — this tool only READS history. The scope is baseline-granted
via the single-source ``auth.WORKSPACE_SCOPES`` (added this PR), so it
needs no second consent.

**Why a separate file (not in ``tools.py``).** Same convention as the
other apps_script feature files (``doc_menu.py``, ``custom_function.py``,
…): each use-case tool lives in its own module so parallel apps_script
PRs stay merge-clean. Importing this module runs the ``@workspace_tool``
decorator, which registers the tool — and ``server.py``'s auto-discovery
walk imports it automatically (leaf name doesn't start with ``_`` and
isn't in the ``{api, scopes}`` denylist), so NO central import edit is
needed.

API reference:
  https://developers.google.com/apps-script/api/reference/rest/v1/processes/listScriptProcesses
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.decorators import workspace_tool
from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service
from appscriptly.services.apps_script.scopes import GAS_PROCESSES_SCOPE
from appscriptly.tool_schemas import AS_LIST_SCRIPT_PROCESSES_OUTPUT_SCHEMA

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# processes.listScriptProcesses page size: the API default is 50. We keep
# the same default and clamp into the API's accepted range so an
# out-of-bounds request gets a clean result instead of a Google 400.
_PAGE_SIZE_MIN = 1
_PAGE_SIZE_MAX = 100
_PAGE_SIZE_DEFAULT = 50


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _simplify_process(process: dict) -> dict:
    """Flatten an Apps Script ``Process`` resource into a compact dict.

    The raw Process carries the project name, the function that ran, the
    process type + status, the user's access level, and timing. We surface
    those flat (snake_case) — the load-bearing fields for "what ran, when,
    and did it succeed".
    """
    return {
        "project_name": process.get("projectName"),
        "function_name": process.get("functionName"),
        "process_type": process.get("processType"),
        "process_status": process.get("processStatus"),
        "start_time": process.get("startTime"),
        "duration": process.get("duration"),
        "user_access_level": process.get("userAccessLevel"),
    }


def list_script_processes(
    creds: Credentials,
    script_id: str,
    *,
    page_size: int = _PAGE_SIZE_DEFAULT,
    page_token: str | None = None,
) -> dict:
    """List a script project's executions via ``processes.listScriptProcesses``.

    Args:
        creds: OAuth credentials carrying the ``script.processes`` scope.
        script_id: the Apps Script project's scriptId (from
            ``as_generate_bound_script`` / ``as_deploy_web_app``, or the
            ``/d/{scriptId}/edit`` script-editor URL). Required.
        page_size: executions per page (clamped to 1-100; API default 50).
        page_token: token from a prior call's ``next_page_token`` to fetch
            the next page. ``None`` (default) starts at the first page.

    Returns:
        ``{script_id, processes, next_page_token, count}`` — ``processes``
        is the flattened list (newest first, per the API); ``next_page_token``
        is ``None`` on the last page; ``count`` is the page length.

    Raises:
        ValueError: empty ``script_id``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated to the
            tool-layer envelope (e.g. 403 if the Apps Script API is not
            enabled, or the caller can't read that project's processes).
    """
    if not script_id or not script_id.strip():
        raise ValueError(
            "script_id cannot be empty — pass an Apps Script project's "
            "scriptId (from as_generate_bound_script / as_deploy_web_app, "
            "or the /d/{scriptId}/edit editor URL)."
        )
    sid = script_id.strip()
    size = _clamp(int(page_size), _PAGE_SIZE_MIN, _PAGE_SIZE_MAX)

    script = get_service("script", "v1", credentials=creds)

    list_kwargs: dict[str, Any] = {"scriptId": sid, "pageSize": size}
    if page_token:
        list_kwargs["pageToken"] = page_token

    # Pure read (idempotent) — safe to retry on a transient 429/5xx.
    resp = execute_with_retry(
        lambda: script.processes().listScriptProcesses(**list_kwargs).execute(),
        idempotent=True,
        op_name="script.processes.listScriptProcesses",
    )
    processes = resp.get("processes", []) or []
    simplified = [_simplify_process(p) for p in processes]
    return {
        "script_id": sid,
        "processes": simplified,
        "next_page_token": resp.get("nextPageToken"),
        "count": len(simplified),
    }


@workspace_tool(
    title="List a script project's executions",
    service="apps_script",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[GAS_PROCESSES_SCOPE],
    output_schema=AS_LIST_SCRIPT_PROCESSES_OUTPUT_SCHEMA,
)
def as_list_script_processes(
    creds,
    script_id: str,
    page_size: int = _PAGE_SIZE_DEFAULT,
    page_token: str | None = None,
) -> dict:
    """List the recent executions (run history) of an Apps Script project.

    USE WHEN: you deployed an automation (via as_generate_bound_script /
    as_deploy_web_app / an as_install_* tool) and want to check whether it
    has RUN — "did the dashboard refresh fire?", "did the last execution
    fail?", "what ran on this script and when?". This is the read-only
    observability companion to the create+deploy tools.

    Backed by the Apps Script API ``processes.listScriptProcesses``. Uses
    the ``script.processes`` scope — a Google SENSITIVE scope, NOT
    restricted (no CASA). Read-only: it cannot run a function or change the
    project; it only reads the execution history.

    Args:
        script_id: the Apps Script project's scriptId. From the
            ``script_id`` returned by as_generate_bound_script /
            as_deploy_web_app, or from the script-editor URL
            (``https://script.google.com/d/{scriptId}/edit``). Required.
        page_size: executions per page (1-100; default 50). Clamped.
        page_token: pass a prior call's ``next_page_token`` to page through
            older executions. Omit to start at the most recent.

    Returns:
        ``{script_id, processes, next_page_token, count}`` — each
        ``processes`` entry is ``{project_name, function_name,
        process_type, process_status, start_time, duration,
        user_access_level}``. ``process_status`` (e.g. ``COMPLETED`` /
        ``FAILED`` / ``RUNNING``) answers "did it succeed?".
        ``next_page_token`` is null on the last page.

    Choreography: get ``script_id`` from the deploy tool that created the
    automation. A ``process_status`` of ``FAILED`` is the signal to inspect
    the script (open ``project_url`` from the deploy result).

    NOTE: requires the Apps Script API to be enabled in the GCP project. A
    project with no execution history returns an empty ``processes`` list
    (``count: 0``), not an error.
    """
    return list_script_processes(
        creds,
        script_id,
        page_size=page_size,
        page_token=page_token,
    )
