"""``POST /api/convert`` — REST wrapper around docx → tabbed-doc conversion."""
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
import uuid
from pathlib import Path

from googleapiclient.errors import HttpError
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse

from google_docs_mcp import keys
from google_docs_mcp.auth import default_data_dir, load_credentials
from google_docs_mcp.credentials import NeedsReauthError, get_credentials_for_user
from google_docs_mcp.docx_import import convert_docx_to_tabbed_doc as _convert_docx
from google_docs_mcp.errors import friendly_http_error_message
from google_docs_mcp.http_server._helpers import (
    _resolve_base_url,
    _resolve_client_config,
)


# PR-Δ3 (2026-05-27): structured audit logger for upload sessions.
#
# Distinct namespace from ``google_docs_mcp.http`` so operators can
# route audit events separately from request/middleware logs (e.g.
# pipe to a SIEM, retain longer, ship to a different sink). The
# per-session line is the smallest forensic primitive: who uploaded
# what (by hash, never by content), when, in which session.
_audit_log = logging.getLogger("google_docs_mcp.audit.upload")


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

    # PR-Δ3: structured audit log line per upload session.
    #
    # ``user_id``: the signed-URL ``uid`` if present (multi-tenant
    # cloud-chat path); else ``anonymous_sandbox`` for the bearer-
    # header / operator path. NEVER the raw OAuth ``sub`` if we have
    # an alternative — pre-truncate to first 8 chars to limit
    # correlation surface in long-retained logs (full sub stays in
    # the per-user state DB, accessible to operators only).
    # ``file_sha256``: hash, NOT content. Purpose is "did THIS bytes-
    # equal file get uploaded twice?" — replay detection, dup-detection,
    # forensic correlation — without retaining the bytes themselves.
    # ``upload_session_id``: per-request UUID. Stable across the rest
    # of THIS request's downstream logs once we propagate it (followup
    # PR-Δ4 wires it through docx_import; for now, scoped here).
    # ``ts``: UTC unix-seconds; operators correlate against Google's
    # audit trail (which uses wall-clock too).
    signed_uid_for_log = getattr(request.state, "signed_url_user_id", None)
    audit_user_id = (
        f"sub:{signed_uid_for_log[:8]}…"
        if isinstance(signed_uid_for_log, str) and signed_uid_for_log
        else "anonymous_sandbox"
    )
    upload_session_id = str(uuid.uuid4())
    _audit_log.info(
        "upload_session "
        "session_id=%s user_id=%s file_size_bytes=%d "
        "file_sha256=%s split_by=%s ts=%d",
        upload_session_id,
        audit_user_id,
        len(contents),
        hashlib.sha256(contents).hexdigest(),
        split_by_raw,
        int(time.time()),
    )

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
            # v2.0b: route via keys.get_key("oauth_state") instead of
            # reading MCP_BEARER_TOKEN directly. Pre-flip this branch was
            # a latent bypass that PR #57's _BYPASS_PATTERNS missed
            # (pattern didn't catch the ``, ""``-default form). Post-flip
            # the str-default bypass would type-error against the
            # bytes-typed get_credentials_for_user signature anyway; the
            # fix is to use the same key-resolution path as the OAuth
            # callback (which also calls oauth_state-related sign/verify).
            signing_key = keys.get_key("oauth_state")
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
