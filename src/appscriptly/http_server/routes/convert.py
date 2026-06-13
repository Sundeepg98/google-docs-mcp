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

from appscriptly import keys
from appscriptly.auth import default_data_dir, load_credentials
from appscriptly.credentials import NeedsReauthError, get_credentials_for_user
from appscriptly.docx_import import convert_docx_to_tabbed_doc as _convert_docx
from appscriptly.errors import friendly_http_error_message
from appscriptly.http_server._helpers import (
    _resolve_base_url,
    _resolve_client_config,
)


# PR-Δ3 (2026-05-27): structured audit logger for upload sessions.
#
# Distinct namespace from ``appscriptly.http`` so operators can
# route audit events separately from request/middleware logs (e.g.
# pipe to a SIEM, retain longer, ship to a different sink). The
# per-session line is the smallest forensic primitive: who uploaded
# what (by hash, never by content), when, in which session.
_audit_log = logging.getLogger("appscriptly.audit.upload")


async def upload_frame_endpoint(request: Request) -> JSONResponse:
    """``POST /upload/frames/<batch_id>/<index>?token=<sig>`` — stage one PNG.

    The base-tier slides→video frame handoff (replaces the
    ``drive.readonly`` Drive round-trip). Public (no bearer): auth is the
    HMAC batch token in the query string, exactly like the docx signed
    upload path — so the bound Apps Script (running as the user) can POST
    each rendered frame straight to the server via ``UrlFetchApp``.

    Hardened to mirror the docx convert path (v2.1):

    - the token is **single-use + user-bound** (see
      ``_frames_staging.verify_frames_token``); a captured token can't be
      replayed across all indices for the TTL, and is bound to one tenant;
    - the body is **size-capped at the endpoint** — both the declared
      Content-Length AND the chunked / Content-Length-omitting case (which
      ``BodySizeLimitMiddleware`` lets fall through). Over-cap → 413;
    - the staging layer additionally enforces per-batch frame-count +
      cumulative-byte caps, so a token holder can't disk-fill the box.

    The per-frame ``index`` is a bounded integer validated in the staging
    layer (no path traversal). Raw PNG bytes in the request body.
    """
    from appscriptly.services.apps_script._frames_staging import (
        _MAX_FRAME_BYTES,
        FrameUploadTooLarge,
        stage_frame_bytes,
        verify_frames_token,
    )

    batch_id = request.path_params["batch_id"]
    index = request.path_params["index"]
    token = request.query_params.get("token", "")
    # verify_frames_token returns the bound user_id (truthy str) on success
    # or None on failure (invalid / expired / replayed token).
    token_uid = verify_frames_token(batch_id, token)
    if token_uid is None:
        return JSONResponse(
            {"error": "Invalid or expired frame upload token"}, status_code=403
        )

    # Reject an over-cap DECLARED Content-Length before reading any body.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_FRAME_BYTES:
                return JSONResponse(
                    {"error": "frame too large", "max_bytes": _MAX_FRAME_BYTES},
                    status_code=413,
                )
        except ValueError:
            return JSONResponse(
                {"error": "invalid Content-Length"}, status_code=400
            )

    # Read the body with a hard cap so a CHUNKED / Content-Length-omitting
    # POST (which the body-size middleware lets through) can't stream an
    # unbounded payload into memory. Stop as soon as we exceed the cap.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_FRAME_BYTES:
            return JSONResponse(
                {"error": "frame too large", "max_bytes": _MAX_FRAME_BYTES},
                status_code=413,
            )
        chunks.append(chunk)
    body = b"".join(chunks)

    if not body:
        return JSONResponse({"error": "Empty frame body"}, status_code=400)
    try:
        stage_frame_bytes(batch_id, index, body)
    except FrameUploadTooLarge as e:
        # Per-batch count / cumulative-byte cap hit — Payload Too Large.
        return JSONResponse({"error": str(e)}, status_code=413)
    except ValueError as e:
        # Malformed batch_id / index (e.g. a traversal attempt) — reject.
        return JSONResponse(
            {"error": f"Invalid frame upload target: {e}"}, status_code=400
        )
    return JSONResponse(
        {"batch_id": batch_id, "index": index, "bytes": len(body)}
    )


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

    **Upload-size cap (signed-URL path).** When the caller authenticated
    via a signed URL, ``BearerTokenMiddleware`` has stashed the verified
    per-URL cap on ``request.state.signed_url_max_bytes``. We enforce it
    here — the cap was HMAC-signed into the URL and previously returned to
    the caller but never checked (a dead contract). Two layers, mirroring
    ``upload_frame_endpoint``:

      1. a fast reject of an honestly-DECLARED over-cap ``Content-Length``
         before the multipart body is parsed; and
      2. an authoritative post-read check on the ACTUAL decoded ``.docx``
         byte count — this is what catches a chunked / Content-Length-
         omitting POST that slips past every Content-Length guard.

    Bearer-header callers (no signed URL) have no per-URL cap; they're
    bounded only by ``BodySizeLimitMiddleware`` / Drive's own ceiling, as
    before.
    """
    # Per-URL upload cap from the signed URL (None for bearer-header
    # callers). Read once up front so both the pre-parse Content-Length
    # fast-reject and the post-read actual-bytes check use the same value.
    signed_max_bytes = getattr(request.state, "signed_url_max_bytes", None)

    # Layer 1: reject an honestly-DECLARED over-cap upload before parsing
    # the body. A chunked / Content-Length-omitting POST has no usable
    # header here and falls through to the post-read check below.
    if signed_max_bytes is not None:
        declared_cl = request.headers.get("content-length")
        if declared_cl is not None:
            try:
                declared_len = int(declared_cl)
            except ValueError:
                return JSONResponse(
                    {"error": "invalid Content-Length"}, status_code=400
                )
            if declared_len > signed_max_bytes:
                return JSONResponse(
                    {"error": "payload too large", "max_bytes": signed_max_bytes},
                    status_code=413,
                )

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

    # Read the uploaded part into memory once. Starlette has already
    # buffered the multipart body (spooled to a temp file past
    # ``max_part_size``), so this measures the ACTUAL decoded ``.docx``
    # byte count regardless of how the body arrived on the wire.
    contents = await upload.read()

    # Layer 2 (authoritative): enforce the signed-URL cap on the real
    # byte count. This is the guard that catches a chunked /
    # Content-Length-omitting POST — the only place we know the true size
    # is after the bytes are in hand. Reject BEFORE writing the temp file
    # so an over-cap payload is never persisted to disk.
    if signed_max_bytes is not None and len(contents) > signed_max_bytes:
        return JSONResponse(
            {"error": "payload too large", "max_bytes": signed_max_bytes},
            status_code=413,
        )

    # Stream the (now size-checked) upload to a temp file so docx_import
    # can read it as a path. Avoids re-holding the payload + reuses the
    # existing local-file code path.
    with tempfile.NamedTemporaryFile(
        suffix=".docx", delete=False
    ) as tmp:
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
