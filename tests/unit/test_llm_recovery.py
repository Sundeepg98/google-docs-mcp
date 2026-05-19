"""Unit tests for the v2.2b LLM_RECOVERY artifact bundle.

Covers:
  - the static MCP resource ``gdocs://error-recovery`` (full table)
  - the templated MCP resource ``gdocs://error-recovery/{key}`` (lookup
    + 404-style payload on miss)
  - the ``gdocs_help`` tool (substring match against pattern catalog,
    case-insensitive)
  - the CI guard that asserts ``_RECOVERY_TABLE`` and
    ``docs/LLM_RECOVERY.md`` stay in sync (drift = build failure)
  - the ROUND-TRIP guard: for each live (non-planned) entry, construct
    a realistic failing tool response, ``json.dumps`` it, and assert
    ``gdocs_help`` matches it. Catches "pattern doesn't match wire
    form" bugs at CI time, before they ship dead. Drove the v2.2b
    review-fix commit (4 of 9 original patterns were dead).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "LLM_RECOVERY.md"


def _parse_doc_keys(doc_text: str) -> set[str]:
    """Extract ``## key: <name>`` headings from the recovery markdown.

    The doc convention is one heading per recovery entry. Anything
    else (intro headings, ``## How to extend``) is ignored.
    """
    return set(re.findall(r"^##\s+key:\s+([a-zA-Z0-9_]+)\s*$", doc_text, re.MULTILINE))


def _read_resource_payload(uri: str) -> dict:
    """Read an MCP resource by URI and return the decoded JSON dict.

    FastMCP 3.x ``read_resource`` returns a ``ResourceResult`` whose
    ``.contents[0].content`` is a JSON string for dict-returning
    resource handlers.
    """
    from google_docs_mcp.server import mcp

    result = asyncio.run(mcp.read_resource(uri))
    body = result.contents[0].content
    return json.loads(body)


# Realistic failing tool responses, one per live (non-planned) entry.
# Each value is the actual Python dict / string a tool returns BEFORE
# it gets JSON-encoded for the wire. The round-trip test json.dumps
# it (mirroring MCP transport) and asserts gdocs_help finds a match.
# Sourced from the real emitters cited in each ``_RECOVERY_TABLE``
# entry's comment block — do NOT invent shapes here; only mirror what
# the production code actually produces.
_REALISTIC_RESPONSES: dict[str, object] = {
    "no_split_points_found": {
        # restructure.gs:69 returns this exact shape (Apps Script ->
        # JSON via Drive API). docx_import.py:268 forwards the
        # ``warnings`` list verbatim into the LLM-visible dict.
        "tabs": [{"title": "Untitled", "tabId": "t.0"}],
        "movedChildren": 0,
        "warnings": ["no_splits"],
    },
    "owned_by_app_false": {
        # drive_api.py:392 — find_doc_by_title with verify_writable
        # populates owned_by_app per match.
        "matches": [
            {
                "file_id": "1abcDEF",
                "name": "external.docx",
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                "trashed": False,
                "owned_by_app": False,
            },
        ],
        "count": 1,
    },
    "needs_authorization": (
        # server.py:206-210 raises ToolError with this body. The
        # round-trip test treats it as a pre-rendered string (not a
        # dict) — ToolError messages are returned as plain text by
        # FastMCP, no JSON wrapping.
        "Google API access required.\n\n"
        "**[Click here to authorize](https://example.com/auth?state=abc)"
        "**\n\nAfter granting access, re-run this tool."
    ),
    "rate_limited": (
        # errors.py:69 friendly_http_error_message for HTTP 429.
        "Google API error: 429 Too Many Requests. Details: "
        "[{'reason': 'rateLimitExceeded', 'message': 'Quota exceeded'}]"
    ),
    "app_not_authorized": {
        # drive_api.py:260 — trash_drive_file soft-failure on 403
        # appNotAuthorizedToFile.
        "trashed": False,
        "file_id": "1abcDEF",
        "name": "external.docx",
        "reason": "app_not_authorized",
        "message": (
            "Cannot trash external.docx: this MCP server's OAuth app "
            "lacks write access to this file."
        ),
    },
    "not_found": {
        # drive_api.py:229 — trash_drive_file soft-failure on 404.
        "trashed": False,
        "file_id": "BOGUS_ID",
        "reason": "not_found",
        "message": "Drive returned 404 for file_id BOGUS_ID.",
    },
    "unexpected_exception": (
        # Any leaked KeyError surfaces with this prefix once FastMCP
        # wraps it. Real exception text varies but the class name is
        # the stable substring.
        "Tool execution failed: KeyError: 'expected_field'"
    ),
    "placeholder_behavior": {
        # Hypothetical tool error referencing the kwarg by name. The
        # ValidationError shape (pydantic) embeds the field name as
        # ``loc``, so the bare substring ``placeholder_behavior``
        # appears in real validation errors too.
        "error": (
            "ValidationError: 1 validation error for placeholder_behavior "
            "value is not a valid enumeration member; permitted: "
            "'delete', 'rename', 'keep'"
        ),
    },
}


# ---------------------------------------------------------------------
# resource: gdocs://error-recovery  (static index)
# ---------------------------------------------------------------------


def test_resource_index_returns_full_table():
    """The static index must enumerate every entry in _RECOVERY_TABLE.

    All 9 required v2.2b entries (no_split_points_found,
    owned_by_app_false, needs_authorization, apps_script_modified,
    rate_limited, app_not_authorized, not_found, unexpected_exception,
    placeholder_behavior) must be present, with the fully-normalized
    field shape (including the v2.2b-fix ``planned`` field).
    """
    from google_docs_mcp.resources import _RECOVERY_TABLE

    payload = _read_resource_payload("gdocs://error-recovery")

    assert payload["schema_version"] == 1
    assert isinstance(payload["entries"], list)
    assert len(payload["entries"]) == len(_RECOVERY_TABLE)

    keys_in_payload = {e["key"] for e in payload["entries"]}
    assert keys_in_payload == set(_RECOVERY_TABLE.keys())

    # Every entry must carry the fully-normalized shape, never missing
    # the NotRequired fields (callers should be able to rely on them).
    # ``planned`` was added by the v2.2b review-fix commit so it's
    # required in the normalized output even when False.
    required_fields = {
        "key", "pattern", "severity", "retriable",
        "wait_seconds", "do", "user_message", "related_tool",
        "planned",
    }
    for entry in payload["entries"]:
        assert required_fields.issubset(entry.keys()), (
            f"entry {entry.get('key')!r} missing fields: "
            f"{required_fields - entry.keys()}"
        )


# ---------------------------------------------------------------------
# resource: gdocs://error-recovery/{key}  (templated lookup)
# ---------------------------------------------------------------------


def test_resource_lookup_returns_known_key():
    """Fetching a known key must return the full normalized entry."""
    payload = _read_resource_payload(
        "gdocs://error-recovery/no_split_points_found"
    )

    assert payload["found"] is True
    assert payload["key"] == "no_split_points_found"
    # Pattern was fixed in v2.2b review-fix commit to the REAL emitter
    # token (restructure.gs:69 emits ``warnings: ['no_splits']``).
    assert payload["pattern"] == "no_splits"
    assert payload["severity"] == "warning"
    assert payload["retriable"] is False
    # do + user_message are required action fields — must be non-empty.
    assert payload["do"].strip()
    assert payload["user_message"].strip()


def test_resource_lookup_unknown_key_returns_available_keys():
    """Miss must surface available_keys so the caller can recover."""
    from google_docs_mcp.resources import _RECOVERY_TABLE

    payload = _read_resource_payload(
        "gdocs://error-recovery/garbage_nonexistent_key"
    )

    assert payload["found"] is False
    assert payload["requested_key"] == "garbage_nonexistent_key"
    assert isinstance(payload["available_keys"], list)
    # Must list EVERY registered key, alphabetically (caller may rely
    # on stable ordering for diffing).
    assert payload["available_keys"] == sorted(_RECOVERY_TABLE.keys())
    assert payload["message"]


# ---------------------------------------------------------------------
# tool: gdocs_help  (substring matcher, case-insensitive)
# ---------------------------------------------------------------------


def test_gdocs_help_matches_known_pattern():
    """A real wire-form error string must hit the matching entry.

    Patterns are matched as case-insensitive substrings against the
    JSON-wire form the LLM sees. Round-trip via ``json.dumps`` to
    mirror what MCP transport actually produces.
    """
    from google_docs_mcp.server import gdocs_help

    # Mirror the wire form: tool returns a dict, MCP json-encodes
    # it before handing it to the client.
    response = {
        "tabs": [{"title": "Untitled", "tabId": "t.0"}],
        "warnings": ["no_splits"],
    }
    json_wire = json.dumps(response)

    result = gdocs_help(json_wire)

    assert result["matched"] is True
    assert result["matched_pattern"] == "no_splits"
    assert result["key"] == "no_split_points_found"
    assert result["retriable"] is False
    assert result["severity"] == "warning"
    assert result["do"].strip()
    assert result["user_message"].strip()
    # planned False (live entry).
    assert result["planned"] is False


def test_gdocs_help_no_match_returns_available():
    """Totally unrelated input must return matched=False + suggestion."""
    from google_docs_mcp.resources import _RECOVERY_TABLE
    from google_docs_mcp.server import gdocs_help

    result = gdocs_help("the quick brown fox jumps over the lazy dog")

    assert result["matched"] is False
    assert "available_patterns" in result
    assert isinstance(result["available_patterns"], list)
    # Must include every registered pattern so caller can pivot.
    expected_patterns = {
        entry["pattern"] for entry in _RECOVERY_TABLE.values()
    }
    assert set(result["available_patterns"]) == expected_patterns
    assert result["suggestion"].strip()


def test_gdocs_help_matches_429_rate_limit():
    """Sanity guard: rate_limited pattern works on a real-shaped string."""
    from google_docs_mcp.server import gdocs_help

    result = gdocs_help("Google API error: 429 Too Many Requests. quota...")

    assert result["matched"] is True
    assert result["key"] == "rate_limited"
    assert result["retriable"] is True
    assert result["wait_seconds"] == 60


def test_gdocs_help_is_case_insensitive():
    """v2.2b review-fix: matching must be case-insensitive.

    LLMs sometimes lowercase or normalize error text before passing
    it back; case skew between pattern and haystack must NOT cause
    a miss. errors.py:69 ``friendly_http_error_message`` also
    lowercases ``details_str`` before its own substring search, so
    case-insensitive matching keeps gdocs_help symmetric with the
    rest of the error-handling code.
    """
    from google_docs_mcp.server import gdocs_help

    # Lowercased version of the rate_limited pattern.
    result_lower = gdocs_help("google api error: 429 too many requests")
    assert result_lower["matched"] is True
    assert result_lower["key"] == "rate_limited"

    # Uppercased version.
    result_upper = gdocs_help("GOOGLE API ERROR: 429 TOO MANY REQUESTS")
    assert result_upper["matched"] is True
    assert result_upper["key"] == "rate_limited"

    # Mixed case (the most realistic LLM-normalization scenario).
    result_mixed = gdocs_help('Found: {"OWNED_BY_APP": FALSE} in result')
    assert result_mixed["matched"] is True
    assert result_mixed["key"] == "owned_by_app_false"


# ---------------------------------------------------------------------
# ROUND-TRIP guard: pattern <-> realistic wire form
# ---------------------------------------------------------------------


def _live_keys() -> list[str]:
    """Return keys whose entry is NOT marked ``planned=True``.

    Planned entries are aspirational (no current emitter), so the
    round-trip test cannot construct a realistic response for them.
    """
    from google_docs_mcp.resources import _RECOVERY_TABLE

    return [
        k for k, v in _RECOVERY_TABLE.items()
        if not v.get("planned", False)
    ]


def test_realistic_responses_cover_all_live_keys():
    """The round-trip fixture map must cover every non-planned key.

    If a new live entry is added to ``_RECOVERY_TABLE``, the fixture
    map (``_REALISTIC_RESPONSES`` at top of this file) must grow with
    it. This test fails the build if it doesn't — preventing
    "added entry but skipped wire-form validation" from slipping
    through review.
    """
    live = set(_live_keys())
    covered = set(_REALISTIC_RESPONSES.keys())

    missing = live - covered
    extra = covered - live

    assert not missing, (
        f"Round-trip fixture missing realistic responses for live "
        f"keys: {sorted(missing)}. Add an entry in "
        "_REALISTIC_RESPONSES at the top of this file mirroring "
        "the actual emitter."
    )
    assert not extra, (
        f"Round-trip fixture has stale responses for non-existent / "
        f"planned keys: {sorted(extra)}. Remove from "
        "_REALISTIC_RESPONSES, or flip the entry's planned flag."
    )


@pytest.mark.parametrize("key", sorted(_REALISTIC_RESPONSES.keys()))
def test_round_trip_realistic_response_matches(key):
    """Per-key round trip: realistic response -> json.dumps -> gdocs_help.

    This is the test that would have caught the 4 dead patterns in
    the original v2.2b ship (reviewer flagged: ``no_split_points_found``
    used wrong wire form, ``owned_by_app_false`` was Python-repr-only,
    ``apps_script_modified`` had no emitter, ``placeholder_behavior``
    had wrong enum values).

    For each live entry, take the realistic emitter output, run it
    through ``json.dumps`` to mirror MCP transport behavior, and
    assert ``gdocs_help`` matches it (with the right key).

    Non-dict responses (``ToolError`` bodies, free-form error
    strings) skip the ``json.dumps`` step — they're already string
    on the wire.
    """
    from google_docs_mcp.server import gdocs_help

    response = _REALISTIC_RESPONSES[key]
    if isinstance(response, str):
        wire_text = response
    else:
        wire_text = json.dumps(response)

    result = gdocs_help(wire_text)

    assert result["matched"] is True, (
        f"gdocs_help failed to match wire form for key {key!r}.\n"
        f"  wire_text = {wire_text!r}\n"
        f"  registered pattern = "
        f"{result.get('matched_pattern', '<none>')!r}\n"
        "Either the pattern is wrong (not in the wire form) OR the "
        "_REALISTIC_RESPONSES fixture is wrong (doesn't mirror the "
        "actual emitter). Check the entry's comment block in "
        "resources.py for the cited emitter location."
    )
    assert result["key"] == key, (
        f"gdocs_help matched wrong key for {key!r}: got {result['key']!r}. "
        f"Pattern collision? Patterns: "
        f"{[e['pattern'] for e in [result]]}"
    )


# ---------------------------------------------------------------------
# CI guard: doc <-> table key parity
# ---------------------------------------------------------------------


def test_recovery_table_matches_doc():
    """``_RECOVERY_TABLE`` keys MUST equal ``docs/LLM_RECOVERY.md`` heading keys.

    The doc is the human-facing source of truth; the table is the
    machine-facing mirror. They must never drift — if you add a row
    to one, you add a row to the other. This test fails the build
    if they desync.
    """
    from google_docs_mcp.resources import _RECOVERY_TABLE

    assert DOC_PATH.exists(), (
        f"Expected source-of-truth doc at {DOC_PATH} — missing. "
        "Either restore the file or update DOC_PATH in this test."
    )

    doc_text = DOC_PATH.read_text(encoding="utf-8")
    doc_keys = _parse_doc_keys(doc_text)
    table_keys = set(_RECOVERY_TABLE.keys())

    missing_in_doc = table_keys - doc_keys
    missing_in_table = doc_keys - table_keys

    assert not missing_in_doc, (
        f"Keys present in _RECOVERY_TABLE but missing from "
        f"docs/LLM_RECOVERY.md: {sorted(missing_in_doc)}. "
        "Add a '## key: <name>' section for each."
    )
    assert not missing_in_table, (
        f"Keys present in docs/LLM_RECOVERY.md but missing from "
        f"_RECOVERY_TABLE: {sorted(missing_in_table)}. "
        "Add the matching dict entry in resources.py."
    )

    # Lock in the v2.2b minimum of 9 entries — guards against an
    # accidental wholesale deletion that happens to keep doc+table
    # in sync but empties both.
    assert len(table_keys) >= 9, (
        f"Recovery table has only {len(table_keys)} entries; v2.2b "
        "requires the 9 baseline entries plus any newer additions."
    )


# ---------------------------------------------------------------------
# parametrized spot check: every entry is a complete, valid record
# ---------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "no_split_points_found",
    "owned_by_app_false",
    "needs_authorization",
    "apps_script_modified",
    "rate_limited",
    "app_not_authorized",
    "not_found",
    "unexpected_exception",
    "placeholder_behavior",
])
def test_required_v2_2b_entries_are_complete(key):
    """All 9 baseline v2.2b entries must be fully populated."""
    from google_docs_mcp.resources import _RECOVERY_TABLE

    entry = _RECOVERY_TABLE.get(key)
    assert entry is not None, f"missing required v2.2b entry: {key}"

    assert entry["pattern"], f"{key}: empty pattern"
    assert entry["severity"] in {"info", "warning", "error"}, (
        f"{key}: invalid severity {entry['severity']!r}"
    )
    assert isinstance(entry["retriable"], bool), (
        f"{key}: retriable must be bool, got {type(entry['retriable']).__name__}"
    )
    assert entry["do"].strip(), f"{key}: empty 'do' field"
    assert entry["user_message"].strip(), f"{key}: empty 'user_message' field"
