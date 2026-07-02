"""Observability endpoints — Fly.io health probe + curl-friendly /info
+ RFC 9728 OAuth protected resource metadata + RFC 9116 security.txt."""
from __future__ import annotations

import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from appscriptly import keys


async def health(_request: Request) -> JSONResponse:
    # PR-Δ5.5 (2026-05-27): the ``service`` field carries the
    # user-facing product name (``appscriptly`` post-rename) rather
    # than the package distribution name or module path. Operators
    # have monitoring + log-aggregation rules that may grep this
    # field; the rename moves the canonical identifier forward
    # without affecting Fly's health-probe contract (which only
    # cares about the HTTP 200 + JSON-parseable body).
    #
    # Deploy-standard hardening (2026-07-02): ``git_commit`` stamps the
    # deployed short SHA into the PUBLIC health payload so "what commit
    # is prod serving" is answerable with an unauthenticated curl (the
    # bearer-authed /info endpoint already exposed it, but the drift
    # monitor and the deploy smoke check need a public read). Provenance
    # chain: deploy.yml passes GIT_COMMIT as a --build-arg, the
    # Dockerfile bakes it via ARG GIT_COMMIT=unknown + ENV GIT_COMMIT,
    # and this handler reads the env at request time (same sourcing as
    # /info). A local run without the env reports "unknown". A short SHA
    # of a public repo leaks nothing.
    return JSONResponse({
        "ok": True,
        "service": "appscriptly",
        "git_commit": os.environ.get("GIT_COMMIT", "unknown"),
    })


# RFC 9116 §2.3 recommends an expiry no more than 1 year out. We hardcode
# 2027-01-01 here as a deliberately conservative ~6-month window so a
# stale deployment of an old image still serves a non-expired contact
# block. Re-bump in CHANGELOG on each tag; the integration test
# `test_security_txt_expires_is_rfc3339_and_in_future` is the canary.
_SECURITY_TXT_EXPIRES = "2027-01-01T00:00:00Z"

_SECURITY_TXT_BODY = f"""\
# RFC 9116 security.txt for google-docs-mcp
# Machine-readable companion to /SECURITY.md + docs/THREAT_MODEL.md
# in the source repo.

Contact: https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new
Expires: {_SECURITY_TXT_EXPIRES}
Preferred-Languages: en
Canonical: https://sundeepg98-docs-mcp.fly.dev/.well-known/security.txt
Policy: https://github.com/Sundeepg98/google-docs-mcp/blob/main/SECURITY.md
Acknowledgments: https://github.com/Sundeepg98/google-docs-mcp/security/advisories
"""


async def security_txt(_request: Request) -> PlainTextResponse:
    """``GET /.well-known/security.txt`` — RFC 9116 machine-readable
    vulnerability disclosure contact.

    Companion to the human-readable ``SECURITY.md`` at the repo root.
    Spec mandates ``Contact:`` and ``Expires:``; the rest are
    recommended fields that materially help reporters. Public endpoint
    (the ``BearerTokenMiddleware`` already excludes ``/.well-known/*``).

    The ``Expires:`` value is hardcoded — see ``_SECURITY_TXT_EXPIRES``
    above for the renewal protocol. A stale deployment of an old image
    still serves a non-expired block thanks to the conservative window.
    """
    return PlainTextResponse(
        _SECURITY_TXT_BODY,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    """``GET /.well-known/oauth-protected-resource`` — RFC 9728 metadata.

    The MCP Authorization spec MANDATES this endpoint for any MCP
    server that exposes OAuth-protected resources. Previously absent
    (returned 404, breaking spec-conformant claude.ai connector
    discovery probes). Added in PR-Δ1 (v2.3.4).

    The companion RFC 8414 endpoint (``/.well-known/
    oauth-authorization-server``) is wired automatically by FastMCP's
    ``GoogleProvider`` (see ``oauth_google.configure_auth_for_http``).
    This endpoint is the OTHER half: 8414 describes the
    authorization server; 9728 describes THIS resource server and
    points at the authorization server(s) it trusts.

    Response shape per RFC 9728 §3 (Protected Resource Metadata):

    - ``resource``: the URL identifying this resource server (our
      MCP endpoint, the protected resource itself).
    - ``authorization_servers``: list of URLs of authorization
      servers that can mint tokens for this resource. In our case
      the SAME base URL, because ``GoogleProvider`` exposes the
      8414 endpoint on this server (it's a proxy in front of
      Google).
    - ``scopes_supported``: list of OAuth scope strings the
      resource may demand. Sourced from
      ``oauth_google.GOOGLE_API_SCOPES`` so additions / removals
      stay in sync without a duplicate registry.
    - ``bearer_methods_supported``: ``["header"]`` — Authorization
      header is the only token presentation we accept (we don't
      take tokens in query strings or POST bodies, that's
      RFC 6750 §2.2/2.3 surfaces we deliberately don't support).
    - ``resource_documentation``: pointer to user-facing docs (the
      MCP root, which describes what the resource does + how to
      use it).

    Public endpoint — like ``/.well-known/oauth-authorization-server``,
    no bearer required (claude.ai's discovery probes it without
    any credential). The ``BearerTokenMiddleware`` already excludes
    ``/.well-known/*`` from auth enforcement.
    """
    # Local import avoids a circular at module load time (the
    # http_server.app imports from this module, and the GOOGLE_API_SCOPES
    # comes through oauth_google which also pulls in the lazy
    # server.py auth wiring chain).
    from appscriptly.http_server._helpers import _resolve_base_url
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    base_url = _resolve_base_url(request)
    return JSONResponse({
        "resource": base_url,
        "authorization_servers": [base_url],
        # Sorted for stable output across deploys — easier to diff
        # in claude.ai connector audit logs.
        "scopes_supported": sorted(GOOGLE_API_SCOPES),
        "bearer_methods_supported": ["header"],
        # MCP root has its own description / instructions surface; for
        # the resource-documentation link we point at the public base
        # so administrators can crawl the human-facing docs from there.
        "resource_documentation": base_url,
    })


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
    from appscriptly import __version__ as _pkg_version

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
