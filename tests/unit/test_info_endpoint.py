"""v2.6 (#48) /info endpoint — bearer-authed observability for preflight.

Replaces a broken `fastmcp client ... call gdocs_server_info`
invocation auth-auditor's R5 pre-mortem caught (fastmcp 3.3.1 has no
`client` subcommand). /info gives the v2.0b preflight script a
curl-shaped surface, gated by the same BearerTokenMiddleware as
/api/*.

Tests:
  1. Bearer-authed GET returns expected JSON shape (counters + ages).
  2. Missing Authorization header → 401.
  3. Wrong bearer → 401.
  4. /info response matches gdocs_server_info's slice — drift between
     the two would let an operator pass the preflight against /info
     while the actual MCP tool reports a problem (or vice versa).
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient


# 32-char master so HKDF / shim paths don't trip on length checks
_TEST_BEARER = "z" * 32


@pytest.fixture(autouse=True)
def setup_keys(monkeypatch):
    """Per-test env + reset counters so assertions are deterministic."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", _TEST_BEARER)
    from google_docs_mcp import keys
    keys._reset_shim_hit_counters_for_tests()
    keys._reset_total_call_counters_for_tests()
    keys._reset_first_call_timestamps_for_tests()
    yield
    keys._reset_shim_hit_counters_for_tests()
    keys._reset_total_call_counters_for_tests()
    keys._reset_first_call_timestamps_for_tests()


def _build_info_app() -> Starlette:
    """Minimal Starlette app with /info gated by the real middleware.

    Reuses the production ``info_endpoint`` + ``BearerTokenMiddleware``
    instead of a hand-rolled stub, so a regression in either gets
    caught here (the whole point is to exercise the live wire-up)."""
    from google_docs_mcp.http_server import (
        BearerTokenMiddleware,
        info_endpoint,
    )
    # The middleware split (v2.6 #48): two separate keys. We pass
    # _TEST_BEARER as both so tests use the existing fixture without
    # standing up a 2nd token. The signed-URL path isn't exercised
    # here — the bearer header path is.
    # v2.0b: BearerTokenMiddleware takes bytes (matches
    # keys.get_key()'s return type). Encode the str fixture at the
    # test boundary.
    bearer_bytes = _TEST_BEARER.encode("utf-8")
    return Starlette(
        routes=[Route("/info", info_endpoint, methods=["GET"])],
        middleware=[Middleware(
            BearerTokenMiddleware,
            bearer_token=bearer_bytes,
            signed_url_key=bearer_bytes,
        )],
    )


# ---------------------------------------------------------------------
# 1. Happy path: bearer-authed GET returns the expected shape
# ---------------------------------------------------------------------


def test_info_endpoint_returns_keys_observability():
    """A correctly-authed GET returns 200 + the contract fields the
    preflight script depends on."""
    from google_docs_mcp import keys

    # Land at least one get_key call so the counters/ages have non-default
    # values to verify (the field shape is the contract, not zero-state).
    keys.get_key("api_bearer")

    client = TestClient(_build_info_app())
    resp = client.get(
        "/info",
        headers={"authorization": f"Bearer {_TEST_BEARER}"},
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text!r}"
    body = resp.json()

    # Contract shape — every field the preflight script parses.
    for field in (
        "version",
        "git_commit",
        "build_time",
        "key_back_compat_shim_active_hits",
        "key_call_totals",
        "key_observability",
    ):
        assert field in body, f"/info missing field {field!r}: {body!r}"

    assert isinstance(body["key_back_compat_shim_active_hits"], dict)
    assert isinstance(body["key_call_totals"], dict)
    assert "first_call_age_seconds" in body["key_observability"]

    # The api_bearer counter we incremented must be visible — proves the
    # endpoint is reading the LIVE counter, not a stale snapshot.
    assert body["key_call_totals"]["api_bearer"] >= 1, (
        f"/info reported stale key_call_totals — expected at least 1 "
        f"api_bearer call (we just made one). Got: {body['key_call_totals']!r}"
    )
    # And first_call_age_seconds must be a float (we just stamped it).
    age = body["key_observability"]["first_call_age_seconds"]["api_bearer"]
    assert isinstance(age, (int, float)) and age >= 0, (
        f"first_call_age_seconds[api_bearer] should be a non-negative "
        f"number after a get_key call; got {age!r}"
    )


# ---------------------------------------------------------------------
# 2. Missing Authorization header → 401
# ---------------------------------------------------------------------


def test_info_endpoint_rejects_missing_bearer():
    """No Authorization header → 401, never the actual info payload."""
    client = TestClient(_build_info_app())
    resp = client.get("/info")
    assert resp.status_code == 401, (
        f"/info served unauthenticated — got {resp.status_code}: "
        f"{resp.text!r}. The bearer gate is broken; observability "
        f"would leak in production."
    )


# ---------------------------------------------------------------------
# 3. Wrong bearer → 401
# ---------------------------------------------------------------------


def test_info_endpoint_rejects_wrong_bearer():
    """A bearer header with the wrong token → 401, not 200."""
    client = TestClient(_build_info_app())
    resp = client.get(
        "/info",
        headers={"authorization": "Bearer not-the-right-token"},
    )
    assert resp.status_code == 401, (
        f"/info accepted a wrong bearer — got {resp.status_code}. "
        f"The hmac.compare_digest gate may have been replaced with "
        f"a weaker check."
    )


# ---------------------------------------------------------------------
# 4. /info matches gdocs_server_info's slice — no drift
# ---------------------------------------------------------------------


def test_info_endpoint_response_matches_gdocs_server_info(monkeypatch):
    """The keys-observability slice of /info MUST equal what
    gdocs_server_info reports. Two surfaces serving different shapes
    would let an operator pass the preflight against /info while the
    MCP tool reports a problem (or vice versa).
    """
    from google_docs_mcp import keys
    from google_docs_mcp.server import gdocs_server_info as _info_tool

    # Land at least one call so both surfaces have non-zero state to
    # compare. The shape contract is what matters; the exact values
    # may drift by a sub-second of clock time between the two reads.
    keys.get_key("signed_url")

    # MCP tool path (calls FastMCP-decorated coroutine).
    # asyncio.run is the modern path; get_event_loop is deprecated
    # outside an existing loop and raises DeprecationWarning under
    # the test harness.
    import asyncio
    tool_result = asyncio.run(_info_tool())

    # HTTP path.
    client = TestClient(_build_info_app())
    http_result = client.get(
        "/info",
        headers={"authorization": f"Bearer {_TEST_BEARER}"},
    ).json()

    # The 3 keys-observability fields must agree on the COUNTS (the
    # ages can drift by a sub-second between the two reads — compare
    # shape only).
    assert tool_result["key_back_compat_shim_active_hits"] == http_result["key_back_compat_shim_active_hits"], (
        "shim hit counters differ between gdocs_server_info and /info "
        "— the two telemetry surfaces have drifted and the preflight "
        "could give a misleading answer."
    )
    assert tool_result["key_call_totals"] == http_result["key_call_totals"], (
        "call totals differ between gdocs_server_info and /info — "
        "telemetry drift."
    )
    # key_observability shape: same set of purposes in first_call_age_seconds
    tool_ages = tool_result["key_observability"]["first_call_age_seconds"]
    http_ages = http_result["key_observability"]["first_call_age_seconds"]
    assert set(tool_ages.keys()) == set(http_ages.keys()), (
        f"key_observability purpose sets differ: tool={set(tool_ages)} "
        f"vs http={set(http_ages)}"
    )
    # Both should have None or a non-negative number for each purpose;
    # exact values can differ by sub-second of clock so compare type, not value.
    for purpose in tool_ages:
        t, h = tool_ages[purpose], http_ages[purpose]
        if t is None:
            assert h is None, (
                f"{purpose!r}: tool=None but http={h!r} — one side "
                f"missed an increment"
            )
        else:
            assert isinstance(h, (int, float)), (
                f"{purpose!r}: tool={t!r} but http={h!r} — type mismatch"
            )
