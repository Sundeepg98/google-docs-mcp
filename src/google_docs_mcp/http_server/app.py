"""ASGI application composition: routes + middleware + lifespan.

This module is the only place that knows about the public route layout
of the HTTP surface. All handler logic lives in ``routes/*``; all
middleware logic in ``middleware``. ``build_app`` wires them together
and ``run_http`` is the uvicorn entrypoint for ``server.main()``.
"""
from __future__ import annotations

import logging
import os

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route

from google_docs_mcp import keys
from google_docs_mcp.oauth_google import CALLBACK_PATH
from .middleware import (
    BearerTokenMiddleware,
    BodySizeLimitMiddleware,
    HealthExemptTrustedHostMiddleware,
    derive_trusted_hosts,
)
from .routes.convert import convert_endpoint
from .routes.oauth import oauth_google_api_callback
from .routes.observability import (
    health,
    info_endpoint,
    oauth_protected_resource_metadata,
)

log = logging.getLogger("google_docs_mcp.http")


def build_app(mcp: FastMCP) -> Starlette:
    """Compose the public HTTP surface: REST routes + MCP HTTP transport."""
    # v2.6 (#48): the DUAL site — bearer header auth AND signed-URL HMAC
    # used the same env var read directly. Now routed through
    # keys.get_key() so the v2.0b strict-flip can HKDF-derive them
    # separately. During the shim window (v1.5 - v2.0b) both still resolve
    # to MCP_BEARER_TOKEN verbatim; the strict-flip activates the split.
    # v2.0b: keys.get_key() returns bytes; pass through to the middleware
    # which compares ``Authorization`` header bytes directly via
    # ``hmac.compare_digest`` (was an f-string equality on str pre-flip,
    # which crashed on HKDF output). Both keys are bytes end-to-end now.
    try:
        bearer_token = keys.get_key("api_bearer")
        signed_url_key = keys.get_key("signed_url")
    except RuntimeError as e:
        # Preserve the historical "must be set when running in HTTP mode"
        # message that operators have grepped for since v1.0.
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var must be set when running in HTTP "
            "mode. Generate a long random string and set it on Fly.io "
            "with `fly secrets set MCP_BEARER_TOKEN=...`."
        ) from e

    # v1.3.1: middleware order is OUTERMOST first per Starlette semantics.
    # TrustedHost first (cheap header check; rejects bad-host requests
    # before any body is read). Bearer second (rejects unauthenticated
    # /api/* without paying the body-size cost). BodySize last (defense-
    # in-depth Content-Length cap; per-endpoint multipart caps live on
    # the endpoints themselves).
    trusted_hosts = derive_trusted_hosts()
    body_max = int(os.environ.get("MCP_BODY_MAX_BYTES", 10 * 1024 * 1024))
    middleware = [
        # v2.3.3: HealthExemptTrustedHostMiddleware bypasses Host
        # validation on /health so Fly's internal probe (which sends
        # Host: <raw-ip>) can reach the handler. See the middleware's
        # docstring for the v89 deploy-log evidence.
        Middleware(
            HealthExemptTrustedHostMiddleware,
            allowed_hosts=trusted_hosts,
        ),
        Middleware(
            BearerTokenMiddleware,
            bearer_token=bearer_token,
            signed_url_key=signed_url_key,
        ),
        Middleware(BodySizeLimitMiddleware, max_bytes=body_max),
    ]

    # FastMCP 3.x quirk: the StreamableHTTPSessionManager inside
    # mcp.http_app() needs its lifespan to run on the PARENT ASGI app
    # too — otherwise the first POST to /mcp errors with
    # "task group was not initialized". So we build the mcp sub-app
    # once and hand its lifespan up to Starlette.
    #
    # Endpoint shape: we set ``path="/mcp"`` on FastMCP and mount at
    # ``/``. This makes ``POST /mcp`` (no trailing slash) the canonical
    # endpoint — claude.ai's custom-connector client hits ``/mcp``
    # exactly and chokes on Starlette's auto 307 redirect to ``/mcp/``
    # when the FastMCP sub-app is mounted at ``/mcp`` instead.
    mcp_app = mcp.http_app(path="/mcp")

    routes = [
        Route("/health", health, methods=["GET"]),
        # v2.6 (#48): /info is the curl-friendly observability surface
        # for the v2.0b preflight script. Bearer-authed via the same
        # BearerTokenMiddleware dispatch path as /api/*.
        Route("/info", info_endpoint, methods=["GET"]),
        Route("/api/convert", convert_endpoint, methods=["POST"]),
        # PR-Δ1 (v2.3.4): RFC 9728 OAuth Protected Resource Metadata.
        # The companion RFC 8414 endpoint is wired by FastMCP's
        # GoogleProvider; this is the OTHER MCP-Authorization-
        # spec-mandated discovery endpoint. Public by design (claude.ai
        # connector discovery probes it without any credential); the
        # BearerTokenMiddleware already excludes /.well-known/*.
        # MUST be declared BEFORE the catch-all Mount("/", ...) so
        # Starlette resolves it before falling through to the FastMCP
        # sub-app's 404.
        Route(
            "/.well-known/oauth-protected-resource",
            oauth_protected_resource_metadata,
            methods=["GET"],
        ),
        # OAuth callback for the v1.1+ per-user Google API auth. Public
        # by design (browser hits it after Google redirect, no bearer
        # token available). Security via HMAC-signed state + single-use
        # nonce store, not via header auth.
        Route(CALLBACK_PATH, oauth_google_api_callback, methods=["GET"]),
        # FastMCP at root mount, with its endpoint at /mcp internally.
        # /mcp (no slash) is the canonical endpoint claude.ai uses.
        Mount("/", app=mcp_app),
    ]

    return Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=mcp_app.lifespan,
    )


def run_http(mcp: FastMCP, *, port: int = 8080) -> None:
    """Boot the HTTP server on ``0.0.0.0:<port>``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = build_app(mcp)
    log.info("starting HTTP MCP server on 0.0.0.0:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
