"""User-facing error message helpers.

Wraps known Google API failure modes with caller-actionable guidance.
Both ``server.py`` (MCP tools) and ``http_server.py`` (REST endpoint)
route ``HttpError`` exceptions through ``friendly_http_error_message``
before surfacing to callers.
"""
from __future__ import annotations

from typing import Any

# HTTP status codes Google documents as transient (retry may succeed):
# 429 rate limit, 500/502/503/504 server-side trouble. Every other
# status is a caller/config problem a retry cannot fix. Kept as a
# module-local frozenset (this module stays a leaf: no import edge just
# for 5 integers) but it MUST mirror the retry layer's
# ``google_api_client._RETRYABLE_STATUS`` — a unit test asserts the two
# sets stay identical (tests/unit/test_errors_retryable.py).
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

# Map fragments that appear in Google API error text -> actionable
# explanation. Match is case-insensitive substring. Order matters
# only for tiebreaks (first match wins).
_GUIDANCE: list[tuple[str, str]] = [
    (
        "conversionunsupportedconversionpath",
        "Drive could not import this file as a .docx — typically means the "
        "file is corrupted or was uploaded programmatically with a broken "
        "ZIP central directory. If you're in claude.ai cloud chat, use the "
        "signed-URL flow (call get_signed_upload_url, then POST the .docx "
        "via requests.post) — it bypasses the Drive connector entirely. "
        "If you're working with a Drive file you uploaded by hand and this "
        "still fails, re-upload via the Drive web UI.",
    ),
    (
        "file not found",
        "The Drive file ID could not be resolved. Verify the ID is correct, "
        "the file exists, and the OAuth user has read access.",
    ),
    (
        "invalid_grant",
        "The OAuth token has been revoked or expired. In cloud/HTTP mode, "
        "call the `gdocs_reset_authorization` tool — the next tool call "
        "returns a fresh consent URL. In stdio mode, delete "
        "`~/.google-docs-mcp/token.json` and re-run any tool to trigger "
        "fresh consent.",
    ),
    (
        "insufficient permission",
        "The OAuth scopes granted to this server don't cover this operation. "
        "Re-authenticate to grant the needed scopes (documents + drive.file).",
    ),
    (
        "rate limit exceeded",
        "Hit Google's per-minute rate limit (60 writes / 300 reads per user "
        "per project). Wait a minute and retry, or batch operations.",
    ),
    (
        "internal error encountered",
        "Google's API returned a transient 500. Often resolves on retry. If "
        "this is reproducible (e.g. same input always 500s), it may be a "
        "race condition in the operation sequence — flag the exact request "
        "shape so it can be reordered.",
    ),
]


def friendly_http_error_message(error: Any) -> str:
    """Convert a googleapiclient HttpError into a caller-facing message.

    Returns a string like::

        "Google API error: 400 Bad Request. Details: ...
        Retryable: false
        Guidance: <actionable explanation, if recognized>"

    ``Retryable`` tells the caller (human or agent) whether repeating
    the same call can plausibly succeed: true only for Google's
    documented-transient statuses (429 / 500 / 502 / 503 / 504); every
    4xx validation/auth failure is false. The reason/details lines
    carry Google's own error message through verbatim so the caller
    sees what Google actually said, not a paraphrase.
    """
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        # Rare SDK path where only the raw response was populated —
        # same fallback the retry layer's predicate uses.
        status_code = getattr(getattr(error, "resp", None), "status", None)
    reason = getattr(error, "reason", "")
    details = getattr(error, "error_details", None) or str(error)
    details_str = str(details).lower()

    retryable = "true" if status_code in RETRYABLE_HTTP_STATUS else "false"
    base = (
        f"Google API error: {status_code} {reason}. Details: {details}\n"
        f"Retryable: {retryable}"
    )

    for fragment, guidance in _GUIDANCE:
        if fragment in details_str or fragment in reason.lower():
            return f"{base}\nGuidance: {guidance}"

    return base
