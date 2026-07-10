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

from appscriptly import keys
from appscriptly.oauth_google import CALLBACK_PATH
from .middleware import (
    BearerTokenMiddleware,
    BodySizeLimitMiddleware,
    HealthExemptTrustedHostMiddleware,
    LicenseKeyMiddleware,
    RequestIdLogFilter,
    RequestIdMiddleware,
    derive_trusted_hosts,
)
from .routes.convert import convert_endpoint, upload_frame_endpoint
from .routes.convert_status import convert_job_status_endpoint
from .routes.oauth import oauth_google_api_callback
from .routes.observability import (
    health,
    info_endpoint,
    oauth_protected_resource_metadata,
    security_txt,
)

log = logging.getLogger("appscriptly.http")


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
        # PR-Δ4: RequestIdMiddleware OUTERMOST so the request_id
        # ContextVar is populated BEFORE any other middleware runs.
        # Even auth-rejected (401) or Host-rejected (400) requests
        # still emit a correlatable log line on the way out.
        Middleware(RequestIdMiddleware),
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
        # PR-Δ5: LicenseKeyMiddleware AFTER bearer auth — same protected
        # surface (/api/* and /info), but checked second so an
        # unauthenticated request still gets a 401 (not a 402) for the
        # "you forgot the bearer" case. Default behavior: no-op
        # (LICENSE_KEY_ENFORCEMENT env var is off for personal use).
        # When commercial activation flips enforcement on, this gate
        # returns 402 Payment Required for missing/invalid keys.
        Middleware(LicenseKeyMiddleware),
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
        # T1.1 job model: poll a convert job minted by POST /api/convert
        # (async=1 / batch / burned-nonce attach). Auth is the pre-signed
        # multi-use status URL (or bearer), enforced in
        # BearerTokenMiddleware; see routes/convert_status.py.
        Route(
            "/api/convert/status/{job_id}",
            convert_job_status_endpoint,
            methods=["GET"],
        ),
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
        # PR-Δ2 (v2.3.5): RFC 9116 security.txt — machine-readable
        # vulnerability disclosure contact. Companion to /SECURITY.md
        # in the repo. Same placement rule as the RFC 9728 endpoint
        # above (MUST be declared BEFORE the Mount("/") catch-all).
        Route(
            "/.well-known/security.txt",
            security_txt,
            methods=["GET"],
        ),
        # OAuth callback for the v1.1+ per-user Google API auth. Public
        # by design (browser hits it after Google redirect, no bearer
        # token available). Security via HMAC-signed state + single-use
        # nonce store, not via header auth.
        Route(CALLBACK_PATH, oauth_google_api_callback, methods=["GET"]),
        # Base-tier slides->video frame handoff: the bound renderFrames()
        # script POSTs each rendered PNG here (authed by a signed batch
        # token), replacing the drive.readonly Drive round-trip. Public by
        # design (the HMAC token in the query string IS the credential;
        # BearerTokenMiddleware only gates /api/* + /info, so /upload/* is
        # already exempt). MUST be declared BEFORE the catch-all Mount("/")
        # below, or Starlette resolves /upload/frames/... into the FastMCP
        # sub-app and the POST 404s (the regression this route fixes).
        Route(
            "/upload/frames/{batch_id}/{index}",
            upload_frame_endpoint,
            methods=["POST"],
        ),
        # FastMCP at root mount, with its endpoint at /mcp internally.
        # /mcp (no slash) is the canonical endpoint claude.ai uses.
        Mount("/", app=mcp_app),
    ]

    return Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=mcp_app.lifespan,
    )


def configure_http_logging() -> None:
    """Root logging config with ``request_id`` stamped on every record.

    PR-Δ4 shipped the ``[req=%(request_id)s]`` format with
    ``RequestIdLogFilter`` attached to the root LOGGER. That wiring
    never covered records from CHILD loggers: logger-level filters run
    only on the logger a record is emitted on, and a record PROPAGATED
    to an ancestor skips the ancestor's logger-level filters entirely,
    going straight to its handlers. So every record from a named logger
    (startup, ``mcp.server`` CallToolRequest lines, the
    ``appscriptly.credentials`` tenant audit) reached the root handler
    without a ``request_id`` attribute, the formatter raised KeyError,
    and the logging machinery wrapped EVERY real line in a
    "--- Logging error ---" traceback block (BUG 4, 2026-07-09).

    Fix, two layers:
      1. the filter goes on the HANDLERS — handler-level filters run
         for every record the handler processes, propagated or not;
      2. ``Formatter(defaults=...)`` supplies ``request_id="-"`` for
         any future handler wired without the filter.

    Outside an HTTP request the placeholder ``-`` appears (the
    ContextVar default) — intentional, so operators can tell "no
    request in scope" from "request_id missing".
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [req=%(request_id)s] "
            "%(name)s %(message)s",
            defaults={"request_id": "-"},
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # basicConfig no-ops when the root logger already has handlers
    # (an embedding process may configure logging first) — stamp the
    # filter on every root handler either way so propagated records
    # always carry request_id.
    request_id_filter = RequestIdLogFilter()
    for root_handler in logging.getLogger().handlers:
        root_handler.addFilter(request_id_filter)


def run_http(mcp: FastMCP, *, port: int = 8080) -> None:
    """Boot the HTTP server on ``0.0.0.0:<port>``."""
    configure_http_logging()

    app = build_app(mcp)
    log.info("starting HTTP MCP server on 0.0.0.0:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
