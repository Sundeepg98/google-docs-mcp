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
"""
from __future__ import annotations

import html as _html  # aliased — `html` is shadowed by local var in _success_page/_error_page
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp import FastMCP
from googleapiclient.errors import HttpError
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import keys, user_store
from .auth import default_data_dir, load_credentials
from .credentials import NeedsReauthError, get_credentials_for_user
from .crypto import NonceStore, verify_signed_params
from .docx_import import convert_docx_to_tabbed_doc as _convert_docx
from .errors import friendly_http_error_message
from .oauth_google import (
    CALLBACK_PATH,
    OAuthCallbackError,
    exchange_code_for_credentials,
    load_client_config,
)

# Process-wide single-use nonce tracker. Used by BOTH the signed-upload
# URLs (existing) and the v1.1+ OAuth state-param replay protection
# (new). Single store is fine — nonce strings are unique-per-mint.
_NONCE_STORE = NonceStore()

log = logging.getLogger("google_docs_mcp.http")


# ---------------------------------------------------------------------------
# REST endpoint — thin wrapper for cloud chat / non-MCP callers
# ---------------------------------------------------------------------------


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
    from . import __version__ as _pkg_version

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


# ---------------------------------------------------------------------------
# OAuth callback (v1.1+) — downstream Google API auth, per-user
# ---------------------------------------------------------------------------


def _resolve_client_config() -> dict:
    """Load the Google OAuth client_secrets JSON.

    Resolution order (first match wins):
      1. ``GOOGLE_OAUTH_CLIENT_SECRETS_JSON`` env var — full JSON inline.
         Right for Fly secrets where we don't want files on disk.
      2. ``GOOGLE_OAUTH_CLIENT_SECRETS_PATH`` env var — path to JSON file.
      3. Fall back to the existing stdio-mode discovery in auth.py.
    """
    inline = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_JSON")
    if inline:
        data = json.loads(inline)
        if not any(k in data for k in ("web", "installed")):
            raise RuntimeError(
                "GOOGLE_OAUTH_CLIENT_SECRETS_JSON must contain a 'web' "
                "or 'installed' top-level key"
            )
        return data

    path_str = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_PATH")
    if path_str:
        return load_client_config(Path(path_str))

    from .auth import find_client_config
    return load_client_config(find_client_config(default_data_dir()))


def _resolve_base_url(request: Request) -> str:
    """Determine the public-facing base URL for OAuth redirects.

    Prefers ``GOOGLE_OAUTH_BASE_URL`` env var (most reliable in prod
    behind Fly's edge proxy). Falls back to reconstructing from the
    request's scheme + host headers — fine for local dev, fragile if
    the deployment is behind a non-standard reverse proxy that
    doesn't set X-Forwarded-*.
    """
    override = os.environ.get("GOOGLE_OAUTH_BASE_URL")
    if override:
        return override.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{scheme}://{host}"


_OAUTH_SUCCESS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authorization complete</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            max-width: 480px; margin: 96px auto; padding: 0 24px;
            color: #1f2328; line-height: 1.6; }}
    .check {{ font-size: 48px; }}
    .small {{ color: #656d76; font-size: 14px; margin-top: 32px; }}
  </style>
</head>
<body>
  <div class="check">{check}</div>
  <h1>{heading}</h1>
  <p>{body}</p>
  <p class="small">You can close this tab now.</p>
</body>
</html>"""


def _success_page() -> Response:
    body_html = _OAUTH_SUCCESS_HTML.format(
        check="✅",
        heading="Google access granted",
        body=(
            "google-docs-mcp can now act on your Drive, Docs, and Apps Script "
            "on your behalf. Return to your chat and retry the action."
        ),
    )
    return Response(body_html, status_code=200, media_type="text/html")


def _error_page(message: str, status_code: int) -> Response:
    body_html = _OAUTH_SUCCESS_HTML.format(
        check="⚠️",
        heading="Authorization didn't complete",
        body=_html.escape(message),
    )
    return Response(body_html, status_code=status_code, media_type="text/html")


async def oauth_google_api_callback(request: Request) -> Response:
    """``GET /oauth/google/api/callback?code=...&state=...``

    Final leg of the per-user Google OAuth dance. Verifies the
    HMAC-signed state, exchanges the auth code for tokens, persists
    them to ``user_store`` keyed by Google ``sub``. Returns a simple
    HTML page the user sees in their browser.
    """
    qp = request.query_params

    # Google sends ?error=access_denied if the user clicked Cancel on
    # the consent screen. Surface that cleanly instead of trying to
    # exchange a nonexistent code.
    if "error" in qp:
        log.info("oauth: user cancelled consent (%s)", qp["error"])
        return _error_page(
            f"You declined the authorization (Google said: {qp['error']}). "
            "Re-run the tool in your chat to try again.",
            status_code=400,
        )

    if "code" not in qp or "state" not in qp:
        return _error_page(
            "Missing 'code' or 'state' in callback URL. This usually "
            "means Google did not complete the authorization.",
            status_code=400,
        )

    # v2.6 (#48): purpose-routed via keys.get_key("oauth_state") so the
    # v2.0b strict-flip activates HKDF-derivation for OAuth state HMACs
    # without further edits here. get_key raises RuntimeError on missing
    # MCP_BEARER_TOKEN; preserve the prior fail-closed behavior.
    try:
        signing_key = keys.get_key("oauth_state").decode("utf-8")
    except RuntimeError:
        log.error("oauth: MCP_BEARER_TOKEN unset; cannot verify state")
        return _error_page(
            "Server configuration error. Contact the operator.",
            status_code=500,
        )

    try:
        client_config = _resolve_client_config()
    except (RuntimeError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        log.error("oauth: client_config load failed: %s", e)
        return _error_page(
            "Server configuration error (OAuth client not configured). "
            "Contact the operator.",
            status_code=500,
        )

    base_url = _resolve_base_url(request)

    # Fly terminates TLS at the edge; inside the container the proxied
    # request shows scheme=http even though the public URL is HTTPS.
    # oauthlib's Flow.fetch_token validates the authorization_response
    # URL and rejects any http://, raising InsecureTransportError. Since
    # we KNOW we're behind Fly's HTTPS edge (base_url begins with
    # https://), rewrite the scheme on the URL we hand to oauthlib. Do
    # NOT set OAUTHLIB_INSECURE_TRANSPORT=1 — that disables transport
    # security checks globally; we only want to lie about THIS one URL.
    authorization_response_url = str(request.url)
    if base_url.startswith("https://") and authorization_response_url.startswith("http://"):
        authorization_response_url = "https://" + authorization_response_url[len("http://"):]

    try:
        user_id, creds_json = exchange_code_for_credentials(
            state=qp["state"],
            authorization_response_url=authorization_response_url,
            base_url=base_url,
            client_config=client_config,
            signing_key=signing_key,
            nonce_store=_NONCE_STORE,
        )
    except OAuthCallbackError as e:
        log.warning("oauth: callback rejected: %s", e)
        return _error_page(str(e), status_code=e.status_code)

    try:
        # MUST be save_credentials_json (not save_state) — the wrapper
        # strips the operator's OAuth client_id + client_secret from the
        # Credentials.to_json() output before persisting. Calling
        # save_state directly here would leak those operator secrets
        # into every per-user row in user_state.db. The matching
        # regression guard is
        # test_oauth_callback_endpoint_strips_operator_secrets_in_production
        # in tests/integration/test_fresh_user_flow.py.
        user_store.save_credentials_json(user_id, creds_json)
    except Exception as e:  # noqa: BLE001 — last line of defence
        log.exception("oauth: user_store.save_credentials_json failed for %s", user_id)
        return _error_page(
            f"Failed to persist credentials: {e}. Contact the operator.",
            status_code=500,
        )

    log.info("oauth: persisted Google API creds for user %s", user_id)
    return _success_page()


async def convert_endpoint(request: Request) -> JSONResponse:
    """``POST /api/convert`` — multipart .docx upload + conversion + optional icons.

    Form fields:
      ``file``: the .docx file (multipart/form-data)
      ``split_by``: optional, one of "heading_1"|"heading_2"|"page_break"|"auto"
      ``title``: optional document title override
      ``icons_by_title``: optional JSON string mapping tab-title fragments
        to single-emoji strings, applied after conversion via
        set_tab_icons. Example: '{"Profile":"\\ud83d\\udc64","Skills":"\\ud83d\\udee0"}'.
        Matching is case-insensitive substring (same semantics as the
        set_tab_icons MCP tool).
    """
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse(
            {"error": "Missing 'file' field in multipart body"}, status_code=400
        )

    filename = upload.filename or "upload.docx"
    if not filename.lower().endswith(".docx"):
        return JSONResponse(
            {"error": f"Expected a .docx upload, got '{filename}'"},
            status_code=400,
        )

    split_by_raw = form.get("split_by") or "heading_1"
    if not isinstance(split_by_raw, str) or split_by_raw not in {
        "heading_1", "heading_2", "page_break", "auto",
    }:
        return JSONResponse(
            {"error": f"Invalid split_by: {split_by_raw!r}"}, status_code=400
        )

    title_raw = form.get("title")
    title: str | None = title_raw if isinstance(title_raw, str) and title_raw else None

    placeholder_behavior_raw = form.get("placeholder_behavior") or "delete"
    if (
        not isinstance(placeholder_behavior_raw, str)
        or placeholder_behavior_raw not in {"delete", "rename", "keep"}
    ):
        return JSONResponse(
            {
                "error": f"Invalid placeholder_behavior: {placeholder_behavior_raw!r} "
                "(must be 'delete', 'rename', or 'keep')"
            },
            status_code=400,
        )
    placeholder_title_raw = form.get("placeholder_title") or "Overview"
    placeholder_icon_raw = form.get("placeholder_icon") or "\U0001f4d1"
    if not isinstance(placeholder_title_raw, str) or not isinstance(
        placeholder_icon_raw, str
    ):
        return JSONResponse(
            {"error": "placeholder_title and placeholder_icon must be strings"},
            status_code=400,
        )

    replace_doc_id_raw = form.get("replace_doc_id")
    replace_doc_id: str | None = (
        replace_doc_id_raw if isinstance(replace_doc_id_raw, str) and replace_doc_id_raw
        else None
    )

    icons_raw = form.get("icons_by_title")
    icons_by_title: dict[str, str] | None = None
    if icons_raw:
        if not isinstance(icons_raw, str):
            return JSONResponse(
                {"error": "icons_by_title must be a JSON string"}, status_code=400
            )
        try:
            parsed = json.loads(icons_raw)
        except json.JSONDecodeError as e:
            return JSONResponse(
                {"error": f"icons_by_title is not valid JSON: {e}"},
                status_code=400,
            )
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            return JSONResponse(
                {"error": "icons_by_title must be a JSON object of {string: string}"},
                status_code=400,
            )
        icons_by_title = parsed

    # Stream the upload to a temp file so docx_import can read it as a
    # path. Avoids holding the full payload in memory + reuses the
    # existing local-file code path.
    with tempfile.NamedTemporaryFile(
        suffix=".docx", delete=False
    ) as tmp:
        contents = await upload.read()
        tmp.write(contents)
        tmp_path = Path(tmp.name)

    # v2.1 multi-tenant dispatch:
    #   - signed-URL callers: per-user creds via request.state.signed_url_user_id
    #     (set by BearerTokenMiddleware after verify_signed_params).
    #   - bearer-header callers: operator creds (legacy). Header-auth means
    #     the caller is whoever holds MCP_BEARER_TOKEN — typically the
    #     operator running smoke tests or a server-to-server caller; they
    #     get the operator's local Drive on purpose. NOT a multi-tenant
    #     path; cloud-chat users go through signed URLs.
    signed_uid = getattr(request.state, "signed_url_user_id", None)
    try:
        if signed_uid is not None:
            client_config = _resolve_client_config()
            signing_key = os.environ.get("MCP_BEARER_TOKEN", "")
            base_url = _resolve_base_url(request)
            try:
                creds = get_credentials_for_user(
                    signed_uid,
                    client_config=client_config,
                    signing_key=signing_key,
                    base_url=base_url,
                )
            except NeedsReauthError as e:
                # Surface a clean error with the re-auth URL so cloud-chat
                # can re-mint after the user re-authorizes. 401 because
                # the per-user creds are absent/revoked, not because of a
                # signed-URL flaw.
                return JSONResponse(
                    {
                        "error": e.reason,
                        "auth_url": e.auth_url,
                        "user_id": signed_uid,
                    },
                    status_code=401,
                )
        else:
            # Bearer-header path — operator creds, single-tenant. Same
            # behavior as v2.0.
            creds = load_credentials(default_data_dir())
        # Pass icons_by_title INTO the convert pipeline so they're
        # applied between Apps Script restructure and placeholder
        # delete. Calling set_tab_icons AFTER delete races against
        # Google's server-state propagation and 500s on heavy converts.
        result = _convert_docx(
            creds,
            docx_path=tmp_path,
            split_by=split_by_raw,  # type: ignore[arg-type]
            title=title,
            icons_by_title=icons_by_title,
            placeholder_behavior=placeholder_behavior_raw,  # type: ignore[arg-type]
            placeholder_title=placeholder_title_raw,
            placeholder_icon=placeholder_icon_raw,
            replace_doc_id=replace_doc_id,
            user_id=signed_uid,
        )
        return JSONResponse(result)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except HttpError as e:
        return JSONResponse(
            {
                "error": friendly_http_error_message(e),
                "status_code": e.status_code,
            },
            status_code=502,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


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
        bearer_token: str,
        signed_url_key: str,
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

        Reviewers: the two parameters MUST come from separate ``get_key()``
        calls (not the same value reused), even today during the shim.
        That preserves the call-site discipline so the v2.0b flip is a
        pure-config change in keys.py, not a re-edit of this middleware.
        """
        super().__init__(app)
        self._expected = f"Bearer {bearer_token}"
        self._signing_key = signed_url_key  # HMAC key for signed-URL auth path

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
        provided = request.headers.get("authorization", "")
        if provided == self._expected:
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
                nonce_store=_NONCE_STORE,
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
         + ``localhost``.
      3. Fail-open ``["*"]`` with WARNING log.

    Production safety: refuses to start if ``FLY_REGION`` is set (machine
    is running on Fly infra) but ``FLY_APP_NAME`` is absent — that's the
    silent fail-open path round-20 adversarial review identified.
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
        return [f"{app_name}.fly.dev", f"*.{app_name}.fly.dev", "localhost"]

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
# Composition + runner
# ---------------------------------------------------------------------------


def build_app(mcp: FastMCP) -> Starlette:
    """Compose the public HTTP surface: REST routes + MCP HTTP transport."""
    # v2.6 (#48): the DUAL site — bearer header auth AND signed-URL HMAC
    # used the same env var read directly. Now routed through
    # keys.get_key() so the v2.0b strict-flip can HKDF-derive them
    # separately. During the shim window (v1.5 - v2.0b) both still resolve
    # to MCP_BEARER_TOKEN verbatim; the strict-flip activates the split.
    try:
        bearer_token = keys.get_key("api_bearer").decode("utf-8")
        signed_url_key = keys.get_key("signed_url").decode("utf-8")
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
        Middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts),
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
