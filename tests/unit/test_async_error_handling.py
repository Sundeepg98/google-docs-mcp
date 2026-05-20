"""R23 B2 — verify async tool exception handling does not leak tracebacks.

Currently only ``gdocs_server_info`` is an ``async def`` MCP tool, and
its sole guard is the bare ``except Exception:`` around
``await mcp.list_tools()`` (server.py near line 749-752). The R23
design-internal audit flagged this as a never-tested blindspot:

  > "FastMCP's behavior on async-tool exception: likely 500 with stack
  >  trace in response. Never tested."

These tests pin the desired behaviour now, so any future regression
(e.g. someone removes the guard, or FastMCP changes its default
exception-to-response mapping) trips CI rather than silently
exposing internals to LLM-facing error messages.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_gdocs_server_info_handles_list_tools_failure():
    """``server.py:749 await mcp.list_tools()`` failure must be caught.

    The bare ``except Exception:`` two lines below should set
    ``tool_names = []`` and the tool should still return a
    well-formed dict — never propagate the underlying exception to
    the caller (where FastMCP would surface it as a 500 with a stack
    trace embedded in the response body)."""
    from google_docs_mcp.server import gdocs_server_info

    boom = AsyncMock(side_effect=RuntimeError("boom-secret-internal-state"))
    with patch("google_docs_mcp.server.mcp.list_tools", boom):
        result = await gdocs_server_info()

    # Shape preserved — caller still gets a usable dict.
    assert isinstance(result, dict), (
        f"Exception in list_tools() must not break the return type. "
        f"Got: {type(result).__name__}"
    )
    # Fallback honoured — empty tool list, count = 0.
    assert result.get("tools") == [], (
        f"Expected empty tools list on list_tools() failure. "
        f"Got: {result.get('tools')!r}"
    )
    assert result.get("tool_count") == 0

    # Critical invariant: the raised exception's message must NOT
    # appear anywhere in the response. If FastMCP (or a future
    # refactor) starts forwarding str(exc), this catches it.
    serialised = repr(result).lower()
    assert "boom-secret-internal-state" not in serialised, (
        f"Raw exception text leaked into tool response — would surface "
        f"to LLM-facing error messages. Response: {serialised}"
    )
    assert "traceback" not in serialised, (
        f"Traceback text leaked into tool response. Response: {serialised}"
    )


@pytest.mark.asyncio
async def test_gdocs_server_info_is_async_so_future_async_guards_apply():
    """Pin the async-def contract.

    R23 B2's premise is "only one async tool exists today; if a
    second one lands, the same exception-leak risk applies." Pin
    that ``gdocs_server_info`` is the async-tool exemplar — if it
    flips to sync, the R23 guard becomes vacuous and this test
    fires to force a re-design of the leak protection."""
    import inspect

    from google_docs_mcp.server import gdocs_server_info

    assert inspect.iscoroutinefunction(gdocs_server_info), (
        "gdocs_server_info was async — the R23 B2 leak-protection "
        "test above relies on awaiting it. If it's now sync, either "
        "delete that test (and document why no async tools exist) or "
        "convert it to a sync exception test."
    )
