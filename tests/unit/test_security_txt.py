"""PR-Δ2 (v2.3.5) — RFC 9116 security.txt endpoint.

RFC 9116 defines ``/.well-known/security.txt`` as the standard machine-
readable location for vulnerability disclosure contact info. The
endpoint serves as the structured companion to the human-readable
``SECURITY.md`` at the repo root.

This file pins:
  - the endpoint exists at the spec-mandated path (regression guard)
  - response is ``text/plain`` per RFC 9116 §3
  - the REQUIRED ``Contact:`` and ``Expires:`` fields are present
  - ``Expires:`` is a valid RFC 3339 datetime in the future (else the
    block is considered stale by spec-conformant scanners)
  - the public-endpoint contract (no bearer required)
"""
from __future__ import annotations

from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _build_well_known_app() -> Starlette:
    """Minimal app with just the security.txt route mounted.

    Mirrors production wiring for this single endpoint; the
    ``BearerTokenMiddleware`` bypass of ``/.well-known/*`` is
    verified separately by the middleware tests — here we exercise
    the handler in isolation."""
    from appscriptly.http_server.routes.observability import security_txt
    return Starlette(routes=[
        Route("/.well-known/security.txt", security_txt, methods=["GET"]),
    ])


def test_security_txt_returns_200_at_well_known_path():
    """Regression guard against accidental route deletion. RFC 9116
    mandates this exact path; scanners + reporters look here first."""
    client = TestClient(_build_well_known_app())
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200, (
        f"RFC 9116 endpoint returned {resp.status_code}; expected 200. "
        f"Body: {resp.text!r}"
    )


def test_security_txt_returns_text_plain_content_type():
    """RFC 9116 §3 requires ``text/plain`` for the response body.
    Canary against an accidental switch to JSONResponse."""
    client = TestClient(_build_well_known_app())
    resp = client.get("/.well-known/security.txt")
    assert resp.headers["content-type"].startswith("text/plain"), (
        f"security.txt must be text/plain per RFC 9116 §3; got "
        f"{resp.headers['content-type']!r}"
    )


def test_security_txt_carries_required_contact_field():
    """RFC 9116 §2.5.4 — ``Contact:`` is REQUIRED. At least one entry
    pointing at the GitHub Security Advisories form (matches the
    ``SECURITY.md`` instructions)."""
    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/security.txt").text
    assert "Contact:" in body, (
        "RFC 9116 REQUIRES a Contact field; not found in response."
    )
    # Match the GitHub Security Advisories URL — the same channel
    # SECURITY.md instructs reporters to use. A drift between the two
    # surfaces would split reports across an inactive channel.
    assert "github.com/Sundeepg98/google-docs-mcp/security/advisories/new" in body, (
        "Contact: must point at the GitHub Security Advisories form "
        "(canonical channel per SECURITY.md)."
    )


def test_security_txt_carries_required_expires_field():
    """RFC 9116 §2.5.5 — ``Expires:`` is REQUIRED. A missing or
    expired field invalidates the entire file per spec, so scanners
    fall back to 'no contact info'."""
    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/security.txt").text
    assert "Expires:" in body, (
        "RFC 9116 REQUIRES an Expires field; not found in response."
    )


def test_security_txt_expires_is_rfc3339_and_in_future():
    """The ``Expires:`` value must be a valid RFC 3339 datetime AND
    must be in the future (else scanners treat the whole file as
    invalid). This is the renewal canary — when the test fires, bump
    ``_SECURITY_TXT_EXPIRES`` in observability.py per the comment
    above it."""
    client = TestClient(_build_well_known_app())
    body = client.get("/.well-known/security.txt").text

    # Parse the Expires: line — RFC 9116 §2.5.5 says exactly one
    # ISO 8601 / RFC 3339 datetime.
    for line in body.splitlines():
        if line.startswith("Expires:"):
            value = line.split(":", 1)[1].strip()
            break
    else:
        raise AssertionError("Expires: line not found")

    # Python 3.11+ handles RFC 3339 'Z' suffix in fromisoformat;
    # for 3.10 compat we replace Z with +00:00.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert parsed > now, (
        f"Expires: must be in the future (RFC 9116 §2.5.5). "
        f"Got {parsed.isoformat()} but now is {now.isoformat()}. "
        f"Bump _SECURITY_TXT_EXPIRES in observability.py."
    )


def test_security_txt_is_publicly_accessible_without_bearer():
    """security.txt is by spec a public discovery endpoint — like the
    RFC 9728 metadata endpoint, no bearer required (scanners and
    researchers hit it without any credential). In production the
    ``BearerTokenMiddleware`` dispatch matcher excludes
    ``/.well-known/*`` from the bearer-required set; this exercises
    the handler-level no-auth-required contract."""
    client = TestClient(_build_well_known_app())
    # No Authorization header at all.
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200
    # Body is real RFC 9116 content, not an auth-required JSON error.
    assert "Contact:" in resp.text
