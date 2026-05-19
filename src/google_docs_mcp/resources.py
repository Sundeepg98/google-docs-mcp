"""LLM recovery resources — machine-readable mirror of docs/LLM_RECOVERY.md.

Surfaces the same error -> action matrix to LLM clients as MCP
resources, so an agent that hit an unfamiliar failure shape can fetch
structured recovery guidance without scraping the markdown.

A CI guard (``tests/unit/test_llm_recovery.py::test_recovery_table_matches_doc``)
asserts the key set in ``_RECOVERY_TABLE`` here equals the heading set
in ``docs/LLM_RECOVERY.md``. Drift = build failure.

When adding a new entry:
  1. Append to ``_RECOVERY_TABLE`` here.
  2. Add the matching ``## key: <name>`` section to ``docs/LLM_RECOVERY.md``.
  3. Re-run ``uv run pytest tests/unit/test_llm_recovery.py -v``.

Pattern guidance — patterns MUST match the literal wire form an LLM
sees, i.e. the JSON the MCP transport produces from the returned dict.
Python repr (``'key': False``) and JSON (``"key": false``) are NOT
interchangeable; pick the JSON form unless the error is surfaced as a
human-readable string (e.g. ``ToolError`` body, ``friendly_http_error_message``).
The round-trip test in ``test_llm_recovery.py`` enforces this by
``json.dumps``-ing a realistic failing response per key and asserting
``gdocs_help`` matches it.

# === IMPORT-ORDER HAZARD ===
# This module does ``from .server import mcp`` at module level. That
# works ONLY because ``server.py`` defers ``from .resources import
# _RECOVERY_TABLE`` to its very last lines (after ``mcp = FastMCP(...)``
# is bound). If anyone moves that ``from .resources import ...`` up
# in server.py, the import chain becomes circular and Python raises
# ``ImportError: cannot import name 'mcp'`` at startup. Keep the late
# bind at the END of server.py, never higher.
"""
from __future__ import annotations

from typing import Literal, TypedDict

from typing_extensions import NotRequired

from .server import mcp


class RecoveryEntry(TypedDict):
    """One row of the recovery matrix.

    Fields:
        pattern: Substring an agent will see literally in an error
            message / response payload. Used by ``gdocs_help`` for
            substring matching (case-insensitive — both pattern and
            input are lowercased before comparison).
        severity: Roughly "how alarming" — informational, recoverable
            warning, or hard error.
        retriable: Whether retrying the SAME call (after any wait)
            is likely to succeed. False means do not auto-retry.
        wait_seconds: Suggested back-off before retry (only meaningful
            if ``retriable=True``). ``None`` for "no wait" / "n/a".
        do: What the LLM should DO next — short, imperative. Must
            reference REAL kwarg names / enum values from the actual
            tool signatures; do NOT invent hypothetical kwargs.
        user_message: What the LLM should SAY to the user — natural
            language, no jargon.
        related_tool: The MCP tool most relevant to recovery, or
            ``None`` if no single tool applies.
        planned: True if the entry documents a failure shape that no
            current emitter produces (e.g. landing in a future
            release). Omit / False for live patterns. Round-trip
            test skips ``planned=True`` entries (no emitter exists
            to construct a realistic payload from).
    """

    pattern: str
    severity: Literal["info", "warning", "error"]
    retriable: bool
    wait_seconds: NotRequired[int | None]
    do: str
    user_message: str
    related_tool: NotRequired[str | None]
    planned: NotRequired[bool]


_RECOVERY_TABLE: dict[str, RecoveryEntry] = {
    "no_split_points_found": {
        # Real emitter: restructure.gs:69 returns ``warnings: ['no_splits']``
        # which serializes to ``"warnings": ["no_splits"]`` over JSON.
        # The bare token ``no_splits`` is the most robust substring
        # (matches both JSON form and Python repr, future-proof if
        # the warning array is renamed).
        "pattern": "no_splits",
        "severity": "warning",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "The .docx had no headings or visible split markers, so "
            "the tabbifier could not auto-segment it. Re-run "
            "gdocs_preview_tab_split with a different split_by value "
            "(try \"heading_2\", \"page_break\", or \"auto\" instead "
            "of the default \"heading_1\"), OR fall back to retrofit "
            "mode by calling gdocs_tab_existing_doc with an explicit "
            "markers=[{\"marker_text\": str, \"tab_title\": str}, ...] "
            "list — that bypasses heading detection entirely. If the "
            "document is truly single-section, call gdocs_make_tabbed_doc "
            "with one tab whose content is the full document text."
        ),
        "user_message": (
            "I could not detect any natural section breaks in that "
            "document. Want me to try a different split style (e.g. "
            "Heading 2 or page breaks), or do you have specific "
            "phrases I should use as section markers?"
        ),
        "related_tool": "gdocs_preview_tab_split",
    },
    "owned_by_app_false": {
        # Real wire form is JSON: ``"owned_by_app": false`` (lowercase
        # boolean). Python repr would be ``'owned_by_app': False``
        # (capital F, single-quote key) — different string. MCP
        # transports return JSON, so pattern targets JSON form. The
        # case-insensitive matcher in gdocs_help handles either casing.
        "pattern": '"owned_by_app": false',
        "severity": "warning",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "The matched Drive file exists but was NOT created by "
            "this MCP server, so write operations (trash, move, "
            "append) will return appNotAuthorizedToFile (403). Do "
            "not attempt destructive writes. Either ask the user to "
            "re-upload the file via this server, or restrict the "
            "workflow to read-only tools (gdocs_read_doc, "
            "gdocs_get_doc_outline)."
        ),
        "user_message": (
            "I found that document, but it was not created through "
            "this connection — Google's permissions stop me from "
            "editing or deleting files I do not own. I can still "
            "read it. Would you like me to read it, or re-create it "
            "through me?"
        ),
        "related_tool": "gdocs_find_doc_by_title",
    },
    "needs_authorization": {
        # Real emitter: server.py:206-210 raises ``ToolError`` whose
        # body contains ``"Google API access required..."`` and
        # ``"**[Click here to authorize](...)**"``. The bare class
        # name ``NeedsReauthError`` does NOT make it into the LLM-
        # visible string (it's the internal exception type, wrapped).
        # Use the most distinctive substring from the rendered body.
        "pattern": "Click here to authorize",
        "severity": "error",
        "retriable": True,
        "wait_seconds": None,
        "do": (
            "The user's Google OAuth grant is missing, expired, or "
            "revoked. The ToolError body contains a Markdown link to "
            "the consent URL. Surface that URL to the user as a "
            "clickable link — do NOT silently retry the tool call. "
            "After the user completes consent, retry the original "
            "tool call once."
        ),
        "user_message": (
            "Your Google authorization needs a refresh. Please open "
            "the 'Click here to authorize' link in the error message "
            "to re-consent, then tell me to retry."
        ),
        "related_tool": "gdocs_reset_authorization",
    },
    "apps_script_modified": {
        # Aspirational entry — no current emitter produces this string.
        # Lands in v2.0's strict-flip when the server starts
        # detecting out-of-band Apps Script edits (script_id hash
        # mismatch) and refusing to use the stale deployment. Kept
        # in the table now so v2.0 doesn't need to update both
        # docs AND code; flip planned=False when the emitter ships.
        # The round-trip test skips entries with planned=True since
        # there's no real failure response to construct.
        "pattern": "Apps Script Web App was modified outside this server",
        "severity": "error",
        "retriable": True,
        "wait_seconds": None,
        "planned": True,
        "do": (
            "PLANNED v2.0 entry — no current code path emits this "
            "string. When v2.0 ships, the Apps Script Web App "
            "attached to the user will be hash-checked; an out-of-"
            "band edit will surface this error. Recovery: call "
            "gdocs_setup_apps_script to regenerate the deployment "
            "and refresh cached state, then retry the original "
            "tool once."
        ),
        "user_message": (
            "Your Apps Script helper was changed outside this "
            "connection. I am re-deploying a fresh copy now — one "
            "moment."
        ),
        "related_tool": "gdocs_setup_apps_script",
    },
    "rate_limited": {
        # Real emitter: errors.py:69 ``friendly_http_error_message``
        # returns ``f"Google API error: {status_code} {reason}. ..."``.
        # For 429 that's ``"Google API error: 429 Too Many Requests..."``.
        # Substring ``Google API error: 429`` matches.
        "pattern": "Google API error: 429",
        "severity": "warning",
        "retriable": True,
        "wait_seconds": 60,
        "do": (
            "Google returned HTTP 429 (per-user / per-project quota "
            "exceeded). Wait wait_seconds (default 60) before "
            "retrying the SAME call. If retry also 429s, batch "
            "nearby operations into a single gdocs_make_tabbed_doc "
            "or fewer gdocs_append_to_tab calls instead of looping "
            "per-paragraph."
        ),
        "user_message": (
            "Google rate-limited me. I will wait about a minute and "
            "retry — or I can batch the remaining work into one call "
            "if you want to skip the wait."
        ),
        "related_tool": None,
    },
    "app_not_authorized": {
        # Real emitter: drive_api.py:260/493/606 etc. — soft-failure
        # dicts ``{"reason": "app_not_authorized", ...}`` serialized
        # to JSON wire ``"reason": "app_not_authorized"``. Pattern
        # matches that JSON literal.
        "pattern": '"reason": "app_not_authorized"',
        "severity": "warning",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "Soft-failure return (NOT a raised exception). The Drive "
            "file exists but was not created by this MCP, so Google "
            "denied the trash/move/update. Do not retry — Google's "
            "policy will not change on retry. Tell the user the file "
            "is external; offer to read-only it or ask them to delete "
            "it from Drive UI themselves."
        ),
        "user_message": (
            "I cannot delete that file because it was not created "
            "through me. You can remove it directly from Google "
            "Drive, or ask me to read its contents instead."
        ),
        "related_tool": "gdocs_trash_file",
    },
    "not_found": {
        # Real emitter: drive_api.py:229/426/571 etc. — soft-failure
        # dicts ``{"reason": "not_found", ...}`` serialized to JSON
        # ``"reason": "not_found"``. Substring matches JSON literal.
        "pattern": '"reason": "not_found"',
        "severity": "warning",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "Soft-failure return. The file_id passed does not "
            "resolve in the user's Drive — either it was trashed, "
            "the user lacks access, or the ID is wrong. Do NOT "
            "raise; surface the friendly message. Offer to call "
            "gdocs_find_doc_by_title with the user's recollection "
            "of the doc title to recover the correct ID."
        ),
        "user_message": (
            "I could not find that document. It may have been "
            "deleted, or the ID I have is wrong. Do you remember "
            "the title? I can search by name."
        ),
        "related_tool": "gdocs_find_doc_by_title",
    },
    "unexpected_exception": {
        # Real emitter: any unhandled ``KeyError`` / ``AttributeError``
        # / ``TypeError`` that leaks past tool boundaries gets wrapped
        # by FastMCP as a ToolError whose message contains the
        # exception class name. Substring ``KeyError`` matches the
        # most common shape; AttributeError / TypeError are covered
        # by the same pattern family (caller can pass either via
        # gdocs_help and the suggestion field nudges toward
        # gdocs_server_info for bug reports).
        "pattern": "KeyError",
        "severity": "error",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "An exception leaked through the soft-failure boundary "
            "(KeyError, AttributeError, TypeError, etc.). This is "
            "a bug in the server, NOT a user error. Do not "
            "auto-retry — the same call will fail the same way. "
            "Capture gdocs_server_info() (version + commit) and the "
            "exception text and ask the user to file an issue. If "
            "urgent, suggest a workaround using a sibling tool "
            "(e.g. read instead of edit, or single-tab create "
            "instead of multi-tab)."
        ),
        "user_message": (
            "Something unexpected went wrong on the server side. "
            "This looks like a bug — would you mind sharing the "
            "error text so it can be reported? I can try a simpler "
            "workaround in the meantime."
        ),
        "related_tool": "gdocs_server_info",
    },
    "placeholder_behavior": {
        # Real emitter: any tool docstring / error mentioning the
        # ``placeholder_behavior`` kwarg. The enum is defined at
        # server.py:509 as ``Literal["delete", "rename", "keep"]`` —
        # NOT "keep_empty" / "add_placeholder" (those were
        # hypothetical and would cause ValidationError if passed).
        # Pattern is the bare kwarg name; the ``do`` field lists the
        # ACTUAL enum values.
        "pattern": "placeholder_behavior",
        "severity": "info",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "The response refers to the placeholder_behavior kwarg "
            "(controls what gdocs_tab_existing_doc does with the "
            "leading placeholder tab after retrofit). The real "
            "enum (server.py:509) is Literal[\"delete\", \"rename\", "
            "\"keep\"]. Choose by intent: \"delete\" removes the "
            "placeholder once content is split (default, cleanest "
            "for human-readable docs); \"rename\" keeps it as the "
            "tab named by placeholder_title (e.g. \"Overview\"); "
            "\"keep\" leaves it as-is. Re-run the tool with the "
            "explicit enum value rather than guessing."
        ),
        "user_message": (
            "One of the tabs had no content. Should I leave it "
            "visibly empty (keep), rename it to \"Overview\" "
            "(rename), or remove it entirely (delete — the default)?"
        ),
        "related_tool": "gdocs_tab_existing_doc",
    },
}


def _normalize_entry(key: str, entry: RecoveryEntry) -> dict:
    """Return a fully-populated dict (NotRequired fields filled with None).

    Keeps the on-wire payload predictable so callers can rely on
    every field being present. ``planned`` defaults to ``False`` when
    omitted so the wire shape is uniform.
    """
    return {
        "key": key,
        "pattern": entry["pattern"],
        "severity": entry["severity"],
        "retriable": entry["retriable"],
        "wait_seconds": entry.get("wait_seconds"),
        "do": entry["do"],
        "user_message": entry["user_message"],
        "related_tool": entry.get("related_tool"),
        "planned": entry.get("planned", False),
    }


@mcp.resource("gdocs://error-recovery")
def error_recovery_index() -> dict:
    """Full error -> recovery matrix as machine-readable JSON.

    Schema mirror of ``docs/LLM_RECOVERY.md``. Use the templated
    resource ``gdocs://error-recovery/{key}`` for single-entry lookups,
    or the ``gdocs_help`` tool for substring matching against an
    actual error string.
    """
    return {
        "schema_version": 1,
        "doc_url": (
            "https://github.com/Sundeepg98/google-docs-mcp/blob/main/"
            "docs/LLM_RECOVERY.md"
        ),
        "entries": [
            _normalize_entry(k, v) for k, v in _RECOVERY_TABLE.items()
        ],
    }


@mcp.resource("gdocs://error-recovery/{key}")
def error_recovery_entry(key: str) -> dict:
    """Single recovery entry by key.

    Returns the normalized entry dict on hit. On miss, returns a
    structured 404-style payload that includes the list of valid
    keys so the caller can correct course without a second round-trip.
    """
    entry = _RECOVERY_TABLE.get(key)
    if entry is None:
        return {
            "found": False,
            "requested_key": key,
            "available_keys": sorted(_RECOVERY_TABLE.keys()),
            "message": (
                f"No recovery entry registered for key {key!r}. See "
                f"available_keys for the full list, or fetch "
                "gdocs://error-recovery for all entries."
            ),
        }
    return {"found": True, **_normalize_entry(key, entry)}
