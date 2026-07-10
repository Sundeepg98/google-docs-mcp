"""``GET /api/convert/status/{job_id}`` - convert-job status + result.

The read side of the T1.1 async job model. The 202 responses from
``POST /api/convert`` carry a ``status_url`` minted by
``build_status_url``: pre-signed with the same ``signed_url`` key as
upload URLs (HMAC over a domain-tagged canonical binding job_id + exp),
valid 24 hours, and deliberately MULTI-use - polling is the point, so
unlike upload URLs there is no nonce.

Auth is enforced in ``BearerTokenMiddleware``: a bearer header passes
(operator), otherwise the ``exp``/``sig`` query params must verify
against the job_id in the path (tampered or expired = 403). By the time
this handler runs the request is authenticated; unknown job ids get a
404 (reachable via bearer, or with a validly-signed URL whose row was
purged after its 7-day retention).

Status vocabulary: ``queued`` | ``running`` | ``done`` | ``error`` are
persisted; ``stalled`` is DERIVED at read time (queued/running with a
heartbeat older than 120s = the owning process died, typically a Fly
deploy). A stalled job resumes under the SAME job_id when the client
re-POSTs the identical convert request within 15 minutes of the job's
creation (fingerprint attach); this URL keeps working across that
re-arm.
"""
from __future__ import annotations

import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from appscriptly import job_store, keys
from appscriptly.crypto import sign_job_status_url
from appscriptly.http_server._helpers import _resolve_base_url

# Path prefix shared with app.py's route table and the middleware's
# signed-status-URL branch. Single definition so the three can't drift.
JOB_STATUS_PATH_PREFIX = "/api/convert/status/"


def build_status_url(request: Request, job_id: str) -> dict[str, Any]:
    """Mint the pre-signed status URL for one job (24h, multi-use).

    Base-URL resolution mirrors the signed-upload mint tool: the
    operator-pinned ``PUBLIC_BASE_URL`` wins (the hostname clients
    actually reach through Fly's edge), falling back to the request's
    own scheme+host for local/dev/test.
    """
    base = os.environ.get("PUBLIC_BASE_URL") or _resolve_base_url(request)
    return sign_job_status_url(
        base_url=f"{base}{JOB_STATUS_PATH_PREFIX}{job_id}",
        signing_key=keys.get_key("signed_url"),
        job_id=job_id,
    )


def job_status_view(row: dict[str, Any]) -> dict[str, Any]:
    """The public JSON shape for one job row (status derivation applied)."""
    derived = job_store.derive_status(row)
    view: dict[str, Any] = {
        "job_id": row["job_id"],
        "status": derived,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "heartbeat_at": row["heartbeat_at"],
    }
    if derived == "done":
        view["result"] = job_store.result_dict(row)
    elif derived == "error":
        err = job_store.error_dict(row) or {}
        view["error"] = err.get("payload")
        # The status this job WOULD have answered synchronously - lets a
        # polling client apply the same handling as a sync caller.
        view["error_http_status"] = err.get("http_status")
    elif derived == "stalled":
        view["note"] = (
            "The server restarted while this job was in flight. Re-POST "
            "the identical convert request (same file bytes or Drive id, "
            "same parameters) within 15 minutes of the job's creation to "
            "resume it under this same job_id; after that window, mint a "
            "fresh signed upload URL and re-upload."
        )
    return view


async def convert_job_status_endpoint(request: Request) -> JSONResponse:
    """``GET /api/convert/status/{job_id}`` - poll one job.

    Always 200 for a known job regardless of the JOB's state (the job's
    failure is data, not a transport failure); 404 for an unknown id.
    """
    job_id = request.path_params["job_id"]
    row = job_store.get_job(job_id)
    if row is None:
        return JSONResponse(
            {
                "error": "unknown job_id (jobs are retained 7 days; "
                "re-POST the convert request to start a new one)"
            },
            status_code=404,
        )
    return JSONResponse(job_status_view(row))
