"""``services/apps_script`` — generic bound-Apps-Script generator (PR-Δ7).

The feature *foundation*. ``as_generate_bound_script`` is the generic
primitive that future feature PRs (slides-for-video, sheets dashboards,
docs menus) all build on: it generates a container-bound Apps Script
project, pushes a ``.gs`` body + manifest, and deploys it — so the
automation lives in the user's Workspace and runs without Claude in the
loop after one deploy.

Layout (mirrors the smallest recent services — sheets / slides):

    services/apps_script/
    ├── __init__.py   — this file (re-exports GAS_BOUND_SCOPES)
    ├── scopes.py     — GAS_BOUND_SCOPES (already in baseline; no 2nd consent)
    ├── api.py        — pure logic + Apps Script REST calls (no decorators)
    └── tools.py      — the @workspace_tool MCP tool (registered via
                        server.py's side-effect import)

**Distinct from ``services/gas_deploy``.** ``gas_deploy`` bootstraps ONE
*standalone* runtime Web App per user (for the lossless-retrofit path).
This service creates a NEW *bound* project per container — that binding
is what unlocks per-file custom menus, sidebars, and edit triggers.
Same Apps Script REST API; different purpose; no duplication. See
``api.py``'s module docstring and the ADR
(``docs/adr/2026-05-28-bound-script-generator.md``).

Re-export discipline: like ``gas_deploy/__init__.py`` (which re-exports
``GAS_DEPLOY_SCOPES``), this re-exports ``GAS_BOUND_SCOPES`` so callers
can ``from google_docs_mcp.services.apps_script import GAS_BOUND_SCOPES``.
Tool REGISTRATION is NOT triggered here — it happens via server.py's
``from .services.apps_script import tools`` side-effect import, matching
the sheets / slides pattern (whose ``__init__.py`` files are pure
docstrings). Importing this package does not import ``tools`` (and thus
does not reach into the live ``mcp`` instance), keeping the import graph
acyclic.
"""
from .scopes import GAS_BOUND_SCOPES

__all__ = ["GAS_BOUND_SCOPES"]
