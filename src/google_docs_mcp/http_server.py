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
from .docx_import import convert_docx_to_tabbed_doc as _convert_docx

log = logging.getLogger("google_docs_mcp.http")


# ---------------------------------------------------------------------------
# REST endpoint — thin wrapper for cloud chat / non-MCP callers
# ---------------------------------------------------------------------------


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "google-docs-mcp"})


async def convert_endpoint(request: Request) -> JSONResponse:
    """``POST /api/convert`` — multipart .docx upload + conversion.

    Form fields:
      ``file``: the .docx file (multipart/form-data)
      ``split_by``: optional, one of "heading_1"|"heading_2"|"page_break"|"auto"
      ``title``: optional document title override
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
        )
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
    """Reject requests without the right ``Authorization: Bearer <token>``.

    Token is set via ``MCP_BEARER_TOKEN`` env var. ``/health`` is exempt
    so Fly.io health probes don't need to know the token.
    """

    def __init__(self, app: Any, *, token: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if request.url.path == "/health":
            return await call_next(request)
        provided = request.headers.get("authorization", "")
        if provided != self._expected:
            return JSONResponse(
                {"error": "missing or invalid bearer token"},
                status_code=401,
            )
        return await call_next(request)


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

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/api/convert", convert_endpoint, methods=["POST"]),
        # FastMCP's MCP-protocol-over-HTTP, for future Claude surfaces
        # that speak MCP directly. The path prefix is ``/mcp``.
        Mount("/mcp", app=mcp.http_app(path="/")),
    ]

    return Starlette(routes=routes, middleware=middleware)


def run_http(mcp: FastMCP, *, port: int = 8080) -> None:
    """Boot the HTTP server on ``0.0.0.0:<port>``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = build_app(mcp)
    log.info("starting HTTP MCP server on 0.0.0.0:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
