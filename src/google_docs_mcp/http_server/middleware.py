"""ASGI middlewares + trusted-hosts derivation.

Five concerns:
  - ``RequestIdMiddleware`` — assign/propagate ``X-Request-ID`` per request
    (PR-Δ4 — correlation IDs for multi-tenant log debugging).
  - ``HealthExemptTrustedHostMiddleware`` — host-allowlist with /health bypass.
  - ``BearerTokenMiddleware`` — bearer / signed-URL auth gate for /api/*.
  - ``BodySizeLimitMiddleware`` — Content-Length cap (defense-in-depth).
  - ``derive_trusted_hosts()`` — host-allowlist resolution for the
    ``TrustedHostMiddleware`` Starlette ships.

Plus a logging filter (``RequestIdLogFilter``) + ContextVar
(``_request_id_var``) that together let every ``logging`` call inside
an HTTP request handler automatically include the active request_id.
"""
from __future__ import annotations

import contextvars
import hmac
import logging
import os
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from google_docs_mcp.crypto import verify_signed_params
from . import _state  # late-bound access to _state._NONCE_STORE so test
                      # reassignments propagate (tests reset between cases)

log = logging.getLogger("google_docs_mcp.http")


# ---------------------------------------------------------------------------
# Request-ID propagation (PR-Δ4 — multi-tenant log correlation)
# ---------------------------------------------------------------------------
#
# Multi-tenant debugging is the operational pain this closes. Pre-PR-Δ4 a
# log line saying "convert failed for user-A" was un-correlatable with
# upstream/downstream lines: was it the same request? a retry? a different
# user's concurrent call? With every line stamped ``request_id=<uuid>``,
# an operator scanning `flyctl logs` can grep one id and reconstruct the
# entire request lifecycle.
#
# Why a ContextVar (not request.state) for the canonical store:
#   1. ``logging.Filter`` can't reach ``request.state`` — it only sees the
#      LogRecord. A module-level ContextVar IS visible to the filter.
#   2. asyncio's contextvars.copy_context() means the value automatically
#      flows into every awaited coroutine inside the request handler
#      without manual plumbing.
#   3. Threads use the same ContextVar, but each thread gets its own
#      copy on entry — no leakage across concurrent requests.
#
# ``request.state.request_id`` is ALSO set so handlers that prefer the
# explicit attribute don't have to import this module.


_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "google_docs_mcp_request_id",
    default="-",
)


def get_request_id() -> str:
    """Return the request_id for the current async/thread context.

    Returns ``"-"`` (the ContextVar default) when called outside a
    request — e.g. from a stdio MCP tool or a background task — so
    log lines have a non-empty placeholder rather than ``None``.
    """
    return _request_id_var.get()


class RequestIdLogFilter(logging.Filter):
    """Inject the active ``request_id`` into every LogRecord.

    Install once per process at logging-config time (in ``run_http``).
    The filter is a no-op when called outside an HTTP request — the
    placeholder ``"-"`` is the default ContextVar value, NOT an error.

    Usage in a formatter::

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [req=%(request_id)s] "
            "%(name)s %(message)s"
        )
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Do not overwrite an explicit request_id the caller may have
        # set (e.g. a background task injecting a synthetic id).
        if not hasattr(record, "request_id"):
            record.request_id = _request_id_var.get()
        return True


# Header name per the de-facto convention. Echoed back to the client
# in the same casing so curl users grepping for X-Request-ID can match
# on either case.
REQUEST_ID_HEADER = "x-request-id"
# Cap on accepted inbound id length so a misbehaving upstream can't
# blow out log lines. UUIDs are 36 chars; we accept up to 128 to allow
# for prefixed conventions (e.g. ``cf-...`` from Cloudflare) without
# letting an attacker DoS log storage with multi-kilobyte ids.
_MAX_INBOUND_REQUEST_ID_LEN = 128


def _sanitize_inbound_request_id(value: str | None) -> str | None:
    """Accept an upstream id if it's reasonably-shaped; else None.

    Reasonable = ASCII, no control chars, no spaces, length ≤ 128.
    Anything else gets replaced by a fresh uuid4 — better to lose
    correlation with a misbehaving upstream than to log an
    attacker-controlled string verbatim.
    """
    if not value:
        return None
    if len(value) > _MAX_INBOUND_REQUEST_ID_LEN:
        return None
    # Reject control chars + spaces. Allow alphanumerics, dashes,
    # underscores, dots, and colons (UUIDs + Cloudflare/AWS conventions).
    if not all(c.isalnum() or c in "-_.:" for c in value):
        return None
    return value


class RequestIdMiddleware:
    """Assign/propagate ``X-Request-ID`` for every HTTP request.

    Pure-ASGI (not ``BaseHTTPMiddleware``-based) so it works for the
    full request lifecycle including streaming responses, and so it
    can wrap the ASGI ``send`` callable to inject the header in the
    response-start message without buffering the body.

    Sits outermost in the middleware stack so the id is populated
    BEFORE auth / Host / body-size checks run — that way even
    rejected-at-middleware requests still get a correlatable log
    line.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Lifespan + websocket scopes: forward unchanged. The
        # ContextVar default ("-") is fine for any logging that
        # happens during lifespan startup/shutdown.
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # Extract any upstream id from the request headers. ASGI
        # headers are a list of (name_bytes, value_bytes) tuples;
        # case-insensitive lookup per spec.
        inbound: str | None = None
        for k, v in scope.get("headers", []):
            if k == REQUEST_ID_HEADER.encode("latin-1"):
                inbound = _sanitize_inbound_request_id(
                    v.decode("latin-1", errors="replace")
                )
                if inbound:
                    break

        request_id = inbound or str(uuid.uuid4())

        # Set the ContextVar so logging.Filter + handler code can read it.
        token = _request_id_var.set(request_id)

        # Wrap `send` to inject the header into the response-start
        # message. We mutate the headers list in place because Starlette
        # passes it through unchanged for non-cached responses.
        request_id_bytes = request_id.encode("latin-1")

        async def send_with_header(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                # Don't double-add if a downstream handler already set one.
                if not any(
                    h[0].lower() == REQUEST_ID_HEADER.encode("latin-1")
                    for h in headers
                ):
                    headers.append(
                        (REQUEST_ID_HEADER.encode("latin-1"), request_id_bytes)
                    )
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            # Reset ContextVar so subsequent requests on this asyncio
            # task / thread don't inherit a stale id.
            _request_id_var.reset(token)


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


# ---------------------------------------------------------------------------
# Health-exempt TrustedHost wrapper (v2.3.3 — Fly internal probe fix)
# ---------------------------------------------------------------------------


class HealthExemptTrustedHostMiddleware:
    """Wrap Starlette's TrustedHostMiddleware but bypass it for /health.

    **Why this exists.** Fly's internal health probe runs on a private
    IPv6/IPv4 address in the 172.19.x.x / fdaa::/64 range and sends an
    HTTP/1.1 request with ``Host: <raw-ip>:<port>`` (the probe doesn't
    resolve the app's public DNS name — it talks to the machine
    directly). Starlette's stock ``TrustedHostMiddleware`` matches a
    fixed allowlist of host *names*; it does NOT accept IP literals
    unless they're individually listed, and Fly's probe IP rotates per
    deploy (172.19.24.105 on v89, different on the next machine), so
    pinning specific IPs is unmaintainable.

    PR #77 (v2.0.6) added ``<app>.internal`` and ``*.internal`` to the
    allowlist to cover DNS-based probes. That worked for some Fly probe
    paths but NOT the raw-IP path that v89's deploy logs surface:

        172.19.24.105:46588 → GET /health → 400 Bad Request
        172.19.24.105:46600 → GET /health → 400 Bad Request
        172.19.24.105:46604 → GET /health → 400 Bad Request

    The cleaner fix (Option B in the v2.3.3 brief): exempt /health from
    Host validation entirely. Health endpoints are infrastructure
    probes by convention; they're hit by load balancers, orchestrators,
    and Fly's internal prober that may not know — or care about — the
    canonical hostname. Validating Host on /health gates a critical
    operational signal on a check the prober cannot satisfy.

    **Security posture preserved.** /health returns only
    ``{"ok": true, "service": "google-docs-mcp"}`` — no sensitive
    state, no auth context, no caller-controlled output. Bypassing
    TrustedHost on this single endpoint does NOT enable Host-header
    attacks (those rely on links/redirects/cache-poisoning against
    endpoints that echo the Host); the only effect is that a request
    with any Host header gets a 200 from /health instead of a 400
    from the middleware.

    Every OTHER route still goes through the full TrustedHost gate.

    Implementation: pure-ASGI (not ``BaseHTTPMiddleware``-based) so
    we can decide route by inspecting ``scope["path"]`` and either
    call the downstream ``app`` directly (bypass) OR delegate to the
    wrapped Starlette ``TrustedHostMiddleware`` (which itself wraps
    the same downstream ``app``). ``allowed_hosts`` is captured at
    construction time; the bypass path has zero per-request overhead
    beyond a single string equality check.
    """

    def __init__(
        self,
        app: Any,
        *,
        allowed_hosts: list[str],
    ) -> None:
        # Local import to keep this module's top-level import block free
        # of the Starlette internal — only this middleware needs it.
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        self._app = app
        # Build the wrapped TrustedHostMiddleware once. It is itself an
        # ASGI app wrapping `app`, so non-/health requests funnel
        # through it and then reach the same downstream pipeline.
        self._guarded = TrustedHostMiddleware(app, allowed_hosts=allowed_hosts)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Lifespan / websocket scopes have no path-routing concept here;
        # forward them through the guarded path so Starlette's lifespan
        # protocol still reaches the app correctly.
        if scope.get("type") == "http" and scope.get("path") == "/health":
            # Bypass TrustedHost — call the wrapped app directly.
            # Same downstream pipeline TrustedHostMiddleware would have
            # called on a successful match; the only difference is the
            # Host check is skipped.
            await self._app(scope, receive, send)
            return
        # All other paths: through the full Host-validation gate.
        await self._guarded(scope, receive, send)
