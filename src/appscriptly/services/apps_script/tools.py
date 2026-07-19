"""Bound Apps Script generator — MCP tool registration (PR-Δ7).

Registers the ONE generic primitive tool ``as_generate_bound_script``.
Importing this module triggers registration with the live ``mcp``
instance — ``server.py`` performs the import at the bottom of its
module, AFTER constructing ``mcp`` and AFTER ``decorators.register(mcp,
...)`` wires the decorator (same side-effect pattern as every other
service folder).

This is the FOUNDATION tool. It ships the *primitive*, not the use
cases. Later feature PRs (slides-for-video, sheets dashboards, docs
menu-installers) compose this generic generator; none of those use-case
tools live here.

**The ``as_`` prefix.** Per the appscriptly rename (PR-Δ5.5), NEW tools
use the ``as_`` (appscriptly-native) prefix. Existing ``gdocs_*`` tools
keep their historical names. This is the first ``as_*`` tool.

**Import discipline.** Same as ``services/sheets/tools.py`` /
``services/slides/tools.py``:

- ``workspace_tool`` from ``appscriptly.decorators`` (it's already
  bound to ``mcp`` by the time server.py side-effect-imports this).
- ``_get_credentials`` + ``_format_http_error`` from ``_tool_helpers``
  (the M3 Phase C extraction) — imported for parity even though the
  ``@workspace_tool(creds=True)`` envelope handles both for the happy
  path; kept top-level so an error-path addition doesn't need a new
  import statement.
- The api module via the standard ``from ... import`` pattern.
- ``@workspace_tool(service="apps_script", scopes=GAS_BOUND_SCOPES, ...)``
  — the ``service=`` literal drives the partition test + telemetry; the
  ``scopes=`` declaration surfaces the Apps Script scopes the tool
  exercises (already in baseline, so no second consent — see scopes.py).
"""
from __future__ import annotations

from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script.api import (
    auto_detect_container_kind as _auto_detect_container_kind,
    build_manifest as _build_manifest,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_GENERATE_BOUND_SCRIPT_OUTPUT_SCHEMA

# Imported for parity with the other services' tools.py; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't trigger a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)


@workspace_tool(
    title="Generate and deploy a bound Apps Script",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment — re-running
    # produces a duplicate script bound to the same container. NOT
    # idempotent (same convention as gsheets_create_spreadsheet /
    # gdocs_make_tabbed_doc). The api layer wraps the create/deploy
    # calls with execute_with_retry(idempotent=False) accordingly.
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_GENERATE_BOUND_SCRIPT_OUTPUT_SCHEMA,
)
def as_generate_bound_script(
    creds,
    container_id: str,
    script_body: str,
    manifest: dict | None = None,
    container_kind: str | None = None,
    name: str | None = None,
    allow_restricted_scopes: bool = False,
    on_conflict: str = "new",
) -> dict:
    """Generate + deploy a *bound* Apps Script inside a Doc / Sheet / Slides.

    This is the primitive for *persistent Workspace automation*. A bound
    script lives INSIDE a specific Google Doc, Sheet, or Slides file and
    can:

      * install a **custom menu** (``Ui.createMenu`` from ``onOpen``) so
        the user gets one-click actions in that file's menu bar;
      * open a **sidebar** (``HtmlService``) with a custom UI panel;
      * run on a **time-driven trigger** (``ScriptApp.newTrigger`` — e.g.
        every hour / every day) with no one watching;
      * react to **edits** (an ``onEdit`` handler) the moment the user
        changes a cell / paragraph;
      * expose **custom functions** usable in Sheets cells.

    All of that runs on Google's infrastructure and lives in the user's
    Workspace — after this single deploy, Claude does NOT need to be in
    the loop for the automation to fire. Example use: "make this Doc
    auto-refresh from the linked Sheet every morning" → generate a bound
    script with a daily time-driven trigger that re-pulls the Sheet data
    and rewrites the Doc; it then runs itself daily, forever, without
    another Claude call.

    PLATFORM QUOTAS: because the automation runs on Google's own Apps
    Script platform, it is bound by Google's per-user quotas (concurrent
    executions, triggers created per day, UrlFetch calls per day, total
    runtime) that appscriptly cannot see or raise. A heavy first-day
    automation (many triggers, frequent runs, or large fetch loops) can
    hit those limits and fail with a Google-side quota error; keep early
    automations modest and space large jobs out.

    USE WHEN: the user wants something that keeps working *after* the
    conversation ends — a recurring job, a button/menu they can re-click,
    a reaction to their own future edits. For a ONE-OFF edit, use the
    direct docs/sheets/slides tools instead (no script needed).

    This is the generic GENERATOR: you supply the ``.gs`` ``script_body``
    that does the work (Claude writes it) and a high-level ``manifest``
    describing the menu / triggers / sidebar / scopes; the tool creates
    the bound project, pushes the code + manifest, and deploys it in one
    call. (Higher-level convenience tools for specific patterns may be
    layered on later — this is the foundation they build on.)

    Args:
        container_id: Drive ID of the Doc / Sheet / Slides file to bind
            the script to (the ID part of the file's URL).
        script_body: the Apps Script ``.gs`` source as a string. Claude
            authors this. It must define the functions the menu /
            triggers reference (e.g. an ``onOpen`` that calls
            ``Ui.createMenu``, the handler functions, etc.). Required —
            an empty body is rejected.
        manifest: OPTIONAL high-level description of the script's
            capabilities. A dict with any of:
              - ``menu``: list of ``{name, function_name}`` menu items;
              - ``triggers``: list of ``{type: "time"|"edit", ...}``;
              - ``sidebar_html``: HTML string for an ``HtmlService``
                sidebar;
              - ``oauth_scopes``: list of extra OAuth scope URLs the
                generated code needs.
            The tool translates this into the real ``appsscript.json``
            (always V8 runtime + a timeZone) and derives the right
            ``oauthScopes`` (a menu/sidebar implies ``script.container.ui``;
            a time trigger implies ``script.scriptapp``). Menus / triggers
            / sidebars are wired by the code in ``script_body`` — the
            manifest's job is the runtime + scopes. Omit for a bare
            manifest (V8 + UTC, no extra scopes).
        container_kind: OPTIONAL ``"docs"`` / ``"sheets"`` / ``"slides"``.
            If omitted (the normal case), the tool auto-detects it from
            the container's Drive mimeType. Pass it only to skip the
            detection round-trip when you already know the kind.
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated name derived from the container kind.
        allow_restricted_scopes: OPTIONAL safety opt-in. By default, if
            ``manifest["oauth_scopes"]`` includes a Google RESTRICTED scope
            (full Gmail / broad Drive), the call is REJECTED — the generic
            generator won't silently arm an automation with restricted
            authority. Pass ``True`` only after telling the user the
            consequences (the automation gains that restricted access, and
            it triggers Google's restricted-scope / CASA verification). The
            built-in ``as_install_*`` tools never need this; it's an escape
            hatch for an explicit, user-acknowledged restricted use case.
        on_conflict: what to do when a bound automation from THIS tool
            already exists on this container. "new" (the default) always
            installs a fresh one (which can leave duplicates); "replace"
            uninstalls the prior install(s) on this container first (no
            duplicate, no orphan); "skip" returns the existing install
            unchanged instead of adding a duplicate. Keyed by (this tool,
            this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, container_id, container_kind,
        project_url}``. ``project_url`` deep-links to the script editor
        (``https://script.google.com/d/{script_id}/edit``) so the user
        can inspect / tweak the generated automation.

    Raises:
        ToolError: invalid container (not a Doc / Sheet / Slides), or any
            Apps Script / Drive API error — the standard decorator
            envelope renders these as user-facing ``ToolError``.

    Choreography: get ``container_id`` from the user's URL, from a prior
    ``gdocs_make_tabbed_doc`` / ``gsheets_create_spreadsheet`` /
    ``gslides_create_presentation`` call, or from
    ``gdocs_find_doc_by_title``. After this returns, the automation is
    live in the file — no further setup. (The first call in a session
    may surface a Google consent prompt for the Apps Script scopes if the
    user hasn't granted them yet; they're in the baseline scope set, so
    most users won't see a second consent.)
    """
    # 1. Resolve the container kind (auto-detect unless caller supplied it).
    kind = container_kind or _auto_detect_container_kind(creds, container_id)

    # 2. Default the project name from the kind when not supplied.
    project_name = name or f"appscriptly bound automation ({kind})"

    # 3. Build the manifest from the high-level description (pure). The
    # restricted-scope guard lives in _build_manifest; we pass the opt-in
    # through so a caller-acknowledged restricted use case can proceed.
    manifest_dict = _build_manifest(
        manifest, allow_restricted_scopes=allow_restricted_scopes
    )

    # 4-6. Mint the bound project (create -> push -> deploy) and record it
    #      in the automation ledger, honoring on_conflict.
    result = _mint_bound_automation(
        creds,
        tool="as_generate_bound_script",
        container_id=container_id,
        container_kind=kind,
        project_name=project_name,
        script_body=script_body,
        manifest_dict=manifest_dict,
        on_conflict=on_conflict,
    )
    script_id = result.script_id
    deployment_id = result.deployment_id

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "container_id": container_id,
        "container_kind": kind,
        "project_url": f"https://script.google.com/d/{script_id}/edit",
    }
