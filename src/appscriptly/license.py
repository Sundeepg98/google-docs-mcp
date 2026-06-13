"""License-key validation seam (PR-Δ5 — commercial-ready engineering).

This module exists to make commercial activation a wiring change rather
than an architectural one. The default behavior is "no enforcement" —
personal users see zero behavior change. Operator flips
``LICENSE_KEY_ENFORCEMENT=true`` (and, eventually, swaps the
verification stub for real Stripe / similar license-key verification)
to activate the gate.

Three call sites today:

  1. ``LicenseKeyMiddleware`` (``http_server.middleware``) — per-request
     check on protected endpoints in HTTP mode. Extracts the key from
     ``X-License-Key`` (commercial customer hits) or ``MCP_LICENSE_KEY``
     env var (self-hosted commercial customer). On invalid: 402 Payment
     Required with a structured message + a doc URL.

  2. (future) per-tool gating — a ``@requires_license`` decorator that
     wraps tool bodies. Not in this PR. The decorator surface
     (``@workspace_tool``) is the natural attachment point and PR-Δ4's
     ``service=`` annotation gives us a place to declare "this service
     requires a license tier". Deferred until we have at least one
     commercial-only tool to gate.

  3. (future) stdio-mode operator activation — a one-liner in
     ``server.py``'s startup that checks the env-var-set key once at
     boot and refuses to register tools if invalid. Deferred — stdio
     is operator-controlled, so the operator's bearer token IS the
     authorization story today.

**Stub verification.** The current ``_verify_token`` fails CLOSED —
it always returns ``False`` (and logs no token material) until a real
verifier lands. A not-yet-implemented verifier must DENY, never grant,
so that flipping enforcement on can't accidentally wave every token
through. When commercial activation happens, swap the stub for a real
verifier:

  - Stripe license keys: ``stripe.licenses.retrieve(token).active``
  - Self-hosted JWT: ``jwt.decode(token, public_key, algorithms=...)``
    + expiry check
  - Internal license server: ``httpx.get(server, params={"key": token})``

The swap is a single-function edit; the middleware + env-var plumbing
+ HTTP 402 response shape stay unchanged. That's the architectural
seam this module establishes.

**Why an env var (not config file).** Aligns with the existing
operator-config convention in this repo (``MCP_BEARER_TOKEN``,
``GOOGLE_OAUTH_BASE_URL``, ``MCP_BODY_MAX_BYTES``, etc.). Operators
set Fly secrets, not config files. A separate license-config file
would invent a new operator surface for no behavioral benefit.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("appscriptly.license")


class LicenseStatus(enum.Enum):
    """Outcome of a license-key check.

    Three values rather than a boolean because the middleware response
    differs between "no enforcement" (200 — pass through), "valid"
    (200 — pass through), and "invalid" (402 — block). Keeping the
    distinction in the return type means call sites don't have to
    re-check the enforcement flag separately.

    ``DISABLED`` and ``VALID`` both pass through the middleware; they
    differ only in the log line (one says "enforcement off", the
    other says "key verified"). Operators monitoring the
    ``appscriptly.license`` logger can tell at a glance whether
    enforcement is live.
    """

    DISABLED = "disabled"
    """Enforcement off (default). Always returned when
    ``LICENSE_KEY_ENFORCEMENT`` env var is unset, empty, or any
    falsy value (``false``, ``0``, ``no``, etc., case-insensitive)."""

    VALID = "valid"
    """Enforcement is on AND the supplied key passed verification."""

    INVALID = "invalid"
    """Enforcement is on AND the supplied key failed verification
    (or was missing). Middleware translates this into HTTP 402."""


@dataclass(frozen=True)
class LicenseCheckResult:
    """Return shape of ``check_license`` — status + the human-readable
    reason that gets logged + (when ``INVALID``) surfaced to the
    caller via the 402 response body.

    Frozen so call sites can't accidentally mutate the reason string
    between log emission and response construction.
    """

    status: LicenseStatus
    reason: str


# Falsy values for the enforcement flag — anything else (including
# typos like "truee") activates enforcement. The bias is toward
# safety: if an operator sets the var to anything non-empty that
# isn't an obvious off-switch, treat it as ON. They'll see the
# enforcement log line and can fix the typo without a surprise
# accidental-disable.
_ENFORCEMENT_OFF_VALUES = frozenset({"", "false", "0", "no", "off"})


def _is_enforcement_enabled() -> bool:
    """True iff ``LICENSE_KEY_ENFORCEMENT`` env var is set to a truthy
    value. Falsy values + unset = disabled (the personal-use default)."""
    raw = os.environ.get("LICENSE_KEY_ENFORCEMENT", "").strip().lower()
    return raw not in _ENFORCEMENT_OFF_VALUES


def _verify_token(token: str) -> bool:
    """STUB — fails CLOSED (always returns ``False``) until a real
    verifier lands. This is the commercial-activation swap point.

    **Fail-closed by design.** A not-yet-implemented verifier MUST
    deny, never grant: returning ``True`` here would mean that the
    instant an operator flips ``LICENSE_KEY_ENFORCEMENT=true`` the
    "gate" would wave every token through — a latent fail-open. Until
    real verification exists, the safe answer to "is this token
    valid?" is "no". Enforcement is off by default, so this ``False``
    only takes effect once an operator explicitly enables the gate,
    at which point fail-closed is exactly the behavior you want (no
    key is accepted until the real verifier is wired in).

    When commercial activation happens, replace this body with the
    real verifier. The function signature is the contract; everything
    else in this module + the middleware speaks to this function only,
    so the swap is localized. Replacement candidates:

      - Stripe license keys: ``stripe.licenses.retrieve(token).active``
      - Self-hosted JWT: ``jwt.decode(token, public_key, ...)`` + expiry
      - Internal license server: ``httpx.get(server, params={...})``

    Future implementations should be deterministic (same token →
    same result within a reasonable cache window) and fast (sub-
    millisecond at the median) — the middleware calls this on every
    protected request. If the real verifier needs a network round-
    trip (Stripe API), add an in-process LRU cache keyed by token-
    hash with a short TTL (~60s) so a single misbehaving downstream
    can't trip a thundering-herd against Stripe's rate limiter.

    Note: deliberately logs NO token material (not even a prefix) — a
    license key is a secret. ``check_license`` already emits a
    redacted, ``len``-only warning on the rejection path for
    observability when enforcement is on.
    """
    # TODO: real verifier (Stripe / JWT / internal license server).
    # Until then, deny: a stub MUST fail closed, never fail open.
    return False


def check_license(token: str | None) -> LicenseCheckResult:
    """Check the supplied license token against the current enforcement
    config.

    Returns:
        ``LicenseCheckResult(status=DISABLED, reason="enforcement off")``
            when ``LICENSE_KEY_ENFORCEMENT`` is unset / falsy. ``token``
            is ignored — DISABLED short-circuits before the verifier
            runs. This is the personal-use default; no behavior change.

        ``LicenseCheckResult(status=INVALID, reason=...)``
            when enforcement is on AND either (a) no token was supplied,
            or (b) the verifier rejected the supplied token.

        ``LicenseCheckResult(status=VALID, reason=...)``
            when enforcement is on AND the verifier accepted the token.

    Args:
        token: The license key supplied by the caller — typically the
            value of the ``X-License-Key`` HTTP header, or the value
            of the ``MCP_LICENSE_KEY`` env var for self-hosted setups.
            ``None`` is permitted and is treated as "no key supplied"
            (returns INVALID under enforcement).
    """
    if not _is_enforcement_enabled():
        # Personal-use default. Token is intentionally unused — the
        # whole point is zero friction when enforcement is off.
        return LicenseCheckResult(
            status=LicenseStatus.DISABLED,
            reason="LICENSE_KEY_ENFORCEMENT is off (personal-use default)",
        )

    if not token:
        log.warning(
            "license: enforcement on, no token supplied — rejecting"
        )
        return LicenseCheckResult(
            status=LicenseStatus.INVALID,
            reason=(
                "License key required. Supply via the X-License-Key "
                "HTTP header or set MCP_LICENSE_KEY on the server."
            ),
        )

    if _verify_token(token):
        return LicenseCheckResult(
            status=LicenseStatus.VALID,
            reason="License key verified",
        )

    log.warning(
        "license: enforcement on, token failed verification (len=%d)",
        len(token),
    )
    return LicenseCheckResult(
        status=LicenseStatus.INVALID,
        reason=(
            "Supplied license key was rejected by the verifier. "
            "Check the key is current and not revoked."
        ),
    )


def resolve_token_from_env() -> str | None:
    """Return the operator-configured license key from the env, or None.

    Used by the middleware when the caller didn't send an
    ``X-License-Key`` header — self-hosted commercial customers
    typically configure the key once at deploy time rather than
    sending it on every request. The header takes precedence if both
    are present (caller-supplied wins so a temporary override is
    possible without restarting the server).
    """
    raw = os.environ.get("MCP_LICENSE_KEY")
    if raw:
        return raw.strip() or None
    return None
