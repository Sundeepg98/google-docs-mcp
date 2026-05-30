"""Hex-style Port + Adapters for Google API client construction (v2.1.2 / M2).

The pre-v2.1.2 ``google_clients.py`` was a pure passthrough wrapper
around ``googleapiclient.discovery.build``. v2.1.2 promotes it into a
proper Port + Adapters shape, matching M1a's ``key_provider.py`` design:

- ``GoogleAPIClient`` Protocol — the port (interface every adapter satisfies).
- ``GoogleApiClientAdapter`` — production adapter; wraps the vendor SDK.
- ``InMemoryGoogleAPIClient`` — test-only adapter; returns pre-registered
  stubs from a ``{(service, version): Resource}`` registry.
- ``RetryingGoogleApiClientAdapter`` — composing adapter; delegates
  ``get_service`` to an inner adapter and additionally exposes
  ``execute_with_retry(callable, *, idempotent: bool)`` for call sites
  that need exponential-backoff + jitter on Google's routine 429/5xx
  responses (PR-Δ3 / 2026-05-27, closes the Hex specialist finding that
  zero retry code existed anywhere in the codebase).
- Facade + injection ergonomics matching ``StorageBackend`` and
  ``KeyProvider``: ``set_google_api_client(client)`` + ``with_google_api_client(client)``.

**Design notes (M2 vs M1a).**

Unlike ``KeyProvider``'s 3-mechanism chain (env override → HKDF → shim),
``GoogleAPIClient`` has a single backend per process. There is no
production scenario where two distinct adapters should compete to serve
a request — production is always ``googleapiclient.discovery.build``.
Therefore **no LayeredKeyProvider equivalent here**: straight Protocol +
adapter is the right shape per Hex specialist Round 2 review.

The Protocol is also stateless: the contract is a single passthrough
method, ``get_service(service, version, *, credentials) -> Resource``.
Test architect's M1a critique about leaky multi-step protocols doesn't
apply — the contract-test is lightweight (Protocol satisfaction +
adapter parity on a single call).

**Backward compatibility.** ``google_clients.get_service`` is preserved
as a delegating facade; every existing import path continues to work.

**Why not skip the port and add cache/retry directly to google_clients?**
The 12-month roadmap includes a potential ``aiogoogle`` async swap.
That swap is the actual swap candidate the port enables — same
``GoogleAPIClient`` interface, ``AiogoogleClientAdapter`` returns
async-shaped Resources. Without the port, the swap is a 14-call-site
sweep; with it, a single ``set_google_api_client(adapter)``.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator, Protocol, TypeVar, runtime_checkable

from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

if TYPE_CHECKING:
    # Base type — googleapiclient.build() accepts any Credentials subclass
    # at runtime (oauth2 user creds, service account, external account,
    # impersonated creds, etc.). Annotating the wrapper with the base
    # type lets every flow share this single chokepoint instead of
    # forcing per-flow wrappers or downstream casts.
    from google.auth.credentials import Credentials


_log = logging.getLogger("appscriptly.retry")
_T = TypeVar("_T")

# HTTP status codes that the Google APIs document as transient:
#   429 Too Many Requests
#   500 Internal Server Error    (Google's internal hiccup)
#   502 Bad Gateway              (LB blip)
#   503 Service Unavailable      (overload / planned)
#   504 Gateway Timeout          (slow backend)
# All other 4xx are caller bugs (auth, validation) — NEVER retry.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# ---------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------


@runtime_checkable
class GoogleAPIClient(Protocol):
    """Port for Google API client construction.

    Adapters return a ``googleapiclient.discovery.Resource`` (or a
    duck-typed equivalent for tests). The single contract is:
    same ``(service, version, credentials)`` triple must yield a
    Resource appropriate for that triple — i.e. a Docs v1 Resource
    for ``get_service("docs", "v1", credentials=alice)``.

    The ``credentials`` parameter is keyword-only at the Protocol
    level. Pre-v2.1.2 ``google_clients.get_service`` enforced this
    via signature inspection; keyword-only here makes the contract
    explicit (a positional ``credentials`` lets a future cache
    accidentally drop it from the cache key, leaking one user's
    Resource to another — the v2.0.3 PR #47 anti-pattern).
    """

    def get_service(
        self,
        service: str,
        version: str,
        *,
        credentials: Credentials,
    ) -> Resource: ...


# ---------------------------------------------------------------------
# Adapter 1: Production — wraps googleapiclient.discovery.build
# ---------------------------------------------------------------------


class GoogleApiClientAdapter:
    """Production adapter: pure passthrough to ``googleapiclient.discovery.build``.

    Same behavior as pre-v2.1.2 ``google_clients.get_service``. No
    caching, no retry — those are deferred behind this port for a
    future ``CachingGoogleApiClientAdapter`` to layer in without
    touching the 14 call sites.

    Stateless — safe to share a single instance process-wide
    (the module-level ``_active_client`` is one).
    """

    def get_service(
        self,
        service: str,
        version: str,
        *,
        credentials: Credentials,
    ) -> Resource:
        return build(service, version, credentials=credentials)


# ---------------------------------------------------------------------
# Adapter 2: Test-only — registry of stub Resources
# ---------------------------------------------------------------------


class InMemoryGoogleAPIClient:
    """Test-only adapter: returns pre-registered Resource stubs.

    Replaces the brittle ``with patch("...build") as mk_build: ...``
    pattern in tests. Usage::

        from unittest.mock import MagicMock
        from appscriptly.google_api_client import (
            InMemoryGoogleAPIClient, with_google_api_client,
        )

        drive_stub = MagicMock(name="drive-v3-stub")
        drive_stub.files().list().execute.return_value = {"files": []}

        with with_google_api_client(InMemoryGoogleAPIClient({
            ("drive", "v3"): drive_stub,
        })):
            ...test body...

    Unknown ``(service, version)`` tuples raise ``KeyError`` with the
    full registry so a missing stub is obvious. This is stricter than
    pre-v2.1.2's ``MagicMock`` autospec behavior — but the strictness
    is the win: a test that didn't register a stub it ends up needing
    fails loudly instead of silently returning a no-op MagicMock.

    **Credentials are NOT part of the key.** Tests that need to verify
    "different users get different Resources" should register distinct
    stubs per credential and use a custom adapter — that's an
    integration concern, not the unit-test happy path.
    """

    def __init__(
        self,
        registry: dict[tuple[str, str], object] | None = None,
    ) -> None:
        self._registry: dict[tuple[str, str], object] = dict(registry or {})

    def get_service(
        self,
        service: str,
        version: str,
        *,
        credentials: Credentials,  # noqa: ARG002 — Protocol-required
    ) -> Resource:
        key = (service, version)
        if key not in self._registry:
            raise KeyError(
                f"No stub registered for {key}. Registered: "
                f"{sorted(self._registry.keys())}. Use "
                f"InMemoryGoogleAPIClient.register({service!r}, {version!r}, stub) "
                f"or pass the stub in the constructor's registry dict."
            )
        return self._registry[key]  # type: ignore[return-value]

    def register(self, service: str, version: str, stub: object) -> None:
        """Add a stub Resource for a ``(service, version)`` tuple.

        Useful when a single test needs to register multiple stubs
        incrementally, or when a fixture builds the base registry and
        per-test code adds the test-specific stub.
        """
        self._registry[(service, version)] = stub


# ---------------------------------------------------------------------
# Adapter 3: RetryingGoogleApiClientAdapter — composing wrapper
# ---------------------------------------------------------------------


def _is_retryable_http_error(exc: BaseException) -> bool:
    """Predicate for tenacity: True iff ``exc`` is a Google ``HttpError``
    whose status code is in ``_RETRYABLE_STATUS``.

    All other exceptions (network errors, programmer bugs, 4xx
    validation errors) propagate immediately — caller bugs MUST NOT
    be retried.
    """
    if not isinstance(exc, HttpError):
        return False
    # status_code is set on every HttpError; fall back to resp.status
    # for the rare case where the SDK only populated the response.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    return status in _RETRYABLE_STATUS


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract ``Retry-After`` header from an ``HttpError`` if present.

    Google often sets this on 429s; honor it as the floor for the
    next attempt instead of stomping on it with our own backoff.
    Returns float seconds (handles HTTP-date or delta-seconds form
    gracefully — falls back to None on parse failure).
    """
    if not isinstance(exc, HttpError):
        return None
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    raw = resp.get("retry-after") if hasattr(resp, "get") else None
    if raw is None:
        return None
    try:
        # Delta-seconds form (the common case).
        return float(raw)
    except (TypeError, ValueError):
        # HTTP-date form is rare from Google in practice; skip rather
        # than pull in email.utils.parsedate_to_datetime for one path.
        return None


class RetryingGoogleApiClientAdapter:
    """Composing adapter: delegates ``get_service`` and adds retry policy.

    Constructed with an inner ``GoogleAPIClient`` (in production: a
    ``GoogleApiClientAdapter``; in tests: an ``InMemoryGoogleAPIClient``)
    plus a retry config. Exposes:

    - ``get_service(service, version, *, credentials) -> Resource`` —
      pure delegation to the inner adapter (so the existing call sites
      need no change).
    - ``execute_with_retry(fn, *, idempotent, op_name)`` — explicit retry
      wrapper for ``.execute()`` callables; the only place call sites
      have to opt in to retry. Idempotence is per-call-site because
      the **caller** (the @workspace_tool decorator + per-tool
      annotation) is the only authority on whether retrying is safe.

    **Why a separate ``execute_with_retry`` instead of wrapping every
    HttpRequest the inner Resource yields?** Patching the
    ``googleapiclient.http.HttpRequest`` chain returned by ``.files()``,
    ``.documents()``, etc. would require an opaque proxy of every
    Resource method (~hundreds of dispatched calls), couple us to
    googleapiclient internals that the SDK reserves the right to
    rearrange, and surrender control over idempotence (the proxy
    can't see the calling tool's annotation). An explicit wrapper at
    each ``.execute()`` site is shorter, honest about scope, and
    portable to a future ``aiogoogle`` swap.

    **Retry policy (defaults match the PR-Δ3 spec).**

    - 3 attempts maximum (first attempt + 2 retries).
    - Exponential backoff: 1s, 2s, 4s, with full jitter.
    - Honors ``Retry-After`` (delta-seconds form) when Google sets it
      on 429s — the next wait is ``max(backoff, retry_after_seconds)``.
    - Retries ONLY ``HttpError`` with status ∈ {429, 500, 502, 503, 504}.
      Everything else (auth errors, validation errors, network errors)
      propagates immediately — caller bugs MUST NOT be retried.
    - Retries ONLY when ``idempotent=True``. Mutating non-idempotent
      operations (``gdocs_make_tabbed_doc``, ``gdocs_install_automation``,
      etc.) bypass retry entirely — re-executing them risks duplicate
      docs / duplicate deployments. Callers read this from the
      ``@workspace_tool(idempotent=...)`` annotation surface (also
      exposed via the MCP ``ToolAnnotations.idempotentHint``).

    Stateless — safe to share a single instance process-wide.
    """

    def __init__(
        self,
        inner: GoogleAPIClient,
        *,
        max_attempts: int = 3,
        base_wait_seconds: float = 1.0,
        max_wait_seconds: float = 8.0,
    ) -> None:
        self._inner = inner
        self._max_attempts = max_attempts
        self._base_wait = base_wait_seconds
        self._max_wait = max_wait_seconds

    def get_service(
        self,
        service: str,
        version: str,
        *,
        credentials: Credentials,
    ) -> Resource:
        # Pure delegation. Resource construction itself is local —
        # nothing to retry.
        return self._inner.get_service(service, version, credentials=credentials)

    def execute_with_retry(
        self,
        fn: Callable[[], _T],
        *,
        idempotent: bool,
        op_name: str = "google_api_call",
    ) -> _T:
        """Execute ``fn()`` with retry policy if ``idempotent`` is True.

        If ``idempotent=False``, ``fn()`` is invoked exactly once and
        any exception propagates. This is the safety floor — a
        partially-completed mutating call cannot be replayed without
        risking duplicates.

        If ``idempotent=True``, transient ``HttpError`` responses
        (429 / 5xx) trigger exponential backoff + jitter retry up to
        ``self._max_attempts`` total attempts. ``Retry-After`` from
        Google is honored as the floor.
        """
        if not idempotent:
            return fn()

        # Closure-captured "last exception" so we can extract
        # Retry-After per-attempt without tenacity owning the state.
        retryer = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=_RetryAfterAwareWait(
                base=self._base_wait,
                cap=self._max_wait,
            ),
            retry=retry_if_exception(_is_retryable_http_error),
            reraise=True,  # surface the real HttpError, not RetryError
        )

        attempt_num = 0
        for attempt in retryer:
            attempt_num += 1
            with attempt:
                try:
                    return fn()
                except HttpError as e:
                    if _is_retryable_http_error(e):
                        _log.info(
                            "transient_google_api_error op=%s attempt=%d/%d "
                            "status=%s retry_after=%s",
                            op_name,
                            attempt_num,
                            self._max_attempts,
                            getattr(e, "status_code", "?"),
                            _retry_after_seconds(e),
                        )
                    raise
        # Should be unreachable — Retrying with reraise=True always
        # either returns from inside the loop or raises.
        raise RuntimeError("RetryingGoogleApiClientAdapter: unreachable")


class _RetryAfterAwareWait:
    """tenacity ``wait`` strategy that honors HTTP ``Retry-After``.

    Falls back to exponential backoff + full jitter when no
    ``Retry-After`` is present. Implemented as a callable class
    instead of using tenacity's ``wait_exponential_jitter`` directly
    because tenacity passes the most-recent attempt to the wait
    callable, letting us peek at the raised exception.
    """

    def __init__(self, *, base: float, cap: float) -> None:
        self._fallback = wait_exponential_jitter(initial=base, max=cap)

    def __call__(self, retry_state) -> float:  # type: ignore[no-untyped-def]
        fallback = self._fallback(retry_state)
        outcome = retry_state.outcome
        if outcome is None:
            return fallback
        exc = outcome.exception()
        retry_after = _retry_after_seconds(exc) if exc is not None else None
        if retry_after is None:
            return fallback
        # Take the LARGER of Google's hint and our jittered backoff.
        # Floors at Retry-After (don't violate the server's request)
        # while still adding our jitter when Retry-After is small.
        return max(float(retry_after), fallback)


# Suppress unused-import warning — RetryError is re-exported below as
# a convenience for callers who want to catch it explicitly.
_ = RetryError


# ---------------------------------------------------------------------
# Module-level default + injection ergonomics
# ---------------------------------------------------------------------


# Process-wide active client. Production wires this to a
# ``RetryingGoogleApiClientAdapter`` composing the production
# ``GoogleApiClientAdapter`` at import time (PR-Δ3 / 2026-05-27).
# The retry policy is opt-in per-call-site via ``execute_with_retry`` —
# ``get_service`` itself is unchanged pure delegation, so the existing
# 14 call sites that just do ``get_service(...)`` are untouched. Call
# sites that want retry call ``get_active_retry_client().execute_with_retry(...)``
# (or use the ``execute_with_retry`` convenience facade defined below).
# Tests swap via ``set_google_api_client()`` or ``with_google_api_client()``
# — the InMemoryGoogleAPIClient adapter does NOT layer retry (tests
# that need to verify retry behavior wire the RetryingGoogleApiClientAdapter
# explicitly around an InMemory inner).
_active_client: GoogleAPIClient = RetryingGoogleApiClientAdapter(GoogleApiClientAdapter())
_client_lock = threading.Lock()


def get_active_client() -> GoogleAPIClient:
    """Return the currently active client.

    Always non-None because the module-level default is wired at import.
    Tests that swap via ``with_google_api_client`` restore the default
    on context exit so subsequent tests see the production adapter
    again.
    """
    return _active_client


def set_google_api_client(client: GoogleAPIClient) -> GoogleAPIClient:
    """Replace the active client. Returns the previous (for restore).

    Tests should prefer ``with_google_api_client`` over raw set + manual
    restore; this helper exists for the rare case where the context-
    manager idiom doesn't fit (e.g. session-scoped pytest fixtures with
    cleanup in finalizers).
    """
    global _active_client
    with _client_lock:
        previous = _active_client
        _active_client = client
    return previous


@contextmanager
def with_google_api_client(client: GoogleAPIClient) -> Iterator[GoogleAPIClient]:
    """Temporarily swap the active client within a ``with`` block.

    Example::

        from appscriptly.google_api_client import (
            InMemoryGoogleAPIClient, with_google_api_client,
        )
        from appscriptly import google_clients
        from unittest.mock import MagicMock

        drive_stub = MagicMock()
        with with_google_api_client(InMemoryGoogleAPIClient({
            ("drive", "v3"): drive_stub,
        })):
            # google_clients.get_service delegates to the active client;
            # the next call returns drive_stub, not a real build() call.
            assert google_clients.get_service(
                "drive", "v3", credentials=...,
            ) is drive_stub

    Restores the prior client on exit — including on exceptions — so
    a test failure in the body doesn't leak the injection into
    subsequent tests.
    """
    previous = set_google_api_client(client)
    try:
        yield client
    finally:
        global _active_client
        with _client_lock:
            _active_client = previous


# ---------------------------------------------------------------------
# Facade — delegates to the active client. Used by ``google_clients.get_service``.
# ---------------------------------------------------------------------


def get_service(
    service: str,
    version: str,
    *,
    credentials: Credentials,
) -> Resource:
    """Return a Google API ``Resource`` via the active ``GoogleAPIClient``.

    Pure delegation today — the production default
    (``GoogleApiClientAdapter``) is byte-equivalent to pre-v2.1.2
    ``google_clients.get_service``. Tests that swap the active client
    via ``with_google_api_client`` redirect the call without touching
    the call site.

    **Backward compatibility.** ``google_clients.get_service`` (the
    pre-v2.1.2 entry point) is preserved as a thin delegating wrapper
    around this function — every existing import path continues to
    work.
    """
    return _active_client.get_service(service, version, credentials=credentials)


def execute_with_retry(
    fn: Callable[[], _T],
    *,
    idempotent: bool,
    op_name: str = "google_api_call",
) -> _T:
    """Facade for retry policy — delegates to the active client.

    Convenience wrapper so call sites don't have to type
    ``get_active_client().execute_with_retry(...)``. Works whenever
    the active client is a ``RetryingGoogleApiClientAdapter``
    (production default + recommended for tests that need retry).

    If the active client lacks ``execute_with_retry`` (e.g. a bare
    ``InMemoryGoogleAPIClient`` in a test that doesn't care about
    retry), falls back to a single invocation — same semantics as
    ``idempotent=False`` would produce. Honest about scope: tests
    that explicitly swap in a non-retrying client get non-retrying
    behavior, which is what they asked for.
    """
    client = _active_client
    impl = getattr(client, "execute_with_retry", None)
    if impl is None:
        return fn()
    return impl(fn, idempotent=idempotent, op_name=op_name)
