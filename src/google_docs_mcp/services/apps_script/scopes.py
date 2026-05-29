"""OAuth scopes needed to generate + deploy a *bound* Apps Script project.

PR-Δ7 — the bound-script generator. These are the same two Apps Script
scopes that ``gas_deploy`` already requests for the runtime installer:

  * ``script.projects``    — create a project, push its content.
  * ``script.deployments`` — cut a version + deploy it.

**No second consent.** Both scopes are ALREADY in the baseline
``auth.SCOPES`` set (added in PR #125, the "Apps Script scopes in
baseline" ship). Declaring them again on the ``as_generate_bound_script``
tool's ``@workspace_tool(scopes=...)`` decorator is a no-op for the
consent flow — the user granted them on first run — but it keeps the
per-tool scope declaration *honest* (the tool annotation surfaces
exactly which scopes the tool exercises, readable via
``tool.annotations.scopes`` from ``mcp.list_tools()`` for observability
/ dynamic-consent UI). See ``decorators.workspace_tool``'s ``scopes=``
docstring for the annotation-vs-resolution split.

**Why bound (not standalone).** ``gas_deploy`` creates a *standalone*
Apps Script project (the runtime installer's Web App). This service
creates a *container-bound* project — ``projects.create`` with a
``parentId`` pointing at a Doc / Sheet / Slides file. Binding is what
lets the generated script install custom menus (``Ui.createMenu``),
sidebars (``HtmlService``), ``onEdit`` simple triggers, and custom
Sheets functions that live *inside* that specific Workspace file. The
scope set is identical; the difference is the ``parentId`` on create.

``drive.file`` is also implicitly needed — ``projects.create`` writes
a Drive file (the script project IS a Drive file) and
``auto_detect_container_kind`` reads the container's ``mimeType`` via
the Drive API. ``drive.file`` is in the baseline too; not re-declared
here since the container-detection read uses the broader Drive grant
the runtime already holds (same as ``gas_deploy``'s note).
"""

# https://developers.google.com/apps-script/api/concepts/scopes
# Both already present in auth.SCOPES (PR #125) — no second-consent.
GAS_BOUND_SCOPES = [
    # Create + update the bound Apps Script project's content.
    "https://www.googleapis.com/auth/script.projects",
    # Cut a version + create a deployment of the bound project.
    "https://www.googleapis.com/auth/script.deployments",
]
