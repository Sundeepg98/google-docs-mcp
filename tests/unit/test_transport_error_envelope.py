"""Wave-3 S2 (R-B): transient-transport error-envelope detail.

The ``@workspace_tool`` wrapper historically caught only ``HttpError``.
Transient TRANSPORT failures (socket timeout, connection reset/refused,
retryable errno) are NOT ``HttpError``, so they escaped the envelope to
the framework's generic tool-error string stripped of any actionable
detail (the ``gdrive_trash_file`` "flake"). S2 adds a boundary mapping
so those surface as a ``ToolError`` carrying a cause summary + the
request id, using the SAME transient classification the retry chokepoint
uses (``google_api_client.is_retryable_transport_error``).

Discriminating contract (per _audit/2026-07-18-wave3-contract.md, S2a):
- a forced previously-escaping (transient transport) exception now yields
  a DETAILED ToolError envelope (mapping is pinned);
- a transient error RETRIES at the chokepoint, then surfaces detail on
  the final failure;
- a NON-transient exception (a real bug: EACCES, a plain ValueError) is
  NEVER mapped as transient - it propagates untouched ("map, do not
  swallow; never mask non-transient bugs as transient").
"""
from __future__ import annotations

import errno
import socket

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from appscriptly.errors import friendly_transport_error_message


@pytest.fixture
def fresh_decorator_module():
    """Reload decorators to reset its global bindings between tests."""
    import importlib

    from appscriptly import decorators
    importlib.reload(decorators)
    yield decorators


@pytest.fixture
def stub_mcp_and_helpers(fresh_decorator_module):
    """A minimal FastMCP instance + fake creds/format helpers, registered.

    Mirrors the fixture in ``test_gdocs_tool_decorator.py`` so this file
    is self-contained. ``fake_format`` records HttpError formatting so
    tests can assert the transport mapping does NOT route through the
    HTTP formatter.
    """
    mcp = FastMCP("transport-envelope-tests")
    format_calls: list = []

    def fake_creds():
        return "fake-creds-sentinel"

    def fake_format(e):
        format_calls.append(e)
        return f"formatted-http: {type(e).__name__}"

    fresh_decorator_module.register(mcp, fake_creds, fake_format)
    return mcp, fresh_decorator_module, format_calls


def _tool_raising(decorators, exc: BaseException):
    """Build a creds=True tool whose body raises ``exc``."""

    @decorators.workspace_tool(
        title="x", service="drive",
        readonly=True, destructive=False, idempotent=True, external=True,
        creds=True,
    )
    def my_tool(creds) -> dict:
        raise exc

    return my_tool


# ---------------------------------------------------------------------
# Transient transport errors -> detailed ToolError (the mapping)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        socket.timeout("timed out"),
        TimeoutError("read timed out"),
        ConnectionResetError("connection reset by peer"),
        ConnectionRefusedError("refused"),
        OSError(errno.ECONNRESET, "reset via raw errno"),
        OSError(errno.ETIMEDOUT, "timed out via raw errno"),
    ],
)
def test_transient_transport_error_maps_to_detailed_toolerror(
    stub_mcp_and_helpers, exc,
):
    """Each transient transport class now yields a ToolError whose message
    carries the friendly transient detail - NOT the framework's generic
    masked string, and NOT the HTTP formatter."""
    _mcp, decorators, format_calls = stub_mcp_and_helpers
    my_tool = _tool_raising(decorators, exc)

    with pytest.raises(ToolError) as ei:
        my_tool()

    msg = str(ei.value)
    assert "Transient network error contacting the Google API" in msg
    assert "Retryable: true" in msg
    # The exception TYPE name survives so the cause is legible.
    assert type(exc).__name__ in msg
    # It must NOT have been routed through the HttpError formatter.
    assert format_calls == []


# ---------------------------------------------------------------------
# NEGATIVE CONTROLS - never mask a non-transient bug as transient
# ---------------------------------------------------------------------


def test_non_transient_oserror_propagates_unmapped(stub_mcp_and_helpers):
    """An OSError with a NON-retryable errno (EACCES = a config/permission
    bug, not a network blip) must propagate as the raw OSError, never be
    rewritten into the transient ToolError. This is the load-bearing
    'map, do not swallow' guard."""
    _mcp, decorators, _ = stub_mcp_and_helpers
    my_tool = _tool_raising(decorators, OSError(errno.EACCES, "permission denied"))

    with pytest.raises(OSError) as ei:
        my_tool()
    # Still the raw OSError, not a ToolError, and not the transient text.
    assert not isinstance(ei.value, ToolError)
    assert ei.value.errno == errno.EACCES


def test_filenotfound_propagates_unmapped(stub_mcp_and_helpers):
    """FileNotFoundError is an OSError (ENOENT) but NOT transient - it must
    bubble untouched so callers/other layers handle it as before."""
    _mcp, decorators, _ = stub_mcp_and_helpers
    my_tool = _tool_raising(decorators, FileNotFoundError("no such file"))

    with pytest.raises(FileNotFoundError):
        my_tool()


def test_plain_value_error_propagates_unmapped(stub_mcp_and_helpers):
    """A plain ValueError (not an OSError at all) is untouched by the new
    clause - the pre-existing 'other exceptions bubble' contract holds."""
    _mcp, decorators, _ = stub_mcp_and_helpers
    my_tool = _tool_raising(decorators, ValueError("bad input"))

    with pytest.raises(ValueError, match="bad input"):
        my_tool()


def test_httperror_still_routes_through_http_formatter(stub_mcp_and_helpers):
    """Regression guard: adding the OSError clause must not shadow the
    HttpError mapping (HttpError is not an OSError)."""
    from unittest.mock import MagicMock

    _mcp, decorators, format_calls = stub_mcp_and_helpers
    resp = MagicMock(status=503, reason="Service Unavailable")
    my_tool = _tool_raising(decorators, HttpError(resp, content=b""))

    with pytest.raises(ToolError, match="formatted-http: HttpError"):
        my_tool()
    assert len(format_calls) == 1


# ---------------------------------------------------------------------
# Retries THEN detail on final failure (chokepoint + boundary together)
# ---------------------------------------------------------------------


def test_transient_retries_then_maps_detail_on_final_failure(
    stub_mcp_and_helpers, monkeypatch,
):
    """A transient transport error inside an idempotent call is RETRIED by
    the chokepoint (execute_with_retry), and when the retries are
    exhausted the still-transport error escapes the tool body and the
    boundary maps it to the detailed ToolError. Proves both halves in one
    flow: retried (call count == max_attempts) AND detailed on final
    failure."""
    _mcp, decorators, _ = stub_mcp_and_helpers

    from appscriptly.google_api_client import (
        InMemoryGoogleAPIClient,
        RetryingGoogleApiClientAdapter,
        execute_with_retry,
        with_google_api_client,
    )

    # Make retry sleeps instant regardless of the wait strategy.
    monkeypatch.setattr("tenacity.nap.sleep", lambda _s: None)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise ConnectionResetError("connection reset by peer")

    adapter = RetryingGoogleApiClientAdapter(
        InMemoryGoogleAPIClient(), max_attempts=3,
    )

    @decorators.workspace_tool(
        title="x", service="drive",
        readonly=True, destructive=False, idempotent=True, external=True,
        creds=True,
    )
    def my_tool(creds) -> dict:
        return execute_with_retry(flaky, idempotent=True, op_name="test")

    with with_google_api_client(adapter):
        with pytest.raises(ToolError) as ei:
            my_tool()

    assert calls["n"] == 3, (
        "chokepoint should have retried the transient transport error up to "
        f"max_attempts before it escaped (saw {calls['n']} attempts)"
    )
    assert "Transient network error contacting the Google API" in str(ei.value)


# ---------------------------------------------------------------------
# friendly_transport_error_message formatter
# ---------------------------------------------------------------------


def test_formatter_contains_type_reason_and_retryable():
    msg = friendly_transport_error_message(
        ConnectionResetError("connection reset by peer")
    )
    assert "ConnectionResetError" in msg
    assert "connection reset by peer" in msg
    assert "Retryable: true" in msg


def test_formatter_handles_empty_str_exception():
    """An exception whose str() is empty (e.g. socket.timeout(), which is
    TimeoutError on 3.10+) must still NAME the type, with no dangling
    'Type: ' fragment left where the reason would go."""
    exc = socket.timeout()
    name = type(exc).__name__
    msg = friendly_transport_error_message(exc)
    assert name in msg                    # the type is named
    assert f"{name}: ." not in msg        # no dangling 'Type: .'


def test_formatter_includes_request_id_when_present():
    msg = friendly_transport_error_message(
        TimeoutError("x"), request_id="req-abc-123",
    )
    assert "req-abc-123" in msg
    assert "Request ID" in msg


@pytest.mark.parametrize("rid", [None, "-", ""])
def test_formatter_omits_request_id_placeholder(rid):
    """None, the ContextVar '-' default, and '' all mean 'no id' - the
    message must not append a useless Request ID line."""
    msg = friendly_transport_error_message(TimeoutError("x"), request_id=rid)
    assert "Request ID" not in msg


def test_formatter_is_dash_free():
    """Hard rule: no em/en dashes in user-visible strings."""
    msg = friendly_transport_error_message(
        ConnectionResetError("boom"), request_id="req-1",
    )
    assert "—" not in msg  # em dash
    assert "–" not in msg  # en dash


# ---------------------------------------------------------------------
# _current_request_id best-effort probe
# ---------------------------------------------------------------------


def test_current_request_id_none_outside_http_request(fresh_decorator_module):
    """Outside an active HTTP request (unit context), the helper returns
    None whether or not the http_server stack happens to be imported -
    never a raw '-' and never a raise."""
    assert fresh_decorator_module._current_request_id() is None
