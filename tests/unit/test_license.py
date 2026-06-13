"""PR-Δ5 — License-key validation seam + middleware.

The license module + middleware exist to make commercial activation a
config flip rather than a code+deploy operation. The default for
personal users is "no enforcement" — these tests pin the
zero-behavior-change contract first, then exercise the
enforcement-on paths.

Three test concerns:

  1. ``check_license`` returns the right ``LicenseStatus`` across the
     env-var matrix (enforcement off vs on; token present vs missing
     vs invalid). The stub verifier always returns True, so all
     "enforcement on + token present" cases yield VALID today; the
     architectural seam is what's pinned.
  2. ``LicenseKeyMiddleware`` integrates with the Starlette middleware
     stack — protected paths get gated, unprotected paths pass
     through, and the response shapes are correct for each case.
  3. The ``X-License-Key`` header takes precedence over the
     ``MCP_LICENSE_KEY`` env var (caller-override discipline).
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


# ---------------------------------------------------------------------
# check_license — the pure-function contract
# ---------------------------------------------------------------------


def test_check_license_returns_DISABLED_when_enforcement_unset(monkeypatch):
    """Default personal-use path: env var unset → DISABLED, token ignored."""
    monkeypatch.delenv("LICENSE_KEY_ENFORCEMENT", raising=False)
    from appscriptly.license import LicenseStatus, check_license

    # Token presence is irrelevant when enforcement is off.
    assert check_license(None).status == LicenseStatus.DISABLED
    assert check_license("anything").status == LicenseStatus.DISABLED
    # The reason text mentions the env-var name so an operator
    # reviewing logs sees the toggle they'd need to flip.
    assert "LICENSE_KEY_ENFORCEMENT" in check_license(None).reason


@pytest.mark.parametrize("falsy_value", ["", "false", "0", "no", "off",
                                          "FALSE", "False", "OFF"])
def test_check_license_treats_falsy_env_values_as_disabled(
    monkeypatch, falsy_value,
):
    """Falsy env var values must keep enforcement off. Case-insensitive."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", falsy_value)
    from appscriptly.license import LicenseStatus, check_license

    assert check_license("any-token").status == LicenseStatus.DISABLED


def test_check_license_returns_INVALID_when_enforcement_on_no_token(
    monkeypatch,
):
    """Enforcement on + no token → INVALID, reason explains both header
    and env-var alternatives so the operator can pick whichever fits
    their deployment."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly.license import LicenseStatus, check_license

    result = check_license(None)
    assert result.status == LicenseStatus.INVALID
    # Empty string also counts as "no token".
    assert check_license("").status == LicenseStatus.INVALID
    # The reason names BOTH supply mechanisms (header + env var).
    assert "X-License-Key" in result.reason
    assert "MCP_LICENSE_KEY" in result.reason


def test_check_license_returns_INVALID_when_stub_verifier_fails_closed(
    monkeypatch,
):
    """Enforcement on + token present, with the UNPATCHED stub verifier
    → INVALID. The stub fails CLOSED (returns False) until a real
    verifier lands, so a bare token is rejected, not accepted. This is
    the security contract: a not-yet-implemented verifier must DENY,
    never grant. When the stub gets swapped for real Stripe
    verification, the VALID path is exercised by monkeypatching the
    verifier (see ``test_check_license_returns_VALID_when_verifier_accepts``)."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly.license import LicenseStatus, check_license

    result = check_license("any-non-empty-token")
    assert result.status == LicenseStatus.INVALID
    assert "rejected" in result.reason.lower()


def test_check_license_returns_VALID_when_verifier_accepts(monkeypatch):
    """When commercial activation swaps the stub for a real verifier
    that ACCEPTS, the VALID path must be reachable. Monkeypatch
    ``_verify_token`` to simulate the post-swap accept behavior — this
    is the seam the stub establishes."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly import license as lic

    monkeypatch.setattr(lic, "_verify_token", lambda _token: True)
    result = lic.check_license("any-non-empty-token")
    assert result.status == lic.LicenseStatus.VALID
    assert "verified" in result.reason.lower()


def test_check_license_returns_INVALID_when_real_verifier_rejects(
    monkeypatch,
):
    """When commercial activation swaps the stub for a real verifier
    that can reject, the INVALID path must be reachable. Monkeypatch
    ``_verify_token`` to simulate post-swap behavior."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly import license as lic

    monkeypatch.setattr(lic, "_verify_token", lambda _token: False)
    result = lic.check_license("revoked-key")
    assert result.status == lic.LicenseStatus.INVALID
    assert "rejected" in result.reason.lower()


# ---------------------------------------------------------------------
# _verify_token — the stub must fail closed and leak no secret material
# ---------------------------------------------------------------------


def test_verify_token_stub_fails_closed():
    """The stub verifier MUST return False for any token. A
    not-yet-implemented verifier denies, never grants — returning True
    would be a latent fail-open the instant enforcement is enabled."""
    from appscriptly.license import _verify_token

    assert _verify_token("any-token") is False
    assert _verify_token("") is False
    # Even a long, structured-looking token is denied (no token is
    # accepted until a real verifier replaces this stub).
    assert _verify_token("license-" + "a" * 40) is False


def test_verify_token_stub_logs_no_token_material(caplog):
    """The stub must not log token contents — not even a prefix. A
    license key is a secret; logging ``token[:8]`` leaked it into log
    sinks. Assert the secret never appears in any captured log record."""
    import logging

    from appscriptly.license import _verify_token

    secret = "fake-license-key-SUPERSECRETVALUE0123456789"
    with caplog.at_level(logging.DEBUG, logger="appscriptly.license"):
        _verify_token(secret)

    combined = "\n".join(rec.getMessage() for rec in caplog.records)
    # Neither the full secret nor its first 8 chars may appear.
    assert secret not in combined
    assert secret[:8] not in combined


def test_check_license_logs_no_token_material_on_rejection(caplog, monkeypatch):
    """End-to-end: enforcement on + a supplied token that fails the
    (fail-closed) verifier must not leak the token into logs anywhere
    along the rejection path."""
    import logging

    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly.license import LicenseStatus, check_license

    secret = "fake-license-key-ANOTHERVALUE9876543210ABCDEF"
    with caplog.at_level(logging.DEBUG, logger="appscriptly.license"):
        result = check_license(secret)

    assert result.status == LicenseStatus.INVALID
    combined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret not in combined
    assert secret[:8] not in combined


# ---------------------------------------------------------------------
# resolve_token_from_env — env-var read helper
# ---------------------------------------------------------------------


def test_resolve_token_from_env_returns_None_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_LICENSE_KEY", raising=False)
    from appscriptly.license import resolve_token_from_env

    assert resolve_token_from_env() is None


def test_resolve_token_from_env_returns_None_for_empty_or_whitespace_value(
    monkeypatch,
):
    """Empty / whitespace-only env var = unset (matches the rest of
    the repo's env-var convention). A literally-blank env var should
    not be treated as "supply a blank license key."
    """
    from appscriptly.license import resolve_token_from_env

    monkeypatch.setenv("MCP_LICENSE_KEY", "")
    assert resolve_token_from_env() is None
    monkeypatch.setenv("MCP_LICENSE_KEY", "   ")
    assert resolve_token_from_env() is None


def test_resolve_token_from_env_strips_surrounding_whitespace(monkeypatch):
    """Operators occasionally paste secrets with trailing newlines; the
    helper strips so the verifier doesn't see the whitespace."""
    monkeypatch.setenv("MCP_LICENSE_KEY", "  secret-key-123  \n")
    from appscriptly.license import resolve_token_from_env

    assert resolve_token_from_env() == "secret-key-123"


# ---------------------------------------------------------------------
# LicenseKeyMiddleware — integration with Starlette
# ---------------------------------------------------------------------


def _build_license_app() -> Starlette:
    """Minimal Starlette app exposing the protected + unprotected paths
    with just the LicenseKeyMiddleware. Lets us drive the middleware in
    isolation from BearerTokenMiddleware (which is tested separately).
    """
    from appscriptly.http_server.middleware import LicenseKeyMiddleware

    async def echo(_request):
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/api/convert", echo, methods=["POST", "GET"]),
            Route("/info", echo, methods=["GET"]),
            # Unprotected paths should pass through regardless.
            Route("/health", echo, methods=["GET"]),
            Route("/mcp", echo, methods=["GET"]),
            Route("/.well-known/security.txt", echo, methods=["GET"]),
        ],
        middleware=[Middleware(LicenseKeyMiddleware)],
    )


def test_middleware_no_op_when_enforcement_disabled(monkeypatch):
    """Personal-use default: no env var set, every request passes
    through regardless of whether a header is supplied."""
    monkeypatch.delenv("LICENSE_KEY_ENFORCEMENT", raising=False)
    client = TestClient(_build_license_app())

    # Protected path, no header — passes through.
    assert client.get("/api/convert").status_code == 200
    # Protected path, with header — passes through (header ignored).
    assert client.get("/info", headers={"x-license-key": "x"}).status_code == 200


def test_middleware_returns_402_when_enforcement_on_no_key(monkeypatch):
    """Enforcement on + no header + no env var = 402 Payment Required.
    Distinct from the 401 BearerTokenMiddleware returns, so monitoring
    can disambiguate auth-missing from license-missing."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    monkeypatch.delenv("MCP_LICENSE_KEY", raising=False)
    client = TestClient(_build_license_app())

    resp = client.get("/api/convert")
    assert resp.status_code == 402, f"got {resp.status_code}: {resp.text!r}"
    body = resp.json()
    assert body["error"] == "license_required"
    # Message guides the operator toward the supply mechanisms.
    assert "X-License-Key" in body["message"]
    assert "MCP_LICENSE_KEY" in body["message"]


def test_middleware_passes_through_when_header_supplies_valid_key(
    monkeypatch,
):
    """Enforcement on + header supplied + verifier accepts = request
    passes through. The header path is the commercial-customer hit
    path. The stub fails closed, so this pins the post-swap behavior by
    monkeypatching the verifier to accept (simulating a real verifier)."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly import license as lic

    monkeypatch.setattr(lic, "_verify_token", lambda _token: True)
    client = TestClient(_build_license_app())

    resp = client.get("/api/convert", headers={"x-license-key": "valid-token"})
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text!r}"


def test_middleware_returns_402_for_header_key_under_failclosed_stub(
    monkeypatch,
):
    """Security regression guard: with the UNPATCHED fail-closed stub,
    enforcement on + a supplied header key must yield 402 (not 200).
    Before the fix the stub returned True, so any token waved a request
    through the instant enforcement was enabled — this pins that the
    gate now denies until a real verifier is wired in."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    monkeypatch.delenv("MCP_LICENSE_KEY", raising=False)
    client = TestClient(_build_license_app())

    resp = client.get("/api/convert", headers={"x-license-key": "any-token"})
    assert resp.status_code == 402, f"got {resp.status_code}: {resp.text!r}"
    assert resp.json()["error"] == "license_required"


def test_middleware_passes_through_when_env_supplies_valid_key(monkeypatch):
    """Enforcement on + env var supplies the key + no header = request
    passes through. The env-var path is the self-hosted customer setup.
    The stub fails closed, so monkeypatch the verifier to accept
    (simulating a real verifier) to exercise the pass-through path."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_LICENSE_KEY", "env-token")
    from appscriptly import license as lic

    monkeypatch.setattr(lic, "_verify_token", lambda _token: True)
    client = TestClient(_build_license_app())

    resp = client.get("/api/convert")
    assert resp.status_code == 200


def test_middleware_header_beats_env_for_key_resolution(monkeypatch):
    """When both are present, the X-License-Key header wins. Lets an
    operator override the env-var default per-request without
    restarting the server.

    Verified indirectly: monkeypatch the verifier to record which
    token it sees, then supply BOTH and assert the header value
    reached the verifier."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_LICENSE_KEY", "env-token")
    from appscriptly import license as lic

    seen: list[str] = []

    def recording_verifier(token: str) -> bool:
        seen.append(token)
        return True

    monkeypatch.setattr(lic, "_verify_token", recording_verifier)
    client = TestClient(_build_license_app())

    client.get("/api/convert", headers={"x-license-key": "header-token"})
    assert "header-token" in seen, (
        f"verifier saw {seen!r} — header should have beaten env var"
    )
    assert "env-token" not in seen, (
        f"env-token reached verifier despite header being present — "
        f"the override discipline broke. seen={seen!r}"
    )


def test_middleware_does_not_gate_unprotected_paths_under_enforcement(
    monkeypatch,
):
    """Enforcement-on must still let /health, /mcp*, /.well-known/*
    through. Otherwise Fly's health probe + claude.ai's connector
    discovery + the OAuth callback flow all break, which is a much
    worse outcome than license-key dispatch."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    monkeypatch.delenv("MCP_LICENSE_KEY", raising=False)
    client = TestClient(_build_license_app())

    # All unprotected paths should be reachable without any key.
    assert client.get("/health").status_code == 200
    assert client.get("/mcp").status_code == 200
    assert client.get("/.well-known/security.txt").status_code == 200


def test_middleware_returns_402_when_real_verifier_rejects_header_key(
    monkeypatch,
):
    """Enforcement on + header supplied + verifier rejects = 402.
    Mirrors the commercial-activation case where Stripe says the
    license expired or was revoked."""
    monkeypatch.setenv("LICENSE_KEY_ENFORCEMENT", "true")
    from appscriptly import license as lic

    monkeypatch.setattr(lic, "_verify_token", lambda _token: False)
    client = TestClient(_build_license_app())

    resp = client.get("/api/convert", headers={"x-license-key": "revoked"})
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "license_required"
    assert "rejected" in body["message"].lower()
