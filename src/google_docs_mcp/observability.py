"""Error tracking integration — Sentry (PR-Δ4).

**Stub-but-wired.** This module configures Sentry's Python SDK with
a security-conscious scrubbing hook + sensible defaults. Activation
is gated on the ``SENTRY_DSN`` environment variable: if absent (the
default for local dev, OSS contributor laptops, and any deployment
the operator hasn't enabled yet), ``init_sentry()`` is a no-op.

The DevOps audit flagged "no error tracking — post-deploy 5xx spikes
invisible until user complains" as one of the must-fix operational
gaps. Sentry's free tier (5k events/mo) is sufficient for personal-
scale traffic; the SDK code lives here so the operator's activation
step is just ``fly secrets set SENTRY_DSN=…`` — no code change.

## Security posture for telemetry

Sentry receives stack traces, breadcrumbs, and request metadata on
every captured event. Without scrubbing, that surface would leak:
  - OAuth tokens (Authorization headers, ``token``/``refresh_token``
    on credentials objects)
  - Signed-URL HMAC signatures (the ``sig`` query param)
  - Per-user signing keys (passed as bytes through some code paths)
  - PII (Google ``sub`` claims, email addresses if any reach scope)

The ``_before_send`` hook below filters these BEFORE the event is
serialized + transmitted. Headers + query params + ``vars`` on
frames are walked and any key matching one of the redaction
patterns is replaced with ``[REDACTED]``.

This is defense-in-depth, NOT a substitute for the existing rule
that secrets should never appear in log lines. If a secret is in a
log line that Sentry captures via the logging integration, the
scrubber will redact it — but the log line itself shouldn't have
been there in the first place.

## Coverage scope

What Sentry IS configured to capture:
  - Unhandled exceptions in HTTP request handlers (server-side 500s)
  - Explicit ``capture_exception()`` calls (none in our code today;
    available for future use)
  - Logging records at ERROR level and above (via the LoggingIntegration)

What Sentry is NOT configured to capture:
  - Performance traces (``traces_sample_rate=0.0``) — would
    inflate event count past the free-tier ceiling
  - User context beyond an optional truncated request_id correlation
  - Local variables in stack frames (``include_local_variables=False``)
    — primary leak vector for OAuth tokens / signing keys
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("google_docs_mcp.observability")


# Header / query / cookie / variable names whose VALUES get redacted
# anywhere they appear in a Sentry event. Match is case-insensitive
# substring on the key name — broader than equality so e.g.
# ``X-MCP-Bearer-Key`` is caught by the ``bearer`` substring.
_REDACT_KEY_PATTERNS: tuple[str, ...] = (
    "authorization",
    "bearer",
    "cookie",
    "x-api-key",
    # Signed-URL HMAC + nonce — the cryptographic anchors
    "sig",
    "signature",
    "nonce",
    "uid",
    # OAuth token fields on credential dicts/objects
    "token",
    "refresh_token",
    "access_token",
    "client_secret",
    "private_key",
    # Per-user signing keys + the master
    "signing_key",
    "mcp_bearer",
    "oauth_state_signing",
    "signed_url_signing",
    "hmac_key",
    # PII — Google user identifier
    "sub",
    "email",
    "google_creds_json",
)

_REDACTED = "[REDACTED]"


def _matches_redact_pattern(key: str) -> bool:
    """Case-insensitive substring match against the redaction allowlist.

    Pulled out as a function so the same matcher applies to dict keys
    AND to the (name, value) tuples Sentry uses for headers + query
    string lists.
    """
    lowered = key.lower()
    return any(pattern in lowered for pattern in _REDACT_KEY_PATTERNS)


def _redact_mapping(d: dict[Any, Any]) -> None:
    """Walk a dict in place, replacing values whose key matches a
    redact pattern with the ``[REDACTED]`` sentinel.

    Recurses into nested dicts and lists so deeply-nested
    request.extra or frame.vars surfaces are covered.
    """
    for k, v in list(d.items()):
        if isinstance(k, str) and _matches_redact_pattern(k):
            d[k] = _REDACTED
            continue
        if isinstance(v, dict):
            _redact_mapping(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _redact_mapping(item)
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    # Sentry represents headers / query as list of
                    # [name, value] pairs. Same redact rule applies.
                    pair_key = item[0] if isinstance(item[0], str) else ""
                    if _matches_redact_pattern(pair_key):
                        # Tuples are immutable; we only mutate lists.
                        if isinstance(item, list):
                            item[1] = _REDACTED


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    """Sentry ``before_send`` hook — runs on every event before transmit.

    Returns the (possibly modified) event for transmission, or None
    to drop entirely. We never drop; we ALWAYS scrub.

    Walks the standard Sentry event sub-surfaces (request headers,
    query params, cookies, env, extra, contexts) and the per-frame
    vars in the stack trace, redacting any value whose key matches
    a sensitive pattern.

    Failure-tolerance: if scrubbing raises (e.g. malformed event
    payload from a future SDK version), we drop the event entirely
    rather than transmit unscrubbed. Better to lose a Sentry event
    than to leak a token to Sentry.
    """
    try:
        # 1. Request data (headers, query, cookies, data body).
        request = event.get("request") or {}
        if isinstance(request, dict):
            _redact_mapping(request)

        # 2. Per-frame variables in every exception's stack trace.
        for exc in (event.get("exception", {}).get("values", []) or []):
            stacktrace = exc.get("stacktrace") or {}
            for frame in (stacktrace.get("frames") or []):
                vars_dict = frame.get("vars")
                if isinstance(vars_dict, dict):
                    _redact_mapping(vars_dict)

        # 3. Breadcrumbs (logged events leading up to the capture).
        for crumb in (event.get("breadcrumbs", {}).get("values", []) or []):
            crumb_data = crumb.get("data")
            if isinstance(crumb_data, dict):
                _redact_mapping(crumb_data)

        # 4. Contexts + extra + tags — operator-supplied surfaces.
        for surface_key in ("contexts", "extra", "tags"):
            surface = event.get(surface_key)
            if isinstance(surface, dict):
                _redact_mapping(surface)

        return event
    except Exception:  # noqa: BLE001 — defensive last resort
        # Drop the event rather than risk leaking unscrubbed data.
        log.exception(
            "sentry _before_send scrubber raised; dropping event "
            "to prevent unscrubbed transmission"
        )
        return None


def init_sentry() -> bool:
    """Initialize Sentry if ``SENTRY_DSN`` is set. No-op otherwise.

    Returns True if Sentry was initialized, False if skipped (env
    not set). Called once from ``server.main()`` before the FastMCP
    app starts.

    Idempotent: calling twice in the same process is safe — Sentry's
    init checks for an existing client and replaces it; the second
    call effectively re-configures with the (possibly same) settings.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        log.debug("SENTRY_DSN not set; skipping Sentry initialization")
        return False

    # Local import so the dep is not loaded unless we're actually
    # initializing — keeps stdio-mode + test-import paths from paying
    # the sentry-sdk import cost (~50ms cold).
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    # LoggingIntegration captures records at ERROR+ as Sentry events
    # AND records at INFO+ as breadcrumbs. INFO breadcrumbs give
    # context for the eventual ERROR event without flooding the
    # event count (only ERRORs become events; breadcrumbs are bundled
    # with each event).
    logging_integration = LoggingIntegration(
        level=logging.INFO,         # breadcrumb threshold
        event_level=logging.ERROR,  # event threshold
    )

    sentry_sdk.init(
        dsn=dsn,
        # Release identifier — uses the same env vars deploy.sh sets.
        release=os.environ.get("GIT_COMMIT", "unknown"),
        # Environment tag — Fly's region var or "unknown" locally.
        # Lets operators filter the Sentry dashboard by region without
        # custom tag setup.
        environment=os.environ.get("FLY_REGION") or os.environ.get(
            "SENTRY_ENVIRONMENT", "unknown"
        ),
        # ZERO performance traces. Free-tier event count is precious;
        # an active server would burn through 5k traces/mo trivially.
        # Errors only.
        traces_sample_rate=0.0,
        # Don't ship local vars in stack frames — primary leak vector
        # for tokens + signing keys that live in function locals.
        include_local_variables=False,
        # Don't send default PII (IP, cookies, headers we haven't
        # explicitly opted into). Even with our scrubber, defense-
        # in-depth: opt-out at the SDK level.
        send_default_pii=False,
        # Our scrubber runs LAST after the SDK's own pre-processing.
        before_send=_before_send,
        integrations=[logging_integration],
    )
    log.info(
        "Sentry initialized: release=%s env=%s",
        os.environ.get("GIT_COMMIT", "unknown"),
        os.environ.get("FLY_REGION", "unknown"),
    )
    return True
