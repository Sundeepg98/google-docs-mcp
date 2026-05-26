"""Observability endpoints — Fly.io health probe + curl-friendly /info."""
from __future__ import annotations

import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from ... import keys


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "google-docs-mcp"})


async def info_endpoint(_request: Request) -> JSONResponse:
    """``GET /info`` — bearer-authed observability endpoint (v2.6 #48).

    Mirrors a slice of ``gdocs_server_info``'s output that the v2.0b
    preflight script needs: shim hits, total calls, first-call ages
    per purpose, plus build provenance for log correlation. Curl-friendly
    JSON so ``scripts/preflight_strict_flip.sh`` can hit it without
    needing the ``fastmcp`` CLI (which dropped its ``client`` subcommand
    in 3.x — auth-auditor R5 pre-mortem caught the broken invocation
    before merge).

    Auth: same path as ``/api/*`` via ``BearerTokenMiddleware`` (the
    dispatch matcher includes ``/info``). 401 on missing/wrong bearer.

    Shape contract: the ``key_back_compat_shim_active_hits``,
    ``key_call_totals``, and ``key_observability`` keys are guaranteed
    by ``test_info_endpoint_response_matches_gdocs_server_info`` to
    match the MCP tool's output — drift between the two is a release
    blocker, not a quiet inconsistency.
    """
    # Local import avoids a circular at module load time
    # (server.py imports from this module; this would re-trigger that
    # import chain on http_server.py module load).
    from ... import __version__ as _pkg_version

    first_call_at = keys.get_first_call_timestamps()
    now = time.time()
    return JSONResponse({
        "version": _pkg_version,
        "git_commit": os.environ.get("GIT_COMMIT", "unknown"),
        "build_time": os.environ.get("BUILD_TIME", "unknown"),
        "key_back_compat_shim_active_hits": keys.get_shim_hit_counters(),
        "key_call_totals": keys.get_total_call_counters(),
        "key_observability": {
            "first_call_age_seconds": {
                purpose: (now - ts) if ts is not None else None
                for purpose, ts in first_call_at.items()
            },
        },
    })
