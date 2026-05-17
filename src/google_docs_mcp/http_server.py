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

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp import FastMCP
from googleapiclient.errors import HttpError
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .auth import default_data_dir, load_credentials
from .crypto import NonceStore, verify_signed_params
from .docs_api import set_tab_icons as _set_tab_icons
from .docx_import import convert_docx_to_tabbed_doc as _convert_docx

# Process-wide single-use nonce tracker for signed upload URLs.
_NONCE_STORE = NonceStore()

log = logging.getLogger("google_docs_mcp.http")


# ---------------------------------------------------------------------------
# REST endpoint — thin wrapper for cloud chat / non-MCP callers
# ---------------------------------------------------------------------------


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "google-docs-mcp"})


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

    try:
        creds = load_credentials(default_data_dir())
        result = _convert_docx(
            creds,
            docx_path=tmp_path,
            split_by=split_by_raw,  # type: ignore[arg-type]
            title=title,
            placeholder_behavior=placeholder_behavior_raw,  # type: ignore[arg-type]
            placeholder_title=placeholder_title_raw,
            placeholder_icon=placeholder_icon_raw,
        )
        if icons_by_title and result.get("doc_id"):
            icon_result = _set_tab_icons(creds, result["doc_id"], icons_by_title)
            result["icons"] = icon_result
        return JSONResponse(result)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except HttpError as e:
        details = (
            e.error_details if hasattr(e, "error_details") else str(e)
        )
        return JSONResponse(
            {
                "error": f"Google API error: {e.status_code} {e.reason}",
                "details": details,
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

    def __init__(self, app: Any, *, token: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {token}"
        self._signing_key = token  # HMAC key for signed-URL auth path

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if not request.url.path.startswith("/api/"):
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
            ok, err, _max = verify_signed_params(
                signing_key=self._signing_key,
                exp=qp["exp"],
                nonce=qp["nonce"],
                max_bytes=qp["max"],
                sig=qp["sig"],
                nonce_store=_NONCE_STORE,
            )
            if ok:
                return await call_next(request)
            return JSONResponse(
                {"error": f"signed URL rejected: {err}"}, status_code=401
            )

        return JSONResponse(
            {"error": "missing or invalid credentials (bearer header or signed URL)"},
            status_code=401,
        )


# ---------------------------------------------------------------------------
# Composition + runner
# ---------------------------------------------------------------------------


def build_app(mcp: FastMCP) -> Starlette:
    """Compose the public HTTP surface: REST routes + MCP HTTP transport."""
    token = os.environ.get("MCP_BEARER_TOKEN")
    if not token:
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var must be set when running in HTTP "
            "mode. Generate a long random string and set it on Fly.io "
            "with `fly secrets set MCP_BEARER_TOKEN=...`."
        )

    middleware = [Middleware(BearerTokenMiddleware, token=token)]

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
        Route("/api/convert", convert_endpoint, methods=["POST"]),
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
