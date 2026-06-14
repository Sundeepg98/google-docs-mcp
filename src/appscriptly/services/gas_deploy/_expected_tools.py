"""Declared tool surface for the gas_deploy service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale.

The Workspace-Automation installer has been renamed twice and keeps ALL
prior names registered as deprecation aliases (one underlying installer,
three registrations):
  ``gdocs_setup_apps_script`` (original)
    → ``gdocs_install_automation`` (PR-α reframe)
    → ``as_install_automation`` (chore/tool-namespace-cleanup canonical).
All three are declared here. Planned removal of the two aliases in v3.0.
No NEW ``gdocs_`` alias was minted in the cleanup — ``gdocs_setup_apps_script``
is the pre-existing legacy alias kept as-is.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "as_install_automation",      # canonical (namespace cleanup)
    "gdocs_install_automation",   # deprecated alias (PR-α name; removal v3.0)
    "gdocs_setup_apps_script",    # deprecated alias (original; removal v3.0)
    # ROADMAP 59 — deploy a standalone doGet/doPost project as a Web App
    # (webhook / HTTP endpoint). Reuses AppsScriptClient; as_* prefix.
    "as_deploy_web_app",
})
