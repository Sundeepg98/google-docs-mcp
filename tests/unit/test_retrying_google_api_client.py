"""RetryingGoogleApiClientAdapter tests (PR-Δ3 / 2026-05-27).

Closes the Hex specialist's finding that ZERO retry code existed for
Google's routine 429 / 5xx responses. Tests the contract from the
spec body:

- Idempotent calls retry on 429 / 500 / 502 / 503 / 504; eventually
  succeed when the transient passes.
- Non-idempotent calls do NOT retry — first error propagates.
- Non-retryable errors (4xx other than 429, network errors) propagate
  immediately even when idempotent.
- ``Retry-After`` header sets a floor for the next attempt.
- After ``max_attempts`` retries, the underlying HttpError surfaces
  (NOT tenacity's RetryError — we set reraise=True).
- ``get_service`` is pure delegation; no retry happens at Resource
  construction (that call is local and non-flaky).
- Protocol conformance — composing wrapper still satisfies
  GoogleAPIClient at runtime.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from appscriptly.google_api_client import (
    GoogleAPIClient,
    GoogleApiClientAdapter,
    InMemoryGoogleAPIClient,
    RetryingGoogleApiClientAdapter,
    execute_with_retry,
    with_google_api_client,
)


# ---------------------------------------------------------------------
# Helpers — build synthetic HttpError instances with a chosen status.
# ---------------------------------------------------------------------


class _FakeResp(dict):
    """Mimics ``googleapiclient.http.HttpRequest.Response``.

    Subclass of ``dict`` so ``resp.get("retry-after")`` works (that's
    how googleapiclient surfaces headers). ``.status`` attribute set
    so HttpError's status_code property resolves.
    """

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__()
        self.status = status
        self.reason = "Synthetic"
        if headers:
            for k, v in headers.items():
                self[k.lower()] = v


def _http_error(status: int, *, retry_after: str | None = None) -> HttpError:
    headers = {"retry-after": retry_after} if retry_after is not None else None
    resp = _FakeResp(status, headers)
    return HttpError(resp=resp, content=b"")


def _make_adapter(
    *,
    max_attempts: int = 3,
    base_wait: float = 0.001,  # tiny: keep tests fast
    max_wait: float = 0.01,
) -> RetryingGoogleApiClientAdapter:
    inner = InMemoryGoogleAPIClient({("drive", "v3"): MagicMock()})
    return RetryingGoogleApiClientAdapter(
        inner,
        max_attempts=max_attempts,
        base_wait_seconds=base_wait,
        max_wait_seconds=max_wait,
    )


# ---------------------------------------------------------------------
# Protocol + delegation
# ---------------------------------------------------------------------


def test_retrying_adapter_satisfies_protocol():
    """The composing adapter MUST still satisfy GoogleAPIClient — that
    invariant is what lets us layer it without touching call sites."""
    adapter = _make_adapter()
    assert isinstance(adapter, GoogleAPIClient)


def test_get_service_is_pure_delegation_no_retry():
    """``get_service`` returns a Resource; that call is local and never
    flaky. Retry MUST NOT happen here — only ``execute_with_retry``
    wraps the ``.execute()`` callable that hits the network."""
    inner = MagicMock(spec=GoogleApiClientAdapter)
    inner.get_service.return_value = MagicMock(name="drive-resource")
    adapter = RetryingGoogleApiClientAdapter(inner, max_attempts=3)

    fake_creds = MagicMock()
    result = adapter.get_service("drive", "v3", credentials=fake_creds)

    inner.get_service.assert_called_once_with("drive", "v3", credentials=fake_creds)
    assert result is inner.get_service.return_value


# ---------------------------------------------------------------------
# Idempotent retry: 429 + each 5xx code
# ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_idempotent_call_retries_on_transient_then_succeeds(status: int):
    """Each documented transient status triggers a retry; if the next
    attempt succeeds, the caller sees the successful return value."""
    adapter = _make_adapter(max_attempts=3)
    sequence: list[Any] = [_http_error(status), "ok"]

    def fn():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    result = adapter.execute_with_retry(fn, idempotent=True, op_name="drive.files.get")
    assert result == "ok"
    assert sequence == []  # both invocations consumed


def test_idempotent_call_eventually_succeeds_after_max_minus_one_retries():
    """Exactly ``max_attempts`` total invocations are made; the last
    one succeeds → caller sees success even with 2 prior failures."""
    adapter = _make_adapter(max_attempts=3)
    sequence: list[Any] = [_http_error(503), _http_error(503), "third-time-lucky"]

    def fn():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    result = adapter.execute_with_retry(fn, idempotent=True)
    assert result == "third-time-lucky"
    assert sequence == []


def test_max_retries_exhausted_reraises_underlying_http_error():
    """When all attempts fail with retryable errors, the original
    ``HttpError`` propagates — NOT tenacity's ``RetryError``. We pass
    ``reraise=True`` so callers can catch on the same exception type
    they'd see in a single-attempt world."""
    adapter = _make_adapter(max_attempts=3)
    raised_errors = [_http_error(503) for _ in range(3)]
    sequence = list(raised_errors)

    def fn():
        raise sequence.pop(0)

    with pytest.raises(HttpError) as exc_info:
        adapter.execute_with_retry(fn, idempotent=True)

    # The last error (the third 503) is what surfaces.
    assert exc_info.value is raised_errors[-1]
    assert sequence == []


# ---------------------------------------------------------------------
# Non-idempotent: NEVER retry
# ---------------------------------------------------------------------


def test_non_idempotent_call_does_not_retry_on_transient():
    """Mutating operations MUST execute exactly once. Re-executing a
    partially-completed mutation risks duplicate side effects
    (duplicate docs, duplicate deploys, duplicate sends). The first
    transient error propagates without a retry attempt."""
    adapter = _make_adapter(max_attempts=3)
    calls = 0
    err = _http_error(503)

    def fn():
        nonlocal calls
        calls += 1
        raise err

    with pytest.raises(HttpError) as exc_info:
        adapter.execute_with_retry(fn, idempotent=False)

    assert exc_info.value is err
    assert calls == 1, "non-idempotent op was retried; this risks duplicate side effects"


def test_non_idempotent_call_returns_success_value_on_first_success():
    """idempotent=False with a successful first attempt is a normal
    pass-through; the value flows back as-is."""
    adapter = _make_adapter(max_attempts=3)

    def fn():
        return {"docId": "abc123"}

    result = adapter.execute_with_retry(fn, idempotent=False)
    assert result == {"docId": "abc123"}


# ---------------------------------------------------------------------
# Non-retryable errors: 4xx (non-429), network, programmer bugs
# ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_non_429_4xx_errors_do_not_retry_even_when_idempotent(status: int):
    """4xx errors are caller bugs — auth failures, validation errors,
    missing resources. Retrying them just hammers the API with the
    same bad request. Only 429 (rate limit) is retryable in the 4xx
    range."""
    adapter = _make_adapter(max_attempts=3)
    calls = 0
    err = _http_error(status)

    def fn():
        nonlocal calls
        calls += 1
        raise err

    with pytest.raises(HttpError):
        adapter.execute_with_retry(fn, idempotent=True)

    assert calls == 1, f"status {status} should not retry (only 429 + 5xx do)"


def test_non_http_exception_does_not_retry():
    """Network errors, type errors, etc. propagate immediately. Retry
    is scoped to ``HttpError`` with retryable status; other exception
    types are programmer/infrastructure bugs that retrying would just
    delay surfacing."""
    adapter = _make_adapter(max_attempts=3)
    calls = 0

    def fn():
        nonlocal calls
        calls += 1
        raise ValueError("intentional non-HttpError")

    with pytest.raises(ValueError, match="intentional"):
        adapter.execute_with_retry(fn, idempotent=True)

    assert calls == 1


# ---------------------------------------------------------------------
# Retry-After header honored as floor
# ---------------------------------------------------------------------


def test_retry_after_header_is_honored_as_wait_floor(monkeypatch):
    """Google sets ``Retry-After`` on many 429s. The adapter MUST wait
    AT LEAST that long before the next attempt — even when our own
    backoff would be shorter — so we don't immediately re-trip the
    server's rate-limit decision."""
    from appscriptly import google_api_client as gac

    adapter = _make_adapter(max_attempts=2, base_wait=0.0001, max_wait=0.001)
    # The Retry-After (5s) is far larger than our default backoff
    # (~0.0001 - 0.001s). The wait function MUST honor it.
    err = _http_error(429, retry_after="5")
    ok_result = "served-after-rate-limit-pause"
    sequence: list[Any] = [err, ok_result]

    # Patch time.sleep so the test doesn't actually wait 5 seconds —
    # but capture what was requested so we can assert the floor.
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "tenacity.nap.time.sleep",
        lambda t: sleep_calls.append(t),
    )

    def fn():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    result = adapter.execute_with_retry(fn, idempotent=True)
    assert result == ok_result
    assert sleep_calls, "no sleep was issued between attempts"
    # The single sleep call should be >= 5.0 (the Retry-After value)
    assert sleep_calls[0] >= 5.0, (
        f"wait {sleep_calls[0]}s violated server's Retry-After=5s hint"
    )
    # Suppress unused-import warning from monkeypatch-only use.
    _ = gac


def test_no_retry_after_falls_back_to_jittered_backoff(monkeypatch):
    """When the server omits Retry-After, we use exponential backoff +
    jitter. Verify the wait is bounded by max_wait_seconds (jitter
    can push it above base, but not above the cap)."""
    adapter = _make_adapter(max_attempts=2, base_wait=0.01, max_wait=0.05)
    err = _http_error(503)  # no retry-after header
    sequence: list[Any] = [err, "ok"]

    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "tenacity.nap.time.sleep",
        lambda t: sleep_calls.append(t),
    )

    def fn():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    adapter.execute_with_retry(fn, idempotent=True)
    assert sleep_calls
    # The fallback path: wait is in [0, max_wait] thanks to jitter.
    assert 0 <= sleep_calls[0] <= 0.05 + 1e-6


# ---------------------------------------------------------------------
# Module-level facade — execute_with_retry uses the active client
# ---------------------------------------------------------------------


def test_facade_routes_through_active_client():
    """The ``execute_with_retry`` facade delegates to the currently
    active client — so call sites don't have to know which adapter
    they're talking to. Production wires the retry adapter; tests
    that need different behavior swap via ``with_google_api_client``."""
    err = _http_error(503)
    ok = "facade-served"
    sequence: list[Any] = [err, ok]

    def fn():
        nxt = sequence.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    inner = InMemoryGoogleAPIClient({("drive", "v3"): MagicMock()})
    retrying = RetryingGoogleApiClientAdapter(
        inner, max_attempts=3, base_wait_seconds=0.0001, max_wait_seconds=0.001,
    )
    with with_google_api_client(retrying):
        result = execute_with_retry(fn, idempotent=True, op_name="test.facade")

    assert result == ok


def test_facade_falls_back_to_single_invocation_for_non_retrying_client():
    """If a test explicitly swapped in a bare ``InMemoryGoogleAPIClient``
    (no retry shell), the facade still works — it just doesn't retry.
    Tests that opt out of retry get exactly what they opted into."""
    bare_client = InMemoryGoogleAPIClient({("drive", "v3"): MagicMock()})
    calls = 0

    def fn():
        nonlocal calls
        calls += 1
        return "single-shot"

    with with_google_api_client(bare_client):
        result = execute_with_retry(fn, idempotent=True)

    assert result == "single-shot"
    assert calls == 1
