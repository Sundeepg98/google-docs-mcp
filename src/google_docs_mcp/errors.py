"""User-facing error message helpers.

Wraps known Google API failure modes with caller-actionable guidance.
Both ``server.py`` (MCP tools) and ``http_server.py`` (REST endpoint)
route ``HttpError`` exceptions through ``friendly_http_error_message``
before surfacing to callers.
"""
from __future__ import annotations

from typing import Any

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
        Guidance: <actionable explanation, if recognized>"
    """
    status_code = getattr(error, "status_code", None)
    reason = getattr(error, "reason", "")
    details = getattr(error, "error_details", None) or str(error)
    details_str = str(details).lower()

    base = f"Google API error: {status_code} {reason}. Details: {details}"

    for fragment, guidance in _GUIDANCE:
        if fragment in details_str or fragment in reason.lower():
            return f"{base}\nGuidance: {guidance}"

    return base
