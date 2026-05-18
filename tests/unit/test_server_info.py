"""gdocs_server_info contract tests.

The tools list MUST match tool_count MUST match the actual registered
count. Inconsistency here means the info tool is lying — a regression
trap for change-detection workflows.
"""
from __future__ import annotations

import asyncio


def test_server_info_self_consistency():
    """tool_count == len(tools) == number of FastMCP-registered tools."""
    from google_docs_mcp.server import mcp, gdocs_server_info

    # gdocs_server_info is registered as an async MCP tool; the
    # FastMCP wrapper makes it callable as a coroutine.
    info = asyncio.run(gdocs_server_info())

    assert info["tool_count"] == len(info["tools"])

    # Also verify against the live registry — same source of truth.
    live_count = len(asyncio.run(mcp.list_tools()))
    assert info["tool_count"] == live_count


def test_server_info_tools_is_sorted():
    """Sorted output gives a stable diff for change detection."""
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert info["tools"] == sorted(info["tools"])


def test_server_info_version_string_present():
    """version must be a non-empty string for deploy fingerprinting."""
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert isinstance(info["version"], str)
    assert info["version"]
    assert info["version"] != "unknown", (
        "version came back 'unknown' — package metadata isn't installed; "
        "run `pip install -e .` in the project root for tests."
    )


def test_server_info_includes_build_provenance_keys():
    """build_time and git_commit keys must exist even if values are 'unknown'."""
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert "build_time" in info
    assert "git_commit" in info


def test_server_info_includes_test_suite_block():
    """v1.1.2+ contract: test_suite block surfaces CI status.

    Must always be present — even when the test-results.json file is
    missing (vanilla docker build without deploy.sh) the block returns
    {"status": "unknown"} per the documented contract. Omitting the
    field entirely would break the agreement that a single shape can
    be relied on.
    """
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert "test_suite" in info, (
        "test_suite block missing from gdocs_server_info — "
        "the v1.1.2+ contract requires it always be present"
    )
    suite = info["test_suite"]
    assert isinstance(suite, dict)
    assert "status" in suite
    assert suite["status"] in ("passed", "failed", "unknown")

    # When status is "passed" the full shape applies.
    if suite["status"] == "passed":
        for key in ("last_run", "commit", "passed", "failed", "skipped"):
            assert key in suite, (
                f"test_suite.{key} missing when status='passed'; "
                f"got: {suite!r}"
            )
        assert suite["failed"] == 0, (
            f"status='passed' but failed={suite['failed']} — contradiction"
        )
