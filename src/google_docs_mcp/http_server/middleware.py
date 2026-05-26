"""ASGI middlewares + trusted-hosts derivation.

Three concerns:
  - ``BearerTokenMiddleware`` — bearer / signed-URL auth gate for /api/*.
  - ``BodySizeLimitMiddleware`` — Content-Length cap (defense-in-depth).
  - ``derive_trusted_hosts()`` — host-allowlist resolution for the
    ``TrustedHostMiddleware`` Starlette ships.

``TrustedHostMiddleware`` itself comes from Starlette; we only
configure its ``allowed_hosts`` here.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from google_docs_mcp.crypto import verify_signed_params
from . import _state  # late-bound access to _state._NONCE_STORE so test
                      # reassignments propagate (tests reset between cases)

log = logging.getLogger("google_docs_mcp.http")


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Bearer-token auth gate for the REST ``/api/*`` endpoints only.

    Token is set via ``MCP_BEARER_TOKEN`` env var.

    Scope of protection (intentionally narrow):
    - ``/api/*`` (currently just ``/api/convert``) — bearer required.
      Cloud chat's Python sandbox calls these and can trivially set
      the Authorization header.

    Everything else — ``/health``, ``/mcp``, ``/mcp/*``,
    ``/.well-known/*``, ``/register`` — is intentionally open:
    - ``/health`` is the Fly.io liveness probe.
    - ``/mcp*`` and the OAuth-discovery endpoints (``/.well-known/...``,
      ``/register``) need to be reachable without auth so claude.ai's
      custom-connector setup can probe them. Claude.ai's connector
      framework speaks MCP+OAuth, not bearer tokens; returning 401
      from these confuses the discovery flow (claude.ai shows
      "Couldn't reach the MCP server"). Until we wire FastMCP's
      OAuthProxy, /mcp/* relies on URL secrecy.
    """

    def __init__(
        self,
        app: Any,
        *,
        bearer_token: bytes,
        signed_url_key: bytes,
    ) -> None:
        """v2.6 (#48): two separate keys instead of one bearer-token-as-both.

        Pre-v2.6 the same env var (``MCP_BEARER_TOKEN``) served BOTH purposes
        verbatim — bearer-header equality AND signed-URL HMAC. v2.0b's
        strict-flip wants the two HKDF-derived separately so leaking the
        bearer to a log doesn't compromise signed URLs (and vice versa).
        ``build_app`` now resolves each via ``keys.get_key("api_bearer")``
        and ``keys.get_key("signed_url")``. The shim window (v1.5 - v2.0b)
        keeps both returning the raw master so existing in-flight URLs +
        bearer tokens continue to work; v2.0b's flip activates the split.

        v2.0b: parameters are ``bytes`` (matches ``keys.get_key()``'s
        return type). The bearer-header equality path now compares the
        request's ``Authorization`` header value as bytes via
        ``hmac.compare_digest`` rather than building an f-string from
        ``bytes`` (which would format as the literal ``"b'...'"`` and
        never match). Per RUNBOOK §3.6, operators are expected to set
        ``MCP_API_BEARER_KEY`` to a UTF-8 string before flipping, so
        the bytes from ``get_key("api_bearer")`` are operator-chosen
        UTF-8; HTTP clients send the same string in the
        ``Authorization: Bearer <value>`` header and the byte
        comparison matches. If the operator skips the override and
        relies on HKDF, ``get_key("api_bearer")`` returns 32 random
        bytes; the operator must compute the same bytes client-side
        (e.g. via the same HKDF derivation) and submit them as the
        header value — RUNBOOK §3.6 covers this.

        Reviewers: the two parameters MUST come from separate ``get_key()``
        calls (not the same value reused), even today during the shim.
        That preserves the call-site discipline so the v2.0b flip is a
        pure-config change in keys.py, not a re-edit of this middleware.
        """
        super().__init__(app)
        # Precompute the expected ``Authorization`` header value as bytes.
        # HTTP/1.1 mandates ASCII in the header line so concatenating the
        # ASCII prefix b"Bearer " with the bearer-token bytes yields the
        # exact byte sequence a conformant client will send; we compare
        # via ``hmac.compare_digest`` for constant-time equality.
        self._expected_bytes: bytes = b"Bearer " + bearer_token
        self._signing_key: bytes = signed_url_key

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # v2.6 (#48): /info is the curl-friendly observability surface
        # the preflight script hits (replaces a broken `fastmcp client`
        # invocation auth-auditor caught pre-merge — fastmcp 3.3.1 has
        # no `client` subcommand). Same bearer-or-signed-URL gate as
        # /api/* so the existing auth discipline applies; no separate
        # auth path.
        path = request.url.path
        if not (path.startswith("/api/") or path == "/info"):
            return await call_next(request)

        # Auth path 1: bearer header (legacy / direct API callers).
        # v2.0b: compare as bytes via hmac.compare_digest. The pre-flip
        # f-string equality assumed bearer_token was str; HKDF returns
        # bytes that aren't UTF-8 in general, so the f-string path
        # broke. ``provided`` is str (Starlette decodes headers as ISO-
        # 8859-1 / latin-1 internally then exposes str); encode to
        # latin-1 to round-trip the same byte sequence the client sent.
        provided_str = request.headers.get("authorization", "")
        provided_bytes = provided_str.encode("latin-1")
        if hmac.compare_digest(provided_bytes, self._expected_bytes):
            return await call_next(request)

        # Auth path 2: signed-URL query string (cloud-chat sandbox).
        # If all four signed-URL params are present, validate the HMAC
        # and consume the nonce here so the endpoint sees an already-
        # authorized request. Bearer-token fallback still wins if both
        # are present and the header matches.
        qp = request.query_params
        if all(k in qp for k in ("exp", "nonce", "max", "sig")):
            # v2.1: ``uid`` is required. verify_signed_params returns the
            # validated user_id; stash it on request.state so the
            # downstream handler can resolve per-user creds without
            # re-parsing the query string.
            ok, err, _max, user_id = verify_signed_params(
                signing_key=self._signing_key,
                exp=qp["exp"],
                nonce=qp["nonce"],
                max_bytes=qp["max"],
                sig=qp["sig"],
                user_id=qp.get("uid"),
                nonce_store=_state._NONCE_STORE,
            )
            if ok:
                request.state.signed_url_user_id = user_id
                return await call_next(request)
            return JSONResponse(
                {"error": f"signed URL rejected: {err}"}, status_code=401
            )

        return JSONResponse(
            {"error": "missing or invalid credentials (bearer header or signed URL)"},
            status_code=401,
        )


# ---------------------------------------------------------------------------
# Host + body-size middleware (v1.3.1 security hardening)
# ---------------------------------------------------------------------------


def derive_trusted_hosts() -> list[str]:
    """Resolve TrustedHost allowlist from env, fail-open with WARN in dev.

    Priority:
      1. ``TRUSTED_HOSTS`` env var (comma-separated) — explicit override.
      2. ``FLY_APP_NAME`` -> derive ``<app>.fly.dev`` + ``*.<app>.fly.dev``
         + ``localhost`` + Fly's internal probe hostnames.
      3. Fail-open ``["*"]`` with WARNING log.

    Production safety: refuses to start if ``FLY_REGION`` is set (machine
    is running on Fly infra) but ``FLY_APP_NAME`` is absent — that's the
    silent fail-open path round-20 adversarial review identified.

    **v2.0.6 — Fly internal probe allowlist.** Fly's deploy health gate
    probes ``GET /health`` from an internal address whose Host header
    does NOT match ``<app>.fly.dev``. Without ``<app>.internal`` and the
    ``*.internal`` machine-id pattern in the allowlist, the probe gets
    a 400 from TrustedHostMiddleware before reaching the handler, the
    health gate fails, and Fly aborts the deploy (rolling back to the
    previous machine). Verified against 5 consecutive failed deploys
    (v78-v82) where ``/health`` returned 200 to external requests but
    400 to Fly's internal probe at ``172.19.24.105``.
    """
    explicit = os.environ.get("TRUSTED_HOSTS", "").strip()
    if explicit:
        return [h.strip() for h in explicit.split(",") if h.strip()]

    app_name = os.environ.get("FLY_APP_NAME", "").strip()
    fly_region = os.environ.get("FLY_REGION", "").strip()

    if fly_region and not app_name:
        # Refuse to fail-open when we're clearly on Fly infra.
        raise RuntimeError(
            "FLY_REGION is set (running on Fly infra) but FLY_APP_NAME "
            "is unset — refusing to fail-open TrustedHost. Set "
            "TRUSTED_HOSTS explicitly or restore FLY_APP_NAME. See "
            "v1.3.1 release notes for context."
        )

    if app_name:
        return [
            f"{app_name}.fly.dev",
            f"*.{app_name}.fly.dev",
            "localhost",
            # Fly's internal health-check + service-discovery hostnames.
            # See v2.0.6 docstring above for the deploy-blocker context.
            f"{app_name}.internal",
            "*.internal",  # covers machine-id.vm.<app>.internal probes
        ]

    log.warning(
        "TrustedHost middleware running fail-open (allow all hosts) -- "
        "neither TRUSTED_HOSTS nor FLY_APP_NAME set. Acceptable for "
        "local dev only."
    )
    return ["*"]


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Enforce a hard cap on declared Content-Length.

    Fast path only -- checks the header before any body is read.
    Chunked uploads / missing Content-Length fall through to the
    endpoint's own enforcement (e.g., Starlette's ``request.form()``
    per-part cap). Defense-in-depth, not a single line of defense.
    """

    def __init__(self, app: Any, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError:
                return JSONResponse(
                    {"error": "invalid Content-Length"},
                    status_code=400,
                )
            if declared > self.max_bytes:
                return JSONResponse(
                    {"error": "payload too large", "max_bytes": self.max_bytes},
                    status_code=413,
                )
        return await call_next(request)
