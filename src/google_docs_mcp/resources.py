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
            substring matching.
        severity: Roughly "how alarming" — informational, recoverable
            warning, or hard error.
        retriable: Whether retrying the SAME call (after any wait)
            is likely to succeed. False means do not auto-retry.
        wait_seconds: Suggested back-off before retry (only meaningful
            if ``retriable=True``). ``None`` for "no wait" / "n/a".
        do: What the LLM should DO next — short, imperative.
        user_message: What the LLM should SAY to the user — natural
            language, no jargon.
        related_tool: The MCP tool most relevant to recovery, or
            ``None`` if no single tool applies.
    """

    pattern: str
    severity: Literal["info", "warning", "error"]
    retriable: bool
    wait_seconds: NotRequired[int | None]
    do: str
    user_message: str
    related_tool: NotRequired[str | None]


_RECOVERY_TABLE: dict[str, RecoveryEntry] = {
    "no_split_points_found": {
        "pattern": 'warnings: ["no_split_points_found"]',
        "severity": "warning",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "The .docx had no headings or visible split markers, so "
            "the tabbifier could not auto-segment it. Re-run "
            "gdocs_preview_tab_split with explicit split_on markers, "
            "OR fall back to a single-tab import using "
            "gdocs_retrofit_existing_docx with force_single_tab=true."
        ),
        "user_message": (
            "I could not detect any natural section breaks in that "
            "document. Want me to import it as a single tab, or do "
            "you have specific heading text I should split on?"
        ),
        "related_tool": "gdocs_preview_tab_split",
    },
    "owned_by_app_false": {
        "pattern": "owned_by_app: false",
        "severity": "warning",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "The matched Drive file exists but was NOT created by "
            "this MCP server, so write operations (trash, move, "
            "append) will return appNotAuthorizedToFile (403). Do "
            "not attempt destructive writes. Either ask the user to "
            "re-upload the file via this server, or restrict the "
            "workflow to read-only tools."
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
        "pattern": "NeedsReauthError",
        "severity": "error",
        "retriable": True,
        "wait_seconds": None,
        "do": (
            "The user's Google OAuth grant is missing, expired, or "
            "revoked. The response shape includes a fresh auth_url. "
            "Surface that URL to the user as a clickable link — do "
            "NOT silently retry the tool call. After the user "
            "completes consent, retry the original tool call once."
        ),
        "user_message": (
            "Your Google authorization needs a refresh. Please open "
            "the auth_url in the response to re-consent, then tell "
            "me to retry."
        ),
        "related_tool": "gdocs_reset_authorization",
    },
    "apps_script_modified": {
        "pattern": "Apps Script Web App was modified outside this server",
        "severity": "error",
        "retriable": True,
        "wait_seconds": None,
        "do": (
            "The Apps Script Web App attached to this user was "
            "edited (or re-deployed) outside the MCP server's "
            "control, so the cached deployment URL / script_id is "
            "stale. Call gdocs_setup_apps_script to regenerate the "
            "deployment and refresh cached state. Then retry the "
            "original tool once."
        ),
        "user_message": (
            "Your Apps Script helper was changed outside this "
            "connection. I am re-deploying a fresh copy now — one "
            "moment."
        ),
        "related_tool": "gdocs_setup_apps_script",
    },
    "rate_limited": {
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
        "pattern": "placeholder_behavior",
        "severity": "info",
        "retriable": False,
        "wait_seconds": None,
        "do": (
            "The response refers to ambiguous placeholder_behavior — "
            "empty / null tab content was either kept as a literal "
            "empty tab or auto-filled with a placeholder paragraph. "
            "Resolution depends on the caller's intent: if the doc "
            "is a template skeleton, pass placeholder_behavior="
            '"keep_empty"; if it is for human reading, pass '
            'placeholder_behavior="add_placeholder". Re-run the '
            "tool with the explicit kwarg rather than guessing."
        ),
        "user_message": (
            "One of the tabs had no content. Should I leave it "
            "visibly empty, or insert a short 'TBD' placeholder so "
            "the tab is not confusing in the sidebar?"
        ),
        "related_tool": "gdocs_make_tabbed_doc",
    },
}


def _normalize_entry(key: str, entry: RecoveryEntry) -> dict:
    """Return a fully-populated dict (NotRequired fields filled with None).

    Keeps the on-wire payload predictable so callers can rely on
    every field being present.
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
