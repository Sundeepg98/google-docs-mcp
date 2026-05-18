"""Apps Script project deployment plumbing.

GENERIC. Knows how to create/push/version/deploy any Apps Script
Web App via Google's Apps Script REST API. Does NOT know about
restructure.gs or anything else specific to google-docs-mcp.

The intent — see ADR-style note in the README "Architecture" section —
is that this sub-package has zero imports from the rest of
google_docs_mcp. If/when a second project ever needs Apps Script
project lifecycle management, this folder can be `git mv`'d to a
standalone repo and published as ``gas-deploy`` on PyPI in one
afternoon. Until then, it lives here as a clean module boundary.
"""
from .client import AppsScriptClient, WebAppDeployment
from .scopes import GAS_DEPLOY_SCOPES

__all__ = [
    "AppsScriptClient",
    "GAS_DEPLOY_SCOPES",
    "WebAppDeployment",
]
