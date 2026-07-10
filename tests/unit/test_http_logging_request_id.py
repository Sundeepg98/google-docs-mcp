"""BUG 4 (2026-07-09) — the request_id logging WIRING, not just the filter.

``tests/unit/test_request_id_middleware.py`` already proves
``RequestIdLogFilter`` injects the right value when it RUNS. Production
still broke because the filter was attached to the root LOGGER: a
record emitted on a child logger propagates straight to the root
logger's HANDLERS, skipping the root logger's own filters, so the
``[req=%(request_id)s]`` formatter raised ``KeyError('request_id')``
and every real log line arrived wrapped in a "--- Logging error ---"
traceback block.

These tests exercise the REAL configuration path (``run_http`` calls
``configure_http_logging``) end to end: child logger -> propagation ->
root handler -> formatter -> stderr, asserting the formatted output is
clean. ``capfd`` reads the stderr file descriptor, which captures both
the handler's stream writes AND anything ``Handler.handleError`` prints
(the "--- Logging error ---" blocks). This is the coverage that would
have caught BUG 4.
"""
from __future__ import annotations

import io
import logging

import pytest

from appscriptly.http_server.app import configure_http_logging


def _strip_root_handlers() -> None:
    """Reproduce the production boot state: an unconfigured root logger.

    Must run INSIDE the test body — pytest's logging plugin attaches its
    own capture handler to the root logger at the start of every test
    phase, so a setup-time strip (in a fixture) is undone before the
    test body executes and ``basicConfig`` would silently no-op.
    """
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)


@pytest.fixture
def restore_root_logging():
    """Put pytest's root handlers + level back after the test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_child_logger_outside_request_context_emits_cleanly(
    restore_root_logging, capfd
):
    """The exact BUG 4 shape: a named (child) logger records OUTSIDE any
    HTTP request context — startup lines, CallToolRequest, the tenant
    audit log. Must format with the ``-`` placeholder, with zero
    logging-machinery error blocks."""
    _strip_root_handlers()
    configure_http_logging()

    logging.getLogger("appscriptly.credentials").info(
        "tenant audit line emitted outside a request"
    )
    logging.getLogger("mcp.server.lowlevel.server").warning(
        "CallToolRequest-shaped line"
    )

    err = capfd.readouterr().err
    assert "--- Logging error ---" not in err, (
        "propagated child-logger records must not explode the formatter; "
        "the request_id filter belongs on the HANDLER, not the root logger"
    )
    assert "KeyError" not in err
    assert err.count("[req=-]") == 2
    assert "tenant audit line emitted outside a request" in err
    assert "CallToolRequest-shaped line" in err


def test_child_logger_inside_request_context_carries_the_id(
    restore_root_logging, capfd
):
    """Within a request context the same propagated path stamps the
    active id (the middleware sets the ContextVar; here we set it
    directly to isolate the logging wiring from the ASGI plumbing)."""
    from appscriptly.http_server.middleware import _request_id_var

    _strip_root_handlers()
    configure_http_logging()

    token = _request_id_var.set("req-abc-123")
    try:
        logging.getLogger("appscriptly.audit.upload").info("inside request")
    finally:
        _request_id_var.reset(token)

    err = capfd.readouterr().err
    assert "--- Logging error ---" not in err
    assert "[req=req-abc-123]" in err
    assert "inside request" in err


def test_configure_is_safe_when_root_already_has_handlers(restore_root_logging):
    """An embedding process may have configured logging first —
    ``basicConfig`` then no-ops. The filter must still land on the
    pre-existing root handlers so records propagated from child loggers
    carry ``request_id`` no matter which handler processes them."""
    _strip_root_handlers()
    root = logging.getLogger()
    buffer = io.StringIO()
    pre_existing = logging.StreamHandler(buffer)
    pre_existing.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(pre_existing)
    root.setLevel(logging.INFO)

    configure_http_logging()

    assert any(
        f.__class__.__name__ == "RequestIdLogFilter" for f in pre_existing.filters
    ), "pre-existing root handlers must receive the request_id filter"

    logging.getLogger("appscriptly.http").info("still formats")
    output = buffer.getvalue()
    assert "--- Logging error ---" not in output
    assert "still formats" in output
