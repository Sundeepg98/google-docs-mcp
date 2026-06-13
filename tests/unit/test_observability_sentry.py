"""PR-Δ4 — Sentry `before_send` scrubber + init gating.

The Sentry SDK is the post-deploy 5xx observability gap closer
(DevOps audit item 2). But Sentry will receive every captured event
verbatim unless we scrub it first — and our event payloads can carry
OAuth tokens, signing keys, signed-URL HMAC sigs, and the Google `sub`
claim (PII). This file pins the scrubber's behavior so a future
"let me simplify the redact list" change can't silently start
leaking tokens to Sentry.

The init path is gated on `SENTRY_DSN`; absence is a no-op so
local dev / OSS contributors / unset-secret deploys don't try to
talk to a Sentry endpoint that isn't there.

What this file does NOT test:
  - The actual transmission to Sentry (would require live DSN +
    network — out of scope for unit tests).
  - The LoggingIntegration capture path (Sentry-internal; not
    redacted any differently than other events).
  - Performance trace sampling (disabled — `traces_sample_rate=0.0`).
"""
from __future__ import annotations

import os

import pytest

from appscriptly.observability import (
    _before_send,
    _matches_redact_pattern,
    _redact_mapping,
    init_sentry,
)


# ---------------------------------------------------------------------
# _matches_redact_pattern — the substring matcher
# ---------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "Authorization",          # canonical bearer header
    "authorization",
    "X-API-Key",
    "Cookie",
    "set-cookie",
    "X-MCP-Bearer-Key",       # ``bearer`` substring
    "sig",                    # signed-URL HMAC param
    "signature",
    "nonce",
    "uid",                    # per-user identity on signed URLs
    "token",
    "refresh_token",
    "access_token",
    "client_secret",
    "private_key",
    "signing_key",
    "MCP_BEARER_TOKEN",       # env var name (case-insensitive substring)
    "hmac_key",
    "sub",                    # Google PII
    "email",
    "google_creds_json",
])
def test_redact_pattern_matches_sensitive_keys(key):
    """Every key that historically carries a secret should be matched
    by the redact substring list. Adding a new sensitive surface
    requires extending `_REDACT_KEY_PATTERNS` AND adding the key here."""
    assert _matches_redact_pattern(key), (
        f"sensitive key not matched by redact patterns: {key!r}"
    )


@pytest.mark.parametrize("key", [
    "doc_id",
    "tab_id",
    "split_by",
    "X-Request-ID",
    "User-Agent",
    "Content-Type",
    "Content-Length",
    "Host",
    "Accept",
    "tab_count",
])
def test_redact_pattern_does_not_match_safe_keys(key):
    """Operational metadata (doc IDs, tab IDs, tool args, standard
    HTTP headers) must NOT be redacted — they're load-bearing for
    debugging and contain no secrets."""
    assert not _matches_redact_pattern(key), (
        f"safe key falsely matched as sensitive: {key!r}"
    )


# ---------------------------------------------------------------------
# _redact_mapping — recursive in-place dict walk
# ---------------------------------------------------------------------


def test_redact_mapping_replaces_top_level_sensitive_values():
    """Bearer header at the top level of a dict (e.g. request.headers
    after Sentry normalization) gets [REDACTED]."""
    headers = {
        "Authorization": "Bearer real-secret-token-xyz",
        "Content-Type": "application/json",
    }
    _redact_mapping(headers)
    assert headers["Authorization"] == "[REDACTED]"
    assert headers["Content-Type"] == "application/json"


def test_redact_mapping_recurses_into_nested_dicts():
    """Frame.vars often nests credential dicts (e.g. the per-user
    creds JSON has a token field inside an outer dict)."""
    event = {
        "request": {
            "headers": {"Authorization": "Bearer x"},
            "data": {
                "creds": {"refresh_token": "secret-refresh"},
                "doc_id": "DOC123",
            },
        },
    }
    _redact_mapping(event)
    assert event["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert event["request"]["data"]["creds"]["refresh_token"] == "[REDACTED]"
    # Non-sensitive sibling preserved.
    assert event["request"]["data"]["doc_id"] == "DOC123"


def test_redact_mapping_walks_lists_of_dicts():
    """Sentry breadcrumbs are a list of dicts; ditto stacktrace
    frames. Recursion through list elements must work."""
    breadcrumbs = [
        {"data": {"sig": "secret-hmac", "endpoint": "/api/convert"}},
        {"data": {"doc_id": "OK"}},
    ]
    container = {"breadcrumbs": breadcrumbs}
    _redact_mapping(container)
    assert breadcrumbs[0]["data"]["sig"] == "[REDACTED]"
    assert breadcrumbs[0]["data"]["endpoint"] == "/api/convert"
    assert breadcrumbs[1]["data"]["doc_id"] == "OK"


def test_redact_mapping_handles_header_pair_lists():
    """Sentry sometimes represents headers as a list of [name, value]
    pairs (not a dict). The matcher applies to the name element of
    each pair and rewrites the value element of matching pairs."""
    request = {
        "headers": [
            ["Authorization", "Bearer secret"],
            ["Content-Type", "application/json"],
            ["Cookie", "session=secret"],
        ],
    }
    _redact_mapping(request)
    # List-of-pairs path: only mutable list pairs get rewritten.
    assert request["headers"][0] == ["Authorization", "[REDACTED]"]
    assert request["headers"][1] == ["Content-Type", "application/json"]
    assert request["headers"][2] == ["Cookie", "[REDACTED]"]


# ---------------------------------------------------------------------
# _before_send — the full Sentry event-shape walk
# ---------------------------------------------------------------------


def test_before_send_redacts_request_headers():
    """The minimum viable test: an exception captured with a request
    that has an Authorization header should ship with that header
    redacted."""
    event = {
        "request": {
            "headers": {"Authorization": "Bearer real-token"},
            "query_string": "uid=user-A&sig=abc123",
        },
    }
    out = _before_send(event, {})
    assert out is not None
    assert out["request"]["headers"]["Authorization"] == "[REDACTED]"


def test_before_send_redacts_exception_frame_vars():
    """Local variables in stack frames are where tokens MOST often
    leak. ``include_local_variables=False`` in init_sentry() is the
    primary defense; the scrubber here is defense-in-depth."""
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "creds.py",
                                "function": "refresh",
                                "vars": {
                                    "refresh_token": "secret-refresh",
                                    "creds_json": '{"token":"AT","refresh_token":"RT"}',
                                    "user_id": "user-A",
                                },
                            },
                        ],
                    },
                },
            ],
        },
    }
    out = _before_send(event, {})
    assert out is not None
    frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars["refresh_token"] == "[REDACTED]"
    # user_id is safe operational metadata — preserved.
    assert frame_vars["user_id"] == "user-A"


def test_before_send_redacts_breadcrumb_data():
    """Breadcrumbs are the log lines leading up to the error.
    If a log line accidentally contained a secret, the scrubber
    catches it on the way out."""
    event = {
        "breadcrumbs": {
            "values": [
                {"category": "log", "data": {"hmac_key": "secret-key-bytes"}},
                {"category": "log", "data": {"doc_id": "DOC123"}},
            ],
        },
    }
    out = _before_send(event, {})
    assert out is not None
    crumbs = out["breadcrumbs"]["values"]
    assert crumbs[0]["data"]["hmac_key"] == "[REDACTED]"
    assert crumbs[1]["data"]["doc_id"] == "DOC123"


def test_before_send_redacts_extra_and_contexts():
    """Operator-supplied ``extra`` and ``contexts`` can carry anything
    a caller wants attached to the event — must be scrubbed too."""
    event = {
        "extra": {"signing_key": "bytes-of-key", "request_id": "req-abc"},
        "contexts": {"auth": {"bearer": "Bearer X"}},
    }
    out = _before_send(event, {})
    assert out is not None
    assert out["extra"]["signing_key"] == "[REDACTED]"
    assert out["extra"]["request_id"] == "req-abc"
    assert out["contexts"]["auth"]["bearer"] == "[REDACTED]"


def test_before_send_drops_event_when_scrubber_raises(monkeypatch):
    """Failure-tolerance: if scrubbing raises (e.g. malformed event
    payload from a future SDK version), we DROP the event rather
    than transmit potentially-unscrubbed data. Better to lose a
    Sentry event than leak a token."""
    # Force _redact_mapping to raise by passing a malformed event the
    # walker doesn't expect. The most reliable way: monkeypatch the
    # helper itself.
    from appscriptly import observability

    def raises(_d):
        raise RuntimeError("simulated scrubber failure")

    monkeypatch.setattr(observability, "_redact_mapping", raises)

    event = {"request": {"headers": {"Authorization": "Bearer x"}}}
    out = observability._before_send(event, {})
    assert out is None, (
        "Scrubber failure should result in event being dropped, NOT "
        "transmitted with potentially-unscrubbed data."
    )


# ---------------------------------------------------------------------
# init_sentry — env-var gating
# ---------------------------------------------------------------------


def test_init_sentry_noop_when_dsn_unset(monkeypatch):
    """No SENTRY_DSN → init returns False, never imports sentry_sdk.
    This is the default for local dev + OSS contributors; must not
    fail or attempt network."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry() is False


def test_init_sentry_noop_on_empty_dsn(monkeypatch):
    """Whitespace-only DSN — same as unset. Operators sometimes
    accidentally set an empty Fly secret; ignore."""
    monkeypatch.setenv("SENTRY_DSN", "   ")
    assert init_sentry() is False


def test_init_sentry_activates_when_dsn_set(monkeypatch):
    """With a syntactically valid DSN, the init path runs and
    returns True. We use a placeholder DSN that's URL-shaped (so
    sentry-sdk init accepts it) but points at a non-routable host
    so even a stray event won't reach a real Sentry instance.

    CRITICAL teardown step: after this test, the Sentry client is
    active in the process and on interpreter exit it will try to
    flush any buffered events (with a 2-second timeout per event).
    Without explicit teardown, the test runner hangs on the
    flush-on-exit path. ``client.close(timeout=0)`` drops the
    buffer + disables the client without flushing.
    """
    monkeypatch.setenv(
        "SENTRY_DSN", "https://example-key@o0.ingest.sentry.io/0",
    )
    import sentry_sdk

    try:
        result = init_sentry()
        assert result is True
        # Confirm Sentry actually loaded. sentry-sdk 2.x replaced the
        # ``Hub.current.client`` API with ``get_client()``.
        client = sentry_sdk.get_client()
        assert client is not None
        assert client.is_active(), "Sentry client should be active after init"
    finally:
        # Drop any buffered events and disable the client so the
        # process exit path doesn't try to flush to the placeholder
        # DSN (would hang the test runner for ~2s per event).
        client = sentry_sdk.get_client()
        if client is not None and client.is_active():
            client.close(timeout=0)
