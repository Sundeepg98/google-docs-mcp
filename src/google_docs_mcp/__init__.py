"""appscriptly — MCP server for Apps Script automation across Google Workspace.

PR-Δ5.5 (2026-05-27): renamed from ``google-docs-mcp`` to ``appscriptly``
on the distribution surface (PyPI name, README, branding). The Python
module path is INTENTIONALLY kept at ``google_docs_mcp`` — see the
``[project]`` comment block in ``pyproject.toml`` and the ADR at
``docs/adr/2026-05-27-rename-to-appscriptly.md`` for the staged
rename plan.
"""
from .server import main

__version__ = "1.5.1"
__all__ = ["main"]
