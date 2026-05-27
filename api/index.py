"""Vercel Python ASGI entrypoint (PR-Δ6 — Vercel pilot).

Vercel's Python runtime discovers handlers at ``api/<name>.py``;
this file exports the FastMCP-built Starlette ASGI app as ``app``,
which is the convention the Vercel Python runtime auto-detects
(matches FastAPI / Starlette / Quart conventions).

Module-load order matters on a Vercel cold start:

  1. Vercel's Python runtime imports this file.
  2. ``init_default_backend_from_env()`` is called BEFORE the FastMCP
     app is built — so the backend is correctly set to VercelKvBackend
     (assuming ``STORAGE_BACKEND=vercel_kv`` env var + the two KV REST
     env vars are bound to the project). Any tool that touches user
     state inside the FastMCP setup chain sees the right backend
     from the first call.
  3. ``configure_auth_for_http(mcp)`` wires the GoogleProvider OAuth
     surface — mirrors what ``server.main()`` does for Fly's
     ``uvicorn.run`` path. Without this call, ``mcp.auth`` stays None
     and HTTP requests can't resolve a user context.
  4. ``build_app(mcp)`` composes the full Starlette app — routes +
     middleware + the FastMCP transport mount at /mcp.

The exported ``app`` is the live ASGI callable Vercel's serverless
function adapter invokes per request.

Statelessness considerations on Vercel:

- **Module-level state resets per cold start.** Lazy in-process
  caches (``_creds_cache`` in _tool_helpers, key call counters in
  keys.py, ``_initialized_paths`` in user_store) all reset when
  Vercel spins up a fresh container. Acceptable: per-call user
  state lives in VercelKvBackend (durable); caches are
  observability + cost optimization, both fine to lose.

- **No on-disk persistence beyond /tmp.** Vercel's tmpfs dies with
  the container. SqliteBackend would silently lose every write —
  hence the mandatory VercelKvBackend selection for the Vercel
  deploy. The backend selector's fail-soft path (warn + fall back
  to SqliteBackend) is intentionally a degraded-mode signal, not
  a supported production state.

- **Max execution time: 60s** (Hobby tier per ``vercel.json``).
  Most tools finish well inside that; tools that approach the
  bound (the docx-import → Apps Script Web App round-trip for
  large documents) will time out. PR-Δ3.5's retry adapter doesn't
  help here — these are inherently long operations. If/when this
  becomes a real operator concern, the answer is the operator
  upgrades to Vercel Pro (300s max) OR the tool is re-architected
  into a background-job pattern. Out of scope for the pilot.

- **Cold-start cost**: ~1-3s on the first request after idle.
  Subsequent warm requests are ~10-50ms overhead. Acceptable for
  the Hobby tier; PR-Δ4's request-ID middleware makes cold-start
  latencies visible in logs.
"""
from __future__ import annotations

import logging

# Configure logging BEFORE any module imports that emit at import
# time. Vercel captures stdout/stderr as the function log surface;
# basicConfig here matches what ``server.run_http`` does for Fly.
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s %(levelname)s [req=%(request_id)s] "
        "%(name)s %(message)s"
    ),
)

# Initialize Sentry (PR-Δ4) BEFORE the rest of the imports so any
# import-time exception is captured. ``init_sentry()`` is a no-op
# when SENTRY_DSN is unset.
from google_docs_mcp.observability import init_sentry  # noqa: E402

init_sentry()

# Now bring up the user-store backend. Doing this BEFORE the FastMCP
# instance / tool registrations import means the first tool call sees
# the resolved backend; doing it AFTER could let an import-time
# user_store access (none exist today, but defense-in-depth) hit the
# default SqliteBackend.
from google_docs_mcp.user_store import init_default_backend_from_env  # noqa: E402

init_default_backend_from_env()

# Import the FastMCP instance + the build_app composer. The
# side-effect tool-registration imports run at module-load time
# (per the M3 Phase A/B/C/admin/sheets/slides/drive bottom-of-
# server.py imports). When this api/index.py module loads, every
# tool registers; the resulting mcp instance is what build_app
# wraps.
from google_docs_mcp.server import mcp  # noqa: E402
from google_docs_mcp.oauth_google import configure_auth_for_http  # noqa: E402
from google_docs_mcp.http_server.app import build_app  # noqa: E402
from google_docs_mcp.http_server.middleware import RequestIdLogFilter  # noqa: E402

# Install the request-ID log filter on the root logger so every
# log line inside an HTTP request handler carries the active
# request_id. Mirrors what ``server.run_http`` does for Fly.
logging.getLogger().addFilter(RequestIdLogFilter())

# Wire the FastMCP GoogleProvider for HTTP-mode per-user auth.
# Idempotent — if mcp.auth is already set (shouldn't be at this
# point in cold-start, but defense-in-depth), this returns early.
configure_auth_for_http(mcp)

# Export the ASGI app. Vercel's Python runtime auto-discovers the
# module-level ``app`` symbol and adapts it to its serverless
# function invocation contract.
app = build_app(mcp)
