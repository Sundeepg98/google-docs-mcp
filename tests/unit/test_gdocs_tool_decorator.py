"""``@gdocs_tool`` decorator behavior tests (v2.0.6 / R28 deferral close).

Pre-v2.0.6: every Google-API tool repeated ~5 lines of
``try: creds = _get_credentials(); return work(creds, ...); except
HttpError: raise ToolError(_format_http_error(e))`` boilerplate. R28's
decorator subsumes that envelope.

This test exercises the decorator in isolation so its contract is
guarded independently of any specific tool's migration.

Coverage:
- ``register()`` must be called before use (RuntimeError on misuse)
- ``creds=True`` injects credentials as the first positional arg and
  strips the ``creds`` param from the visible MCP input schema
- ``creds=True`` translates HttpError → ToolError(_format_http_error(e))
- ``creds=False`` is a pure passthrough (no auth fetch, no exception
  rewrapping) — required for tools with custom auth shapes like
  gdocs_setup_apps_script and gdocs_reset_authorization
- ToolError raised inside the function body propagates verbatim
  (the decorator must NOT wrap pre-validation errors)
- Function metadata (__name__, __doc__) survives the wrap
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError


@pytest.fixture
def fresh_decorator_module():
    """Reload decorators module to reset its global bindings between tests."""
    import importlib

    from google_docs_mcp import decorators
    importlib.reload(decorators)
    yield decorators


@pytest.fixture
def stub_mcp_and_helpers(fresh_decorator_module):
    """A minimal FastMCP instance + fake creds/format helpers, registered."""
    mcp = FastMCP("decorator-tests")
    creds_calls = []
    format_calls = []

    def fake_creds():
        creds_calls.append(True)
        return "fake-creds-sentinel"

    def fake_format(e):
        format_calls.append(e)
        return f"formatted: {type(e).__name__}"

    fresh_decorator_module.register(mcp, fake_creds, fake_format)
    return mcp, fresh_decorator_module, creds_calls, format_calls


# ---------------------------------------------------------------------
# register() guard
# ---------------------------------------------------------------------


def test_gdocs_tool_raises_before_register(fresh_decorator_module):
    """Misuse: calling the decorator before register() must fail loudly."""
    with pytest.raises(RuntimeError, match="register"):
        fresh_decorator_module.gdocs_tool(
            title="x", readonly=True, destructive=False,
            idempotent=True, external=False,
        )


# ---------------------------------------------------------------------
# creds=True: injection, signature stripping, HttpError translation
# ---------------------------------------------------------------------


def test_creds_true_injects_creds_as_first_arg(stub_mcp_and_helpers):
    _mcp, decorators, creds_calls, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def my_tool(creds, doc_id: str) -> dict:
        return {"creds": creds, "doc_id": doc_id}

    result = my_tool("DOC123")
    assert result == {"creds": "fake-creds-sentinel", "doc_id": "DOC123"}
    assert len(creds_calls) == 1


def test_creds_true_strips_creds_from_mcp_input_schema(stub_mcp_and_helpers):
    """``creds`` is server-injected; clients must not see it as a tool param."""
    mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def my_tool(creds, doc_id: str, *, verbose: bool = False) -> dict:
        return {"doc_id": doc_id, "verbose": verbose}

    tools = asyncio.run(mcp.list_tools())
    [t] = [t for t in tools if t.name == "my_tool"]
    schema_props = set(t.parameters.get("properties", {}).keys())
    assert "creds" not in schema_props
    assert schema_props == {"doc_id", "verbose"}


def test_creds_true_translates_httperror_to_toolerror(stub_mcp_and_helpers):
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        resp = MagicMock(status=404, reason="Not Found")
        raise HttpError(resp, content=b"")

    with pytest.raises(ToolError, match="formatted: HttpError"):
        my_tool(5)
    assert len(format_calls) == 1


def test_creds_true_propagates_tool_error_verbatim(stub_mcp_and_helpers):
    """Pre-validation errors inside the body must NOT be re-wrapped."""
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        if x < 0:
            raise ToolError("x must be non-negative")
        return {"x": x}

    with pytest.raises(ToolError, match="non-negative"):
        my_tool(-1)
    # _format_http_error must not have been called.
    assert format_calls == []


def test_creds_true_propagates_other_exceptions(stub_mcp_and_helpers):
    """Non-HttpError exceptions must bubble; only HttpError is translated."""
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        my_tool(1)
    assert format_calls == []


def test_creds_true_preserves_function_metadata(stub_mcp_and_helpers):
    _mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def named_tool(creds, x: int) -> dict:
        """My docstring."""
        return {"x": x}

    assert named_tool.__name__ == "named_tool"
    assert named_tool.__doc__ == "My docstring."


# ---------------------------------------------------------------------
# creds=False: pure passthrough
# ---------------------------------------------------------------------


def test_creds_false_does_not_fetch_credentials(stub_mcp_and_helpers):
    _mcp, decorators, creds_calls, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=False, creds=False,
    )
    def my_tool(x: int) -> dict:
        return {"x": x}

    result = my_tool(5)
    assert result == {"x": 5}
    assert creds_calls == []  # never invoked the auth helper


def test_creds_false_does_not_translate_httperror(stub_mcp_and_helpers):
    """``creds=False`` tools handle their own exception shape (e.g.
    gdocs_setup_apps_script returns structured ``status: "failed"``).
    The decorator must not interfere."""
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=False,
    )
    def my_tool(x: int) -> dict:
        resp = MagicMock(status=500, reason="Boom")
        raise HttpError(resp, content=b"")

    with pytest.raises(HttpError):
        my_tool(5)
    assert format_calls == []  # decorator did NOT call _format_http_error


# ---------------------------------------------------------------------
# Annotations are wired to ToolAnnotations correctly
# ---------------------------------------------------------------------


def test_annotations_propagated_to_mcp_tool(stub_mcp_and_helpers):
    mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="A specific title",
        readonly=True, destructive=False, idempotent=True, external=False,
    )
    def my_tool(x: int) -> dict:
        return {"x": x}

    tools = asyncio.run(mcp.list_tools())
    [t] = [t for t in tools if t.name == "my_tool"]
    a = t.annotations
    assert a is not None
    assert a.title == "A specific title"
    assert a.readOnlyHint is True
    assert a.destructiveHint is False
    assert a.idempotentHint is True
    assert a.openWorldHint is False
