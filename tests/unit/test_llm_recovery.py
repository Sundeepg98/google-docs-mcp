"""Unit tests for the v2.2b LLM_RECOVERY artifact bundle.

Covers:
  - the static MCP resource ``gdocs://error-recovery`` (full table)
  - the templated MCP resource ``gdocs://error-recovery/{key}`` (lookup
    + 404-style payload on miss)
  - the ``gdocs_help`` tool (substring match against pattern catalog)
  - the CI guard that asserts ``_RECOVERY_TABLE`` and
    ``docs/LLM_RECOVERY.md`` stay in sync (drift = build failure)
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


# ---------------------------------------------------------------------
# resource: gdocs://error-recovery  (static index)
# ---------------------------------------------------------------------


def test_resource_index_returns_full_table():
    """The static index must enumerate every entry in _RECOVERY_TABLE.

    All 9 required v2.2b entries (no_split_points_found,
    owned_by_app_false, needs_authorization, apps_script_modified,
    rate_limited, app_not_authorized, not_found, unexpected_exception,
    placeholder_behavior) must be present.
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
    required_fields = {
        "key", "pattern", "severity", "retriable",
        "wait_seconds", "do", "user_message", "related_tool",
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
    assert payload["pattern"] == 'warnings: ["no_split_points_found"]'
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
# tool: gdocs_help  (substring matcher)
# ---------------------------------------------------------------------


def test_gdocs_help_matches_known_pattern():
    """A real-world-shaped error string must hit the matching entry.

    Patterns are registered as Python-dict-repr style (the form an
    agent sees when it dumps a returned dict with ``str(result)``).
    The test mirrors that representation deliberately — if the
    canonical pattern shape changes (e.g. flipped to JSON), the
    test signal must change with it.
    """
    from google_docs_mcp.server import gdocs_help

    # Simulate the Python-repr the LLM sees if it str()-renders the
    # tool result, e.g. after a preview_tab_split call returned
    # {"warnings": ["no_split_points_found"], ...}.
    fake_error = (
        "gdocs_preview_tab_split result: "
        '{"tabs": [...], warnings: ["no_split_points_found"]}'
    )

    result = gdocs_help(fake_error)

    assert result["matched"] is True
    assert result["matched_pattern"] == 'warnings: ["no_split_points_found"]'
    assert result["key"] == "no_split_points_found"
    assert result["retriable"] is False
    assert result["severity"] == "warning"
    assert result["do"].strip()
    assert result["user_message"].strip()


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
