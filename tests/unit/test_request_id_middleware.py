"""PR-Δ4 — RequestIdMiddleware + RequestIdLogFilter.

Closes the "no request correlation IDs" gap the DevOps audit flagged.
Multi-tenant debugging used to be impossible: a log line saying
"convert failed for user-A" couldn't be linked to upstream/downstream
lines with certainty. After PR-Δ4, every log line emitted inside an
HTTP request handler carries ``request_id=<uuid>``; one grep per
incident reconstructs the full request lifecycle.

This file pins:
  - inbound ``X-Request-ID`` header is honored (claude.ai's proxy may
    pass one; preserving it lets cross-system correlation work)
  - a uuid4 is generated when no inbound id is present
  - oversize / control-char / wrong-charset inbound ids are rejected
    (not echoed verbatim) so a misbehaving upstream cannot DoS log
    storage or smuggle ANSI escapes into operator terminals
  - the response carries the same id in ``X-Request-ID`` so the
    client can correlate from its end
  - logs emitted INSIDE the request handler carry ``request_id`` via
    the ``RequestIdLogFilter`` reading the ContextVar
  - the ContextVar resets between requests so request N+1 never
    inherits request N's id
"""
from __future__ import annotations

import logging
import uuid

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_docs_mcp.http_server import (
    RequestIdLogFilter,
    RequestIdMiddleware,
    get_request_id,
)
from google_docs_mcp.http_server.middleware import (
    REQUEST_ID_HEADER,
    _sanitize_inbound_request_id,
)


# ---------------------------------------------------------------------
# Pure helper — _sanitize_inbound_request_id
# ---------------------------------------------------------------------


def test_sanitize_accepts_canonical_uuid():
    """uuid4 hex form is the canonical id shape."""
    canonical = "0192c0ff-ee00-7e57-9a55-1234567890ab"
    assert _sanitize_inbound_request_id(canonical) == canonical


def test_sanitize_accepts_cloudflare_style_prefix():
    """Cloudflare's ``cf-...`` ray id form is reasonable; accept."""
    cf_id = "cf-8d0c1a2b3c4d5e6f"
    assert _sanitize_inbound_request_id(cf_id) == cf_id


def test_sanitize_rejects_none_and_empty():
    assert _sanitize_inbound_request_id(None) is None
    assert _sanitize_inbound_request_id("") is None


def test_sanitize_rejects_oversize():
    """129 chars > 128-char cap — reject to bound log line size."""
    over = "a" * 129
    assert _sanitize_inbound_request_id(over) is None


def test_sanitize_rejects_control_chars():
    """ANSI escape smuggling: a CR or ESC in a log line can corrupt
    operator terminals. Reject anything outside the allowlist."""
    for bad in ("abc\ndef", "abc\rdef", "abc\x1b[31mred", "abc def"):
        assert _sanitize_inbound_request_id(bad) is None, (
            f"control / space char passed sanitization: {bad!r}"
        )


def test_sanitize_rejects_punctuation_and_path_chars():
    """Characters not in the allowlist (alnum + ``-_.:``) — slashes,
    semicolons, brackets, etc. — get rejected. Unicode LETTERS pass
    because ``str.isalnum()`` accepts them and they don't break log
    parsing (the actual threats are control chars + whitespace,
    covered by the test above). Path-like chars are the realistic
    smuggling vector to fence here."""
    for bad in ("abc/def", "abc;def", "abc[def]", "abc=def", "abc<def>"):
        assert _sanitize_inbound_request_id(bad) is None, (
            f"non-allowlisted char passed sanitization: {bad!r}"
        )


# ---------------------------------------------------------------------
# Middleware behavior — header propagation + ContextVar
# ---------------------------------------------------------------------


def _build_echo_app() -> Starlette:
    """App with a single route that returns the ContextVar value
    captured INSIDE the handler — confirms the middleware set it
    before the handler ran."""
    async def echo(_request):
        return JSONResponse({"request_id_in_handler": get_request_id()})

    return Starlette(
        routes=[Route("/echo", echo, methods=["GET"])],
        middleware=[Middleware(RequestIdMiddleware)],
    )


def test_middleware_generates_uuid_when_no_inbound_header():
    """Default path: no upstream id → generate uuid4 → set ContextVar
    + echo in response header."""
    client = TestClient(_build_echo_app())
    resp = client.get("/echo")
    assert resp.status_code == 200

    response_id = resp.headers.get(REQUEST_ID_HEADER)
    assert response_id is not None, "X-Request-ID missing from response"
    # Should parse as a valid UUID — confirms the generator path.
    parsed = uuid.UUID(response_id)
    assert parsed.version == 4, f"expected uuid4, got version {parsed.version}"

    # The id seen by the handler MUST equal the id echoed in the response.
    assert resp.json()["request_id_in_handler"] == response_id


def test_middleware_honors_inbound_request_id_header():
    """Upstream proxy may pass one — preserve so cross-system
    correlation works end-to-end."""
    client = TestClient(_build_echo_app())
    upstream_id = "cf-upstream-trace-12345"
    resp = client.get("/echo", headers={"X-Request-ID": upstream_id})

    assert resp.headers.get(REQUEST_ID_HEADER) == upstream_id
    assert resp.json()["request_id_in_handler"] == upstream_id


def test_middleware_replaces_misbehaving_inbound_id_with_generated_uuid():
    """An upstream sending a 1KB id or a control-char-laden string
    should NOT have that id echoed back verbatim. We generate a fresh
    uuid4 in those cases — better to lose correlation than smuggle."""
    client = TestClient(_build_echo_app())
    # 200-char oversized id.
    bad_id = "a" * 200
    resp = client.get("/echo", headers={"X-Request-ID": bad_id})

    response_id = resp.headers.get(REQUEST_ID_HEADER)
    assert response_id != bad_id, "oversize id was echoed verbatim — DoS vector"
    # Should be a valid uuid4 (generator fallback).
    parsed = uuid.UUID(response_id)
    assert parsed.version == 4


def test_middleware_resets_contextvar_between_requests():
    """Critical isolation guarantee: request N's id must not leak into
    request N+1's handler (single-threaded TestClient reuses the
    asyncio task)."""
    client = TestClient(_build_echo_app())
    r1 = client.get("/echo", headers={"X-Request-ID": "first"})
    r2 = client.get("/echo")  # no header

    assert r1.json()["request_id_in_handler"] == "first"
    # r2 should have generated its own uuid4, NOT inherited "first".
    second_id = r2.json()["request_id_in_handler"]
    assert second_id != "first"
    # And it should parse as a uuid (generator fallback path).
    uuid.UUID(second_id)


def test_middleware_passes_through_non_http_scopes():
    """Lifespan + websocket scopes get forwarded unchanged. Verified
    by entering a TestClient context (which triggers lifespan
    startup/shutdown) without the middleware raising."""
    client = TestClient(_build_echo_app())
    with client:
        resp = client.get("/echo")
        assert resp.status_code == 200


def test_get_request_id_outside_request_returns_placeholder():
    """Calling ``get_request_id()`` from a stdio tool or a background
    task (no HTTP request in flight) should return ``"-"`` — the
    placeholder default, NOT raise."""
    assert get_request_id() == "-"


# ---------------------------------------------------------------------
# Log filter — request_id propagates to LogRecord
# ---------------------------------------------------------------------


def test_log_filter_injects_request_id_inside_request(caplog):
    """The most operationally important assertion: a log call made
    from INSIDE a request handler gets ``record.request_id`` set to
    the active ContextVar value, NOT the placeholder."""
    test_log = logging.getLogger("test_request_id_filter.in_request")
    test_log.addFilter(RequestIdLogFilter())

    async def emit_log(_request):
        test_log.warning("from handler")
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/emit", emit_log, methods=["GET"])],
        middleware=[Middleware(RequestIdMiddleware)],
    )
    client = TestClient(app)

    inbound_id = "test-handler-corr-id"
    with caplog.at_level(logging.WARNING, logger="test_request_id_filter.in_request"):
        resp = client.get("/emit", headers={"X-Request-ID": inbound_id})

    assert resp.status_code == 200
    # Find OUR record (caplog may capture others from middleware).
    matching = [r for r in caplog.records if r.name == "test_request_id_filter.in_request"]
    assert matching, "test logger emitted no records — caplog miss?"
    assert getattr(matching[0], "request_id", None) == inbound_id, (
        f"request_id not propagated to LogRecord. Got: "
        f"{getattr(matching[0], 'request_id', 'MISSING')!r}"
    )


def test_log_filter_injects_placeholder_outside_request(caplog):
    """A log call from OUTSIDE any request context (e.g. module load
    time, background task) gets the ContextVar default ``"-"`` —
    explicitly NOT None, so formatters don't render literal None."""
    test_log = logging.getLogger("test_request_id_filter.outside_request")
    test_log.addFilter(RequestIdLogFilter())

    with caplog.at_level(logging.WARNING, logger="test_request_id_filter.outside_request"):
        test_log.warning("from background")

    matching = [r for r in caplog.records if r.name == "test_request_id_filter.outside_request"]
    assert matching, "test logger emitted no records — caplog miss?"
    assert matching[0].request_id == "-"


def test_log_filter_preserves_explicit_request_id_attribute(caplog):
    """A caller that explicitly attaches request_id via
    ``log.warning(..., extra={'request_id': X})`` must win over the
    ContextVar lookup — this is the escape hatch for background tasks
    that want to log under a synthetic correlation id."""
    test_log = logging.getLogger("test_request_id_filter.explicit")
    test_log.addFilter(RequestIdLogFilter())

    with caplog.at_level(logging.WARNING, logger="test_request_id_filter.explicit"):
        test_log.warning("explicit", extra={"request_id": "synthetic-bg-id"})

    matching = [r for r in caplog.records if r.name == "test_request_id_filter.explicit"]
    assert matching, "test logger emitted no records — caplog miss?"
    assert matching[0].request_id == "synthetic-bg-id"
