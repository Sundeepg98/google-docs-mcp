"""Remote HTTP transport for v0.9.0 / Wave D.

Exposes the MCP server over HTTP for cloud-chat workflows where the
caller can't reach the user's local machine. Two endpoints:

- ``GET /health`` -> ``{"ok": true}`` for Fly.io health checks
- ``POST /api/convert`` -> thin REST wrapper around
  ``convert_docx_to_tabbed_doc``. Accepts ``multipart/form-data`` with a
  ``file`` field (the .docx bytes) plus form fields for ``split_by``,
  ``title``. Returns the same JSON shape as the MCP tool. This is what
  Claude.ai cloud chat's Python sandbox calls.
- ``/mcp/*`` -> proper MCP streamable-HTTP transport, for any future
  Claude surface that consumes MCP-over-HTTP directly.

Auth: a single ``MCP_BEARER_TOKEN`` env var. All protected endpoints
require ``Authorization: Bearer <token>``. Health check is public for
Fly.io probes.

**Internal structure (v2.2.1 — Gap #8 mechanical split):**

::

    http_server/
    ├── __init__.py         # this file — re-exports the public surface
    ├── _state.py           # process-wide _NONCE_STORE
    ├── _helpers.py         # _resolve_client_config, _resolve_base_url
    ├── _pages.py           # OAuth callback HTML + CSP
    ├── middleware.py       # BearerTokenMiddleware, BodySizeLimitMiddleware
    ├── app.py              # build_app, run_http (composition)
    └── routes/
        ├── observability.py    # health, info_endpoint
        ├── oauth.py            # oauth_google_api_callback
        └── convert.py          # convert_endpoint

Pre-v2.2.1 every concern lived in a single 830-LOC ``http_server.py``.
The split is mechanical — same functions, same behavior, no new
abstractions — to bring the file's SRP debt in line with what the
M3 refactor cleared for ``server.py``. Per ARCHITECTURE.md §5, files
exceeding ~400 LOC get split into sub-modules along concern lines.

Public names are re-exported here so external callers (server.py,
tests) keep working with ``from google_docs_mcp.http_server import X``.
Tests that ``patch("google_docs_mcp.http_server.X", ...)`` for
``X`` defined in a submodule must patch at the submodule path instead
(e.g. ``http_server.routes.convert.get_credentials_for_user``) —
this is the standard mock-patching rule and applies whenever code
moves into a sub-package.
"""
from __future__ import annotations

# --- Composition ---
from .app import build_app, run_http

# --- Middleware ---
from .middleware import (
    BearerTokenMiddleware,
    BodySizeLimitMiddleware,
    HealthExemptTrustedHostMiddleware,
    derive_trusted_hosts,
)

# --- Route handlers ---
from .routes.convert import convert_endpoint
from .routes.oauth import oauth_google_api_callback
from .routes.observability import health, info_endpoint

# --- Page helpers (security-sensitive HTML; tested directly) ---
from ._pages import _CSP_HEADER, _OAUTH_SUCCESS_HTML, _error_page, _success_page

# --- Shared resolution helpers (tested via integration tests) ---
from ._helpers import _resolve_base_url, _resolve_client_config

# --- Process-wide state ---
from ._state import _NONCE_STORE

__all__ = [
    # Composition + lifecycle
    "build_app",
    "run_http",
    # Route handlers (public — referenced by tests + server.py)
    "convert_endpoint",
    "health",
    "info_endpoint",
    "oauth_google_api_callback",
    # Middleware
    "BearerTokenMiddleware",
    "BodySizeLimitMiddleware",
    "HealthExemptTrustedHostMiddleware",
    "derive_trusted_hosts",
    # Page helpers (test surface)
    "_CSP_HEADER",
    "_OAUTH_SUCCESS_HTML",
    "_error_page",
    "_success_page",
    # Resolution helpers
    "_resolve_base_url",
    "_resolve_client_config",
    # Process-wide state
    "_NONCE_STORE",
]
