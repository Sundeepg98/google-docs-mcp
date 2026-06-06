"""``services/gas_deploy`` — Apps Script project deployment plumbing.

Generic Apps Script REST wrapper — create/push/version/deploy any
Apps Script Web App. Knows nothing about ``restructure.gs`` or other
google-docs-mcp specifics.

**M3 Phase C (v2.1.5):** moved from top-level ``gas_deploy/`` into
``services/gas_deploy/`` to match the docs + drive per-service folder
pattern, and ``client.py`` renamed to ``api.py`` for symmetry. The
``scopes.py`` module stays as a separate file (it's a public constant
list, not API plumbing).

Layout:

- ``api.py``    — ``AppsScriptClient`` + ``WebAppDeployment`` (relocated
                  from ``gas_deploy/client.py`` in Phase C).
- ``scopes.py`` — ``GAS_DEPLOY_SCOPES`` constant for OAuth provisioning.
- ``tools.py``  — ``@workspace_tool``-decorated MCP tool functions
                  for the Workspace automation runtime installer.
                  PR-α (v2.3.4): registers both
                  ``gdocs_install_automation`` (canonical) and
                  ``gdocs_setup_apps_script`` (deprecation alias).
                  Imported explicitly from ``server.py`` AFTER the
                  ``mcp`` instance is constructed.

Historical note: pre-Phase-C, the original ``gas_deploy/__init__.py``
claimed "zero imports from appscriptly" as a portability goal.
That claim was already stale before Phase C — ``client.py`` imported
``appscriptly.google_clients.get_service`` (the wrapper chokepoint
from PR #75). Phase C accepts the integration explicitly; the previous
"git mv to a standalone repo" path is no longer the architectural plan.
"""
from .api import AppsScriptClient, WebAppDeployment
from .scopes import GAS_DEPLOY_SCOPES

__all__ = [
    "AppsScriptClient",
    "GAS_DEPLOY_SCOPES",
    "WebAppDeployment",
]
