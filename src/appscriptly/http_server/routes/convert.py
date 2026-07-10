"""``POST /api/convert`` — REST wrapper around docx → tabbed-doc conversion.

**Job model (T1.1, 2026-07-10).** Conversion no longer runs inline in the
request coroutine. The handler validates the request, creates a durable
job row (``appscriptly.job_store``, SQLite on the Fly ``/data`` volume)
and spawns the work as a detached asyncio task
(``appscriptly.http_server.jobs``). Consequences:

- The DEFAULT (no ``async`` field) response is unchanged: the handler
  awaits the task and returns the full converter result exactly as
  before. But a client disconnect mid-wait no longer kills the work -
  the await is shielded (``jobs.wait_for_outcome``), so cancelling the
  request coroutine leaves the detached task running; the conversion
  completes and the row records it.
- ``async=1`` opts into an immediate ``202 {job_id, status_url}``; the
  pre-signed ``status_url`` (24h validity, multi-use) reports
  queued|running|done|error (+ derived ``stalled``) and carries the
  full result once done. See ``convert_status.py``.
- The signed-URL nonce is consumed at JOB CREATION, not at middleware
  verification. A request that fails validation (bad field, missing
  creds) leaves the URL usable; a retry of the SAME request whose nonce
  is already burned ATTACHES to the job that burned it via the request
  fingerprint (user + content hash + params) within 15 minutes,
  returning the in-flight/succeeded outcome instead of duplicating
  work or documents. FAILED jobs are deliberately NOT attach targets
  (N1): a re-POST matching a failed attempt starts a fresh conversion
  (with a fresh signed URL when the original nonce was consumed).
- **Deploy semantics:** a Fly deploy/restart kills in-flight tasks.
  Rows stop heartbeating and derive ``stalled``; re-POSTing the
  identical request re-arms the SAME job row (status URLs stay valid).
  This is what makes a blind client retry safe across deploys.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from googleapiclient.errors import HttpError
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse

from appscriptly import job_store, keys
from appscriptly.auth import default_data_dir, load_credentials
from appscriptly.credentials import NeedsReauthError, get_credentials_for_user
from appscriptly.docx_import import convert_docx_to_tabbed_doc as _convert_docx
from appscriptly.errors import friendly_http_error_message
from appscriptly.http_server import _state, jobs
from appscriptly.http_server._helpers import (
    _resolve_base_url,
    _resolve_client_config,
)
from appscriptly.http_server.routes.convert_status import build_status_url
from appscriptly.retrofit import retrofit_existing_docx as _retrofit_docx


# PR-Δ3 (2026-05-27): structured audit logger for upload sessions.
#
# Distinct namespace from ``appscriptly.http`` so operators can
# route audit events separately from request/middleware logs (e.g.
# pipe to a SIEM, retain longer, ship to a different sink). The
# per-session line is the smallest forensic primitive: who uploaded
# what (by hash, never by content), when, in which session.
_audit_log = logging.getLogger("appscriptly.audit.upload")

# Upper bound on inputs per request (multipart ``file`` parts or
# ``drive_file_ids`` entries). Each input becomes its own job + worker
# thread; 20 is far above the observed batch sizes (3-4 docs) while
# keeping a single request from monopolizing the box.
MAX_BATCH_ITEMS = 20


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


# ---------------------------------------------------------------------
# Request-side plumbing for the job model
# ---------------------------------------------------------------------


@dataclass
class _PlannedJob:
    """One input's resolved execution plan.

    ``kind`` is one of:
      create          - no attachable job; a fresh row + task is needed
                        (including when the only candidate FAILED - a
                        failed attempt never captures retries, N1)
      rearm           - an attachable row exists but its process died
                        (derived stalled / orphaned); re-run on the SAME
                        row so issued status URLs stay valid
      attach_running  - an identical job is live in this process; reuse
                        its task instead of duplicating work
      attach_done     - an identical job already SUCCEEDED; reuse result
    """
    label: str
    fingerprint: str
    kind: str
    work: Callable[[], dict[str, Any]] | None = None
    job_id: str | None = None
    row: dict[str, Any] | None = None
    task: "asyncio.Task[tuple[str, Any, Any]] | None" = None

    @property
    def attached(self) -> bool:
        return self.kind != "create"

    @property
    def needs_spawn(self) -> bool:
        return self.kind in ("create", "rearm")


def _form_str(value: object) -> str | None:
    """A non-empty string form value, else None (files/absent/empty)."""
    return value if isinstance(value, str) and value else None


def _fingerprint(user_key: str, content_key: str, fp_params: dict[str, Any]) -> str:
    """Request fingerprint: WHO converts WHAT with WHICH output-affecting
    params. Same triple within the attach window = same job.

    REBASE NOTE (streams convert-core / nested): any NEW output-affecting
    form param added to this endpoint (e.g. ``on_conflict``,
    ``nest_by``) MUST be added to the ``fp_params`` dict built in
    ``convert_endpoint``, or retries that vary the new param would
    wrongly attach to each other's jobs.
    """
    material = json.dumps(
        {"user": user_key, "content": content_key, "params": fp_params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _plan_input(
    label: str,
    fingerprint: str,
    work: Callable[[], dict[str, Any]],
    user_key: str,
) -> _PlannedJob:
    """Decide create / re-arm / attach for one input.

    MUST be called from the event loop with no await between here and
    the spawn (see the NO-AWAIT comment in ``convert_endpoint``): the
    lookup + decision + row-write + task-spawn sequence is then atomic
    with respect to other requests, so two identical concurrent POSTs
    serialize and the second one attaches to the first one's job.

    N1 (2026-07-10 retest): FAILED terminal jobs never capture the
    fingerprint window. Attaching to an error row replayed the cached
    failure on every identical re-POST for 15 minutes - the failure
    text's own "re-run the conversion" advice could not work without
    mutating a param to shift the hash. A retry matching a failed job
    now starts a NEW job (a fresh signed URL is needed if the original
    nonce was consumed; the 401 says exactly that).
    """
    row = job_store.find_attachable_job(fingerprint, user_key)
    if row is None:
        return _PlannedJob(label=label, fingerprint=fingerprint, kind="create", work=work)

    derived = job_store.derive_status(row)
    if derived in ("queued", "running"):
        task = jobs.get_task(row["job_id"])
        if task is not None:
            return _PlannedJob(
                label=label, fingerprint=fingerprint, kind="attach_running",
                job_id=row["job_id"], row=row, task=task,
            )
        # Live-looking row but no task in this process. Either the task
        # finished a moment ago (re-read shows done/error) or the row
        # belongs to a process that died recently enough that its
        # heartbeat hasn't aged past the stalled threshold. Nobody will
        # ever run the latter; re-arm it now rather than waiting for
        # the stalled derivation to catch up.
        fresh = job_store.get_job(row["job_id"])
        if fresh is not None:
            row = fresh
            derived = job_store.derive_status(row)
            if derived in ("queued", "running", "stalled"):
                return _PlannedJob(
                    label=label, fingerprint=fingerprint, kind="rearm",
                    job_id=row["job_id"], row=row, work=work,
                )

    if derived == "done":
        result = job_store.result_dict(row) or {}
        if not result.get("error"):
            return _PlannedJob(
                label=label, fingerprint=fingerprint, kind="attach_done",
                job_id=row["job_id"], row=row,
            )
        # A done row whose result carries ``error`` is a partial-failure
        # envelope persisted by a pre-N3 build (current runners finish
        # those as status=error). Treat it as failed: fall through to a
        # fresh job rather than replaying the corpse.
    if derived == "error":
        # Failed attempts are not idempotency anchors (N1): fall
        # through to a fresh job.
        return _PlannedJob(
            label=label, fingerprint=fingerprint, kind="create", work=work,
        )
    if derived == "stalled":
        # The owning process died mid-run. Re-run on the same row.
        return _PlannedJob(
            label=label, fingerprint=fingerprint, kind="rearm",
            job_id=row["job_id"], row=row, work=work,
        )
    # done-with-error (legacy rows): new job.
    return _PlannedJob(label=label, fingerprint=fingerprint, kind="create", work=work)


def _make_file_work(
    creds: Any,
    contents: bytes,
    params: dict[str, Any],
    signed_uid: str | None,
) -> Callable[[], dict[str, Any]]:
    """Blocking converter closure for an uploaded .docx.

    The temp file is created INSIDE the closure (on the worker thread)
    and owned by it: attach paths never touch disk, and the file is
    unlinked when the conversion ends however it ends. If the process
    dies mid-run the orphan lives in the container's /tmp, which does
    not survive a Fly deploy.
    """
    def work() -> dict[str, Any]:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)
        try:
            if params["markers"] is not None:
                return _retrofit_docx(
                    creds,
                    markers=params["markers"],
                    docx_path=tmp_path,
                    title=params["title"],
                    icons_by_title=params["icons_by_title"],
                    placeholder_behavior=params["placeholder_behavior"],
                    placeholder_title=params["placeholder_title"],
                    placeholder_icon=params["placeholder_icon"],
                    replace_doc_id=params["replace_doc_id"],
                    on_conflict=params["on_conflict"],
                )
            return _convert_docx(
                creds,
                docx_path=tmp_path,
                split_by=params["split_by"],
                nest_by=params["nest_by"],
                title=params["title"],
                icons_by_title=params["icons_by_title"],
                placeholder_behavior=params["placeholder_behavior"],
                placeholder_title=params["placeholder_title"],
                placeholder_icon=params["placeholder_icon"],
                replace_doc_id=params["replace_doc_id"],
                on_conflict=params["on_conflict"],
                user_id=signed_uid,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return work


def _make_drive_work(
    creds: Any,
    drive_file_id: str,
    params: dict[str, Any],
    signed_uid: str | None,
) -> Callable[[], dict[str, Any]]:
    """Blocking converter closure for an existing Drive file (T3.3
    convert-from-Drive): same pipeline, ``drive_file_id`` input mode,
    reusing the converter's existing Drive entry (fetch+convert for a
    .docx, copy for a native Google Doc). ``drive.file`` visibility
    applies: the file must be app-accessible."""
    def work() -> dict[str, Any]:
        if params["markers"] is not None:
            return _retrofit_docx(
                creds,
                markers=params["markers"],
                drive_file_id=drive_file_id,
                title=params["title"],
                icons_by_title=params["icons_by_title"],
                placeholder_behavior=params["placeholder_behavior"],
                placeholder_title=params["placeholder_title"],
                placeholder_icon=params["placeholder_icon"],
                replace_doc_id=params["replace_doc_id"],
                on_conflict=params["on_conflict"],
            )
        return _convert_docx(
            creds,
            drive_file_id=drive_file_id,
            split_by=params["split_by"],
            nest_by=params["nest_by"],
            title=params["title"],
            icons_by_title=params["icons_by_title"],
            placeholder_behavior=params["placeholder_behavior"],
            placeholder_title=params["placeholder_title"],
            placeholder_icon=params["placeholder_icon"],
            replace_doc_id=params["replace_doc_id"],
            on_conflict=params["on_conflict"],
            user_id=signed_uid,
        )
    return work


def _job_descriptor(request: Request, plan: _PlannedJob) -> dict[str, Any]:
    """The 202-shape public descriptor for one planned job."""
    assert plan.job_id is not None
    row = job_store.get_job(plan.job_id)
    status = job_store.derive_status(row) if row is not None else "queued"
    minted = build_status_url(request, plan.job_id)
    return {
        "input": plan.label,
        "job_id": plan.job_id,
        "status": status,
        "status_url": minted["status_url"],
        "status_url_expires_at": minted["expires_at"],
        "attached_to_existing_job": plan.attached,
    }


async def _sync_outcome(plan: _PlannedJob) -> tuple[str, Any, Any]:
    """Resolve a plan to the runner outcome tuple for the sync path."""
    if plan.task is not None:
        # Fresh spawn or attach to a live task. The shielded await
        # (jobs.wait_for_outcome) is what makes a client disconnect
        # kill only THIS coroutine and never the job (T1.1 core
        # property) - a bare ``await plan.task`` would cancel the job
        # with its awaiter.
        return await jobs.wait_for_outcome(plan.task)
    # The only task-less plan is attach_done (failed jobs stopped being
    # attachable under N1; every other kind spawns a task).
    assert plan.kind == "attach_done" and plan.row is not None
    return ("done", job_store.result_dict(plan.row), None)


def _sync_response(outcome: tuple[str, Any, Any], attached: bool) -> JSONResponse:
    """Map a runner outcome to the historical synchronous response.

    A FRESH job's success payload is byte-identical to the pre-job-model
    response (the converter result verbatim). Attach/re-arm responses
    additionally carry ``attached_to_existing_job: true`` so a retrying
    client can tell it coalesced with earlier work.

    S2.5 partial-failure contract (post-#229): a pipeline that died
    AFTER content started moving RETURNS its envelope (kept doc +
    completion manifest + ``error`` field) instead of raising - signal
    the failure via the status code while keeping the recovery data in
    the body, exactly like the pre-job-model endpoint.
    """
    kind, a, b = outcome
    if kind == "done":
        payload = dict(a) if attached else a
        if attached:
            payload["attached_to_existing_job"] = True
        status = 500 if isinstance(payload, dict) and payload.get("error") else 200
        return JSONResponse(payload, status_code=status)
    payload = dict(b)
    if attached:
        payload["attached_to_existing_job"] = True
    return JSONResponse(payload, status_code=a)


async def convert_endpoint(request: Request) -> JSONResponse:
    """``POST /api/convert`` — .docx/Drive-file → tabbed-doc conversion jobs.

    Exactly ONE input mode per request:
      ``file``: one or more multipart .docx parts (repeat the field for
        a batch);
      ``drive_file_id``: convert an existing app-accessible Drive .docx
        or Google Doc (no upload; send as a plain form field);
      ``drive_file_ids``: JSON string array of Drive file IDs (batch).

    Shared form fields (apply to every input in the request):
      ``split_by``: "heading_1"|"heading_2"|"page_break"|"auto"
      ``nest_by``: "heading_2" (only with split_by="heading_1"): each
        Heading 1 becomes a parent tab, Heading 2s become child tabs
      ``title``: document title override (single-input requests only;
        defaults to each uploaded file's own name stem)
      ``icons_by_title``: JSON string mapping tab-title fragments to
        single-emoji strings (case-insensitive substring match, same
        semantics as the set_tab_icons MCP tool)
      ``placeholder_behavior``: "delete"|"rename"|"keep" (default delete)
      ``placeholder_title`` / ``placeholder_icon``: used when renaming
      ``replace_doc_id``: trash this doc after a successful build
        (single-input requests only)
      ``on_conflict``: "new" (default) | "replace" | "skip" when an
        app-visible doc with the same final title already exists
      ``markers``: JSON string array of {"marker_text", "tab_title"} -
        injects Heading 1s into a styled .docx that has none, then
        converts (the retrofit path). Only with heading_1 splitting.
      ``async``: "1"/"true" returns 202 {job_id, status_url} immediately
        instead of waiting for the conversion (recommended for large
        docs; the pre-signed status_url is valid 24h and multi-use).

    **Response shapes.** Single input, no ``async``: the full converter
    result (unchanged from the pre-job-model endpoint), or the mapped
    error (400/500/502). Single input with ``async=1``: 202 with a job
    descriptor. Batch (multiple files or ``drive_file_ids``): ALWAYS
    202 ``{"jobs": [descriptor, ...]}`` - a batch is inherently async;
    poll each ``status_url``.

    **Retry semantics (job model).** The conversion itself runs in a
    detached task and survives client disconnects. Re-POSTing an
    identical request (same user, same file bytes or Drive id, same
    params) within 15 minutes attaches to the existing job instead of
    duplicating it - including when the single-use signed URL's nonce
    was already consumed by the first attempt, and including after a
    server deploy killed the in-flight run (the row derives ``stalled``
    and the retry re-arms it under the same job_id). FAILED attempts
    are not deduplicated (N1): a re-POST matching a failed job starts a
    fresh conversion; if the original URL's nonce was consumed, mint a
    fresh signed URL first (the 401 for a burned nonce says so). The
    nonce is consumed only when a request actually creates a job, so a
    rejected request (validation error, missing creds) never burns the
    URL.

    **Upload-size cap (signed-URL path).** When the caller authenticated
    via a signed URL, ``BearerTokenMiddleware`` has stashed the verified
    per-URL cap on ``request.state.signed_url_max_bytes``. Two layers:

      1. a fast reject of an honestly-DECLARED over-cap ``Content-Length``
         before the multipart body is parsed; and
      2. an authoritative post-read check on the ACTUAL decoded byte
         count, summed across every uploaded part - this is what catches
         a chunked / Content-Length-omitting POST.

    Bearer-header callers (no signed URL) have no per-URL cap; they're
    bounded only by ``BodySizeLimitMiddleware`` / Drive's own ceiling.
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

    # ---- input modes: file part(s) | drive_file_id | drive_file_ids ----
    uploads = [u for u in form.getlist("file") if isinstance(u, UploadFile)]
    drive_file_id = _form_str(form.get("drive_file_id"))

    drive_ids_raw = form.get("drive_file_ids")
    drive_file_ids: list[str] | None = None
    if drive_ids_raw is not None and drive_ids_raw != "":
        if not isinstance(drive_ids_raw, str):
            return JSONResponse(
                {"error": "drive_file_ids must be a JSON string array"},
                status_code=400,
            )
        try:
            parsed_ids = json.loads(drive_ids_raw)
        except json.JSONDecodeError as e:
            return JSONResponse(
                {"error": f"drive_file_ids is not valid JSON: {e}"},
                status_code=400,
            )
        if (
            not isinstance(parsed_ids, list)
            or not parsed_ids
            or not all(isinstance(i, str) and i for i in parsed_ids)
        ):
            return JSONResponse(
                {
                    "error": "drive_file_ids must be a non-empty JSON array "
                    "of Drive file ID strings"
                },
                status_code=400,
            )
        drive_file_ids = parsed_ids

    modes_used = sum(
        1 for present in (bool(uploads), drive_file_id is not None,
                          drive_file_ids is not None) if present
    )
    if modes_used != 1:
        return JSONResponse(
            {
                "error": "Provide exactly one input: 'file' multipart "
                "part(s) with .docx bytes, 'drive_file_id' (one existing "
                "Drive file), or 'drive_file_ids' (JSON array, batch)."
            },
            status_code=400,
        )

    n_inputs = len(uploads) or (1 if drive_file_id else len(drive_file_ids or []))
    if n_inputs > MAX_BATCH_ITEMS:
        return JSONResponse(
            {
                "error": f"Too many inputs in one request: {n_inputs} "
                f"(max {MAX_BATCH_ITEMS}). Split the batch."
            },
            status_code=400,
        )

    for upload in uploads:
        filename = upload.filename or "upload.docx"
        if not filename.lower().endswith(".docx"):
            return JSONResponse(
                {"error": f"Expected a .docx upload, got '{filename}'"},
                status_code=400,
            )

    # ---- shared params (validation preserved from the pre-job model) ----
    split_by_provided = _form_str(form.get("split_by")) is not None
    split_by_raw = form.get("split_by") or "heading_1"
    if not isinstance(split_by_raw, str) or split_by_raw not in {
        "heading_1", "heading_2", "page_break", "auto",
    }:
        return JSONResponse(
            {"error": f"Invalid split_by: {split_by_raw!r}"}, status_code=400
        )

    # nest_by (T3.1): strictly "heading_2", strictly with
    # split_by="heading_1". Anything else is a loud 400 BEFORE any Drive
    # work - a nested request must never silently produce a flat doc.
    nest_by_raw = form.get("nest_by")
    nest_by: str | None = None
    if nest_by_raw:
        if not isinstance(nest_by_raw, str) or nest_by_raw != "heading_2":
            return JSONResponse(
                {
                    "error": f"Invalid nest_by: {nest_by_raw!r} "
                    "(the only supported value is 'heading_2')"
                },
                status_code=400,
            )
        if split_by_raw != "heading_1":
            return JSONResponse(
                {
                    "error": "nest_by='heading_2' requires "
                    f"split_by='heading_1' (got split_by={split_by_raw!r})"
                },
                status_code=400,
            )
        nest_by = "heading_2"

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

    # on_conflict (T2.3): what to do when an app-visible doc with the
    # same final title already exists. Validated here, applied inside
    # the pipeline (title lookup runs under drive.file visibility).
    on_conflict_raw = form.get("on_conflict") or "new"
    if not isinstance(on_conflict_raw, str) or on_conflict_raw not in {
        "new", "replace", "skip",
    }:
        return JSONResponse(
            {
                "error": f"Invalid on_conflict: {on_conflict_raw!r} "
                "(must be 'new', 'replace', or 'skip')"
            },
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

    # markers (T3.2 parity with the retrofit path): inject Heading 1s
    # into a styled .docx that has visual banners instead of headings,
    # then convert. Implies heading_1 splitting.
    markers_raw = form.get("markers")
    markers: list[dict[str, str]] | None = None
    if markers_raw:
        if not isinstance(markers_raw, str):
            return JSONResponse(
                {"error": "markers must be a JSON string"}, status_code=400
            )
        try:
            parsed_markers = json.loads(markers_raw)
        except json.JSONDecodeError as e:
            return JSONResponse(
                {"error": f"markers is not valid JSON: {e}"}, status_code=400
            )
        if (
            not isinstance(parsed_markers, list)
            or not parsed_markers
            or not all(
                isinstance(m, dict)
                and isinstance(m.get("marker_text"), str)
                and isinstance(m.get("tab_title"), str)
                for m in parsed_markers
            )
        ):
            return JSONResponse(
                {
                    "error": "markers must be a non-empty JSON array of "
                    '{"marker_text": str, "tab_title": str} objects'
                },
                status_code=400,
            )
        if split_by_provided and split_by_raw != "heading_1":
            return JSONResponse(
                {
                    "error": "markers implies split_by=heading_1 (the "
                    f"injected markers ARE Heading 1s); got split_by="
                    f"{split_by_raw!r}"
                },
                status_code=400,
            )
        if nest_by is not None:
            # The retrofit entry has no nest_by support; dropping the
            # param silently would be the exact bug class T3.2 fixes.
            return JSONResponse(
                {
                    "error": "nest_by is not supported together with "
                    "markers (the retrofit path injects flat Heading 1s "
                    "only)"
                },
                status_code=400,
            )
        markers = parsed_markers

    async_raw = form.get("async")
    async_flag = False
    if async_raw is not None and async_raw != "":
        if not isinstance(async_raw, str) or async_raw.strip().lower() not in {
            "1", "true", "yes", "0", "false", "no",
        }:
            return JSONResponse(
                {"error": f"Invalid async value: {async_raw!r} (use '1' or '0')"},
                status_code=400,
            )
        async_flag = async_raw.strip().lower() in {"1", "true", "yes"}

    batch_mode = len(uploads) > 1 or drive_file_ids is not None
    if batch_mode and title is not None:
        return JSONResponse(
            {
                "error": "title is not supported with batch input (it "
                "would name every document identically); titles derive "
                "from each file's name"
            },
            status_code=400,
        )
    if batch_mode and replace_doc_id is not None:
        return JSONResponse(
            {
                "error": "replace_doc_id is not supported with batch "
                "input (one prior doc cannot be replaced by N results)"
            },
            status_code=400,
        )

    # ---- read upload bytes; enforce the signed cap on the REAL sum ----
    # Starlette has already buffered the multipart body (spooled to a
    # temp file past ``max_part_size``), so this measures the ACTUAL
    # decoded byte count regardless of how the body arrived on the wire.
    file_items: list[tuple[str, bytes]] = []
    total_bytes = 0
    for upload in uploads:
        contents = await upload.read()
        total_bytes += len(contents)
        file_items.append((upload.filename or "upload.docx", contents))

    # Layer 2 (authoritative): the only place the true size is known is
    # after the bytes are in hand; the sum across parts is what the
    # signed cap authorized. Rejecting here consumes NO nonce (job-model
    # deferral), so an over-cap attempt does not burn the URL.
    if signed_max_bytes is not None and total_bytes > signed_max_bytes:
        return JSONResponse(
            {"error": "payload too large", "max_bytes": signed_max_bytes},
            status_code=413,
        )

    # PR-Δ3: structured audit log line per uploaded file (one session id
    # per request; drive-sourced inputs are not uploads and emit none).
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
    signed_uid_for_log = getattr(request.state, "signed_url_user_id", None)
    audit_user_id = (
        f"sub:{signed_uid_for_log[:8]}…"
        if isinstance(signed_uid_for_log, str) and signed_uid_for_log
        else "anonymous_sandbox"
    )
    upload_session_id = str(uuid.uuid4())
    for filename, contents in file_items:
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
    #
    # Credential resolution runs BEFORE any job is created, so a
    # NeedsReauthError never burns the nonce: re-authorize, then retry
    # the same signed URL.
    signed_uid = getattr(request.state, "signed_url_user_id", None)
    try:
        if signed_uid is not None:
            client_config = _resolve_client_config()
            # v2.0b: route via keys.get_key("oauth_state") — same
            # key-resolution path as the OAuth callback.
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

        # Params handed to the converter closures, and (canonicalized)
        # into the fingerprint. REBASE NOTE: any new output-affecting
        # form param added to this endpoint lands in BOTH this dict and
        # the converter call sites in _make_file_work/_make_drive_work,
        # or retries varying the new param would wrongly attach to each
        # other's jobs (nest_by/on_conflict wired 2026-07-10).
        base_params: dict[str, Any] = {
            "split_by": split_by_raw,
            "nest_by": nest_by,
            "title": title,
            "icons_by_title": icons_by_title,
            "placeholder_behavior": placeholder_behavior_raw,
            "placeholder_title": placeholder_title_raw,
            "placeholder_icon": placeholder_icon_raw,
            "replace_doc_id": replace_doc_id,
            "on_conflict": on_conflict_raw,
            "markers": markers,
        }
        user_key = signed_uid if signed_uid is not None else "operator"

        # ------------------------------------------------------------
        # Plan + create + spawn. NO AWAIT from the first
        # find_attachable_job to the last start_job: every call in this
        # block is synchronous, so the event loop cannot interleave a
        # second request and the decide-create-spawn sequence is atomic
        # (two identical concurrent POSTs serialize; the later one
        # attaches instead of double-creating).
        # ------------------------------------------------------------
        plans: list[_PlannedJob] = []
        if uploads:
            for filename, contents in file_items:
                item_params = dict(base_params)
                if item_params["title"] is None:
                    # BUG 2a: default the title to the uploaded file's
                    # real stem so the doc carries its final name from
                    # the moment it exists in Drive (the pipeline would
                    # otherwise name it after the NamedTemporaryFile).
                    # Per input, so each batch entry gets its own name.
                    stem = Path(filename).stem.strip()
                    if stem:
                        item_params["title"] = stem
                fp = _fingerprint(
                    user_key,
                    f"sha256:{hashlib.sha256(contents).hexdigest()}",
                    item_params,
                )
                plans.append(_plan_input(
                    filename, fp,
                    _make_file_work(creds, contents, item_params, signed_uid),
                    user_key,
                ))
        else:
            for fid in ([drive_file_id] if drive_file_id else (drive_file_ids or [])):
                fp = _fingerprint(user_key, f"drive:{fid}", base_params)
                plans.append(_plan_input(
                    fid, fp,
                    _make_drive_work(creds, fid, base_params, signed_uid),
                    user_key,
                ))

        # Nonce semantics (T1.1): consumed at job creation, and only
        # when this request actually starts work. Pure attaches leave
        # the URL unburned. A burned nonce is tolerated exactly when
        # every input that needs work is a RE-ARM of a job this
        # fingerprint already created (the legitimate deploy-retry);
        # a burned nonce with a brand-new job to create is a replay.
        nonce = getattr(request.state, "signed_url_nonce", None)
        needs_create = any(p.kind == "create" for p in plans)
        needs_work = any(p.needs_spawn for p in plans)
        if nonce is not None and needs_work:
            exp = getattr(
                request.state, "signed_url_exp", int(time.time()) + 600
            )
            fresh_nonce = _state._NONCE_STORE.consume(nonce, exp)
            if not fresh_nonce and needs_create:
                return JSONResponse(
                    {
                        "error": "signed URL rejected: URL already used "
                        "(and this request matches no in-flight job to "
                        "attach to). Mint a fresh signed upload URL and "
                        "retry."
                    },
                    status_code=401,
                )

        # Row writes first, spawns second — still zero awaits. A kill
        # landing between the two leaves fresh queued rows that the
        # retry re-arms via their fingerprints.
        for plan in plans:
            if plan.kind == "create":
                plan.job_id = job_store.create_job(user_key, plan.fingerprint)
            elif plan.kind == "rearm":
                assert plan.job_id is not None
                job_store.rearm_job(plan.job_id)
        for plan in plans:
            if plan.needs_spawn:
                assert plan.job_id is not None and plan.work is not None
                plan.task = jobs.start_job(plan.job_id, plan.work)
        # ------------------------------------------------------------
        # End of the no-await critical section.
        # ------------------------------------------------------------

        if batch_mode:
            # A batch is inherently async: N multi-minute conversions
            # cannot ride one HTTP response. 202 + one descriptor per
            # input, each with its own pre-signed status URL.
            return JSONResponse(
                {"jobs": [_job_descriptor(request, p) for p in plans]},
                status_code=202,
            )

        plan = plans[0]
        if async_flag:
            return JSONResponse(_job_descriptor(request, plan), status_code=202)

        # Sync default: await completion and answer exactly like the
        # pre-job-model endpoint (fresh path byte-identical; attach
        # paths add attached_to_existing_job=true).
        outcome = await _sync_outcome(plan)
        return _sync_response(outcome, attached=plan.attached)

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
