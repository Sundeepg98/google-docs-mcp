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
- ``CachingGoogleApiClientAdapter`` — composing adapter; bounded LRU of
  built Resources keyed ``(service, version, credential identity)``
  (BUG 1b / 2026-07-10 — kills the per-call discovery parse that OOMed
  the 512MB machine; thread-safety rationale on the class).
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

import errno
import hashlib
import logging
import math
import os
import random
import socket
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator, Protocol, TypeVar, runtime_checkable

from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError, UnknownApiNameOrVersion
from googleapiclient.http import HttpRequest
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
# Socket / transport deadline (ROADMAP Hardening-P1)
# ---------------------------------------------------------------------
#
# Without this, ``googleapiclient.discovery.build(credentials=...)``
# constructs its AuthorizedHttp around a bare ``httplib2.Http()`` whose
# socket has NO timeout — so a stalled TCP connection (dropped packets,
# a half-open Google LB socket, a network partition) hangs the
# ``.execute()`` call FOREVER and never raises. That silently defeats
# the retry layer below: tenacity can only retry an attempt that
# *finishes* (by raising), and a hung socket never finishes.
#
# Fix: attach a connect+read deadline to the underlying transport so a
# stall raises ``socket.timeout`` (== ``TimeoutError``, an OSError) fast.
# The retry predicate then treats that as transient, so the EXISTING
# backoff actually kicks in on a hang instead of the call wedging.
#
# 30s is comfortably above Google's p99 latency for the calls this
# server makes (single Docs/Drive/Sheets/Slides operations) while still
# bounding a true stall. Override via env for slow links / debugging.
_DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_HTTP_TIMEOUT_ENV = "GOOGLE_API_HTTP_TIMEOUT_SECONDS"


def _resolve_http_timeout_seconds() -> float:
    """Return the socket timeout (seconds) for the Google API transport.

    Reads ``GOOGLE_API_HTTP_TIMEOUT_SECONDS`` if set to a positive
    number; otherwise falls back to ``_DEFAULT_HTTP_TIMEOUT_SECONDS``.
    A malformed or non-positive value is ignored (with a warning) in
    favor of the default — a bad env var must never silently disable
    the deadline (that would re-introduce the hang this fixes).
    """
    raw = os.environ.get(_HTTP_TIMEOUT_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_HTTP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _log.warning(
            "ignoring non-numeric %s=%r; using default %.0fs",
            _HTTP_TIMEOUT_ENV,
            raw,
            _DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_HTTP_TIMEOUT_SECONDS
    # Require a FINITE positive value. ``float("nan")`` parses fine but
    # fails ``nan > 0`` *and* ``nan <= 0`` (both False) — a nan timeout
    # would silently disable the socket deadline, re-introducing the very
    # hang this guards against. ``inf`` likewise means "no deadline".
    if not math.isfinite(value) or value <= 0:
        _log.warning(
            "ignoring non-positive/non-finite %s=%r; using default %.0fs",
            _HTTP_TIMEOUT_ENV,
            raw,
            _DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_HTTP_TIMEOUT_SECONDS
    return value


def _is_retryable_transport_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a transient network/transport failure.

    Covers the socket-deadline hang this module guards against plus the
    adjacent transient connection failures that are safe to retry on an
    **idempotent** call:

    - ``socket.timeout`` / ``TimeoutError`` — our connect/read deadline
      fired (or the OS connect timer did).
    - ``ConnectionError`` (``ConnectionReset/Aborted/Refused``) — the
      socket dropped mid-flight.
    - ``OSError`` with errno ``ETIMEDOUT`` / ``ECONNRESET`` /
      ``ECONNABORTED`` — the same conditions surfaced as a raw OSError
      by httplib2's socket layer.

    Deliberately NARROW: a generic ``OSError`` with some other errno
    (e.g. ``ENOENT`` from a misconfigured cert path, ``EACCES``) is a
    config/programmer bug, NOT a transient blip, and must propagate
    immediately — same philosophy as the 4xx-never-retry rule for
    ``HttpError``. ``ServerNotFoundError`` (DNS) is intentionally NOT
    retried: a name that doesn't resolve won't resolve on attempt 2.
    """
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError) and exc.errno in _RETRYABLE_ERRNOS:
        return True
    return False


# errno values that map to a transient transport blip (see
# _is_retryable_transport_error). socket.timeout already covers the
# deadline case; these catch the raw-OSError form some platforms raise.
_RETRYABLE_ERRNOS = frozenset(
    getattr(errno, _name)
    for _name in ("ETIMEDOUT", "ECONNRESET", "ECONNABORTED")
    if hasattr(errno, _name)
)


def is_retryable_transport_error(exc: BaseException) -> bool:
    """Public predicate: True iff ``exc`` is a transient transport failure.

    Thin public wrapper over ``_is_retryable_transport_error`` so the
    tool-boundary envelope (``decorators.workspace_tool``) can classify the
    exact SAME transient set the retry chokepoint treats as retryable
    (socket timeout, connection reset/refused, retryable errno) without
    duplicating the membership rule. Keeping "transient" defined in ONE
    place is what lets the boundary map only the errors the chokepoint
    already considers safe to retry, and re-raise everything else so a real
    bug is never mislabeled as a transient blip. This adds NO behavior to
    the retry machinery, which keeps using the private predicate directly.
    """
    return _is_retryable_transport_error(exc)


def _build_authorized_http(credentials: Credentials) -> object:
    """Build a credentialed HTTP transport carrying a socket deadline.

    Returns a ``google_auth_httplib2.AuthorizedHttp`` wrapping an
    ``httplib2.Http(timeout=N)`` — i.e. exactly the transport
    ``googleapiclient.discovery.build(credentials=...)`` builds for
    itself, but with a connect+read timeout on the socket. ``build()``
    accepts this via its ``http=`` parameter, and the deadline then
    applies to every request the resulting Resource issues (and to the
    discovery-document fetch ``build`` itself performs).

    The AuthorizedHttp is additionally wrapped in ``_GetRetryHttp``
    (next-wave polish, 2026-07-10): ONE bounded transport-level retry
    for GET requests answered with 429/5xx, so read paths that never
    adopted ``execute_with_retry`` still absorb a single Google blip.
    Non-GET methods pass through untouched.

    Imports are local so module import stays light and the dependency
    surface is explicit at the one place it's used. Both libraries are
    hard transitive deps of ``google-api-python-client`` (already
    installed), so this adds no new top-level requirement.
    """
    import google_auth_httplib2
    import httplib2

    timeout = _resolve_http_timeout_seconds()
    base_http = httplib2.Http(timeout=timeout)
    return _GetRetryHttp(
        google_auth_httplib2.AuthorizedHttp(credentials, http=base_http)
    )


class _GetRetryHttp:
    """Transport wrapper: ONE bounded retry for GET requests on 429/5xx.

    Sits around the ``AuthorizedHttp`` every Resource built by this
    module uses, so EVERY call site gets the floor policy without
    per-site adoption:

    - ``GET`` answered with a status in ``_RETRYABLE_STATUS`` → sleep
      (exponential-style base with Google's ``Retry-After`` as the
      floor, capped) and re-issue the request ONCE. GETs are
      idempotent by HTTP semantics, so the blind transport-level
      retry is safe.
    - Any other method → single passthrough, byte-identical behavior.
      Mutating calls keep their safety at the ``execute_with_retry``
      layer, where the caller declares idempotence.

    Interplay with ``RetryingGoogleApiClientAdapter`` (the tenacity
    layer above): call sites that already wrap reads in
    ``execute_with_retry(idempotent=True)`` may observe up to
    ``tenacity_attempts * 2`` wire requests in the worst case — still
    strictly bounded, and only on requests Google is actively failing.
    The win is the long tail of read paths that never adopted the
    explicit wrapper (docs fetches inside creation flows, discovery
    fetches, probes): they now absorb a single transient blip instead
    of surfacing it.

    Attribute access other than ``request`` is delegated verbatim to
    the wrapped http object (googleapiclient introspects attributes
    like ``connections`` / ``credentials`` on the transport it's
    handed).
    """

    #: One retry — the "ONE bounded retry" contract. Not configurable
    #: on purpose; anything smarter belongs to the tenacity layer.
    _MAX_GET_RETRIES = 1
    #: Base sleep before the single retry; Retry-After can raise it.
    _BASE_SLEEP_SECONDS = 1.0
    #: Never honor a Retry-After above this (a health probe shouldn't
    #: wedge a tool call for minutes because Google said "3600").
    _MAX_SLEEP_SECONDS = 8.0

    def __init__(self, http: object) -> None:
        self._http = http

    @staticmethod
    def _retry_after_from_headers(resp: object) -> float | None:
        """Delta-seconds ``Retry-After`` from an httplib2 Response.

        httplib2 lowercases header keys and the Response is dict-like.
        HTTP-date form is skipped (rare from Google; same trade-off as
        ``_retry_after_seconds`` above).
        """
        raw = resp.get("retry-after") if hasattr(resp, "get") else None
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _sleep_before_retry(self, resp: object) -> None:
        retry_after = self._retry_after_from_headers(resp)
        wait = max(self._BASE_SLEEP_SECONDS, retry_after or 0.0)
        wait = min(wait, self._MAX_SLEEP_SECONDS)
        # Full jitter fraction so simultaneous callers don't re-sync.
        wait += random.uniform(0.0, 0.25)
        time.sleep(wait)

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: object = None,
        headers: object = None,
        **kwargs: object,
    ):
        resp, content = self._http.request(  # type: ignore[attr-defined]
            uri, method, body=body, headers=headers, **kwargs
        )
        status = getattr(resp, "status", None)
        if method.upper() != "GET" or status not in _RETRYABLE_STATUS:
            return resp, content
        _log.info(
            "transient_google_transport_get_retry status=%s uri_host=%s",
            status,
            uri.split("/", 3)[2] if "//" in uri else "?",
        )
        self._sleep_before_retry(resp)
        return self._http.request(  # type: ignore[attr-defined]
            uri, method, body=body, headers=headers, **kwargs
        )

    def __getattr__(self, name: str):
        return getattr(self._http, name)


def _make_request_builder(credentials: Credentials) -> type[HttpRequest]:
    """Return a ``requestBuilder`` that binds each request to a FRESH transport.

    This is the thread-safety keystone that makes a built ``Resource``
    safe to SHARE across threads (and therefore safe to cache — see
    ``CachingGoogleApiClientAdapter``):

    - ``httplib2.Http`` is NOT thread-safe. googleapiclient's own
      thread-safety guide says each thread must use its own instance,
      and its prescribed pattern is exactly this: a custom
      ``requestBuilder`` that ignores the Resource's shared ``_http``
      and constructs every ``HttpRequest`` around a fresh
      ``AuthorizedHttp(credentials, http=httplib2.Http())``.
    - The ``credentials`` object IS shared across those fresh
      transports — that sharing is part of the documented pattern.
      Two threads may race to refresh an expired token; both refreshes
      succeed independently and last-write-wins on the in-memory
      token. Benign: Google honors concurrent refresh grants.
    - Trade-off: a fresh ``Http`` per request forfeits intra-call
      connection reuse (one TLS handshake per API call). Accepted
      deliberately — it is the price of making Resources cacheable,
      and the cache removes the per-call discovery-document parse
      that was the actual OOM driver (BUG 1, 2026-07-09).

    The fresh transport carries the same socket deadline as the
    baseline one (Hardening-P1) via ``_build_authorized_http``.
    """

    def build_request(_http: object, *args: object, **kwargs: object) -> HttpRequest:
        # ``_http`` is the Resource's shared transport — deliberately
        # ignored (sharing it across threads is the httplib2 hazard).
        return HttpRequest(_build_authorized_http(credentials), *args, **kwargs)  # type: ignore[arg-type]

    # The SDK stub types ``requestBuilder`` as ``type[HttpRequest]``,
    # but the runtime only ever CALLS it, and googleapiclient's own
    # thread-safety guide passes a plain function exactly like this
    # one. The cast bridges the over-narrow stub without weakening the
    # runtime contract.
    from typing import cast
    return cast("type[HttpRequest]", build_request)


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
    """Production adapter: wraps ``googleapiclient.discovery.build``.

    Two deliberate hardenings on top of a plain ``build()`` call:

    - ROADMAP Hardening-P1: the underlying HTTP transport carries a
      connect+read **socket timeout** so a stalled connection fails
      fast (and retryably) instead of hanging ``.execute()`` forever.
    - BUG 1a (2026-07-10): discovery is pinned to the library's
      BUNDLED static documents (``static_discovery=True``) and every
      request gets its own transport via ``_make_request_builder`` so
      the returned Resource is safe to share across threads (which is
      what lets ``CachingGoogleApiClientAdapter`` cache it). At the
      pinned google-api-python-client version, static discovery is
      already the effective default when no ``discoveryServiceUrl``
      is supplied — passing it explicitly protects against the
      library default drifting and documents the dependency on the
      bundled documents. ``cache_discovery=False`` skips the dead
      oauth2client-era file cache probe (and its per-build
      "file_cache is only supported with oauth2client<4.0.0" log
      spam).

    Stateless — safe to share a single instance process-wide
    (the module-level ``_active_client`` composes one).
    """

    def get_service(
        self,
        service: str,
        version: str,
        *,
        credentials: Credentials,
    ) -> Resource:
        # Build the SAME AuthorizedHttp that ``build(credentials=...)``
        # would construct internally, except the wrapped ``httplib2.Http``
        # carries a socket deadline. We pass ``http=`` (NOT ``credentials=``
        # — passing both raises) so the deadline reaches every request,
        # including a dynamic discovery-document fetch on the fallback
        # path below.
        #
        # IMPORTANT (auth-path isolation, per the timeout-PR gate): this
        # does NOT touch credential resolution. ``AuthorizedHttp`` is the
        # exact wrapper googleapiclient uses under the hood; we hand it
        # the already-resolved ``credentials`` object unchanged and only
        # set ``timeout`` on the transport socket. Token refresh, scopes,
        # and the per-user credential plumbing are entirely unaffected.
        authorized_http = _build_authorized_http(credentials)
        request_builder = _make_request_builder(credentials)
        try:
            return build(
                service,
                version,
                http=authorized_http,
                requestBuilder=request_builder,
                static_discovery=True,
                cache_discovery=False,
            )
        except UnknownApiNameOrVersion:
            # Safety valve: a (service, version) with no bundled static
            # document. Every service this codebase builds ships in the
            # bundle (pinned by test_static_discovery_covers_every_service),
            # so this path only fires for a FUTURE service added without
            # updating the bundle expectations — degrade to a network
            # discovery fetch instead of failing the tool call.
            _log.warning(
                "no bundled static discovery document for %s %s; "
                "falling back to dynamic (network) discovery",
                service,
                version,
            )
            return build(
                service,
                version,
                http=authorized_http,
                requestBuilder=request_builder,
                static_discovery=False,
                cache_discovery=False,
            )


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
    """Predicate: True iff ``exc`` is a Google ``HttpError`` whose status
    code is in ``_RETRYABLE_STATUS`` (429 / 500 / 502 / 503 / 504).

    4xx validation/auth errors and any non-``HttpError`` propagate
    immediately — caller bugs MUST NOT be retried. (Transient transport
    failures are handled separately by ``_is_retryable_transport_error``;
    the combined ``_is_retryable`` is what the retry machinery uses.)
    """
    if not isinstance(exc, HttpError):
        return False
    # status_code is set on every HttpError; fall back to resp.status
    # for the rare case where the SDK only populated the response.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    return status in _RETRYABLE_STATUS


def _is_retryable(exc: BaseException) -> bool:
    """Combined retry predicate used by the retry machinery.

    Retries BOTH transient Google ``HttpError`` responses (429/5xx) AND
    transient transport failures (the socket-deadline hang this module
    guards against, plus connection drops) — see
    ``_is_retryable_http_error`` and ``_is_retryable_transport_error``.
    This is the single predicate tenacity is wired to, so a timed-out
    socket now triggers the existing backoff instead of wedging the call.
    """
    return _is_retryable_http_error(exc) or _is_retryable_transport_error(exc)


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

        If ``idempotent=True``, transient failures trigger exponential
        backoff + jitter retry up to ``self._max_attempts`` total
        attempts: both ``HttpError`` responses (429 / 5xx) and transient
        transport failures (socket timeout from the deadline on the
        Google API transport, connection resets). ``Retry-After`` from
        Google is honored as the floor for the HTTP case.
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
            retry=retry_if_exception(_is_retryable),
            reraise=True,  # surface the real error, not RetryError
        )

        attempt_num = 0
        for attempt in retryer:
            attempt_num += 1
            with attempt:
                try:
                    return fn()
                except Exception as e:  # noqa: BLE001 — log+reraise; tenacity's retry= decides
                    # Log-only; we re-raise unconditionally and let the
                    # Retrying ``retry=`` predicate (_is_retryable) decide
                    # whether another attempt happens. Two transient
                    # classes get an info line so a retry is visible in
                    # logs; everything else falls straight through.
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
                    elif _is_retryable_transport_error(e):
                        _log.info(
                            "transient_google_api_transport_error op=%s "
                            "attempt=%d/%d error=%s",
                            op_name,
                            attempt_num,
                            self._max_attempts,
                            type(e).__name__,
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
# Adapter 4: CachingGoogleApiClientAdapter — bounded per-identity cache
# ---------------------------------------------------------------------
#
# BUG 1b (2026-07-09 OOM): every tool call re-ran ``build()`` — a
# multi-hundred-KB discovery-document read + json parse + Resource
# construction PER CALL. A single /api/convert issues dozens of
# ``get_service`` calls, and the repeated parse garbage on a 512MB
# machine at ~390MB baseline RSS was the kill driver. Caching the
# built Resource makes ``build()`` a once-per-(service, version, user)
# cost instead of a per-call cost.

# Bound on cached Resources. ~10 (service, version) pairs exist in the
# codebase, so 32 comfortably covers several concurrently-active users
# while capping worst-case memory (a parsed Resource is single-digit
# MB for the heaviest services). LRU eviction handles overflow; an
# evicted Resource is just garbage-collected — in-flight requests hold
# their own per-request transport (see ``_make_request_builder``), so
# eviction can never yank a socket out from under a running call.
_DEFAULT_SERVICE_CACHE_MAX_ENTRIES = 32
_SERVICE_CACHE_MAX_ENTRIES_ENV = "GOOGLE_API_SERVICE_CACHE_MAX_ENTRIES"


def _resolve_service_cache_max_entries() -> int:
    """Return the cache bound. ``<= 0`` disables caching entirely.

    Mirrors ``_resolve_http_timeout_seconds``: a malformed value falls
    back to the default (with a warning) rather than silently changing
    behavior; an explicit ``0`` (or negative) is the operator kill
    switch that turns the caching adapter into a passthrough.
    """
    raw = os.environ.get(_SERVICE_CACHE_MAX_ENTRIES_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_SERVICE_CACHE_MAX_ENTRIES
    try:
        return int(raw)
    except (TypeError, ValueError):
        _log.warning(
            "ignoring non-integer %s=%r; using default %d",
            _SERVICE_CACHE_MAX_ENTRIES_ENV,
            raw,
            _DEFAULT_SERVICE_CACHE_MAX_ENTRIES,
        )
        return _DEFAULT_SERVICE_CACHE_MAX_ENTRIES


def _credential_cache_identity(credentials: Credentials) -> str | None:
    """Derive a stable, per-user cache-key component from ``credentials``.

    Returns ``None`` when no trustworthy identity can be derived — the
    caching adapter then BYPASSES the cache for that call (fail open to
    the pre-cache build-per-call behavior, which is always correct).

    Identity source, in order:

    - ``refresh_token``: stable for the lifetime of a grant, unique per
      user, and REPLACED when the user revokes + re-authorizes — so a
      re-granted user can never hit a Resource bound to their revoked
      credentials.
    - ``token`` (access token): unique per user but rotates ~hourly;
      each rotation is a cache miss + rebuild, and the stale entries
      age out via LRU. Only used when no refresh token is present.

    Cross-tenant safety: two distinct users can never share a key
    because both token kinds are per-user secrets (the mutation guard
    is ``test_distinct_credentials_get_distinct_resources``). The
    sha256 keeps raw token material out of cache keys so a debugger /
    log dump of the cache never exposes a secret. The cached VALUE is
    a Resource; the credentials object it carries is the same one the
    caller passed — nothing additional is retained.
    """
    for attr in ("refresh_token", "token"):
        value = getattr(credentials, attr, None)
        if isinstance(value, str) and value:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return f"{attr}:{digest}"
    return None


class CachingGoogleApiClientAdapter:
    """Composing adapter: bounded LRU of built Resources.

    Key: ``(service, version, credential identity)`` — see
    ``_credential_cache_identity`` for the identity derivation and the
    fail-open rule when no identity exists.

    **Thread-safety reasoning** (the whole point of this design —
    httplib2 is not thread-safe, so a naive cache would share one
    ``Http`` connection object across concurrent tool calls and
    corrupt request state):

    - A cache HIT hands the same Resource to multiple threads. That is
      safe ONLY because the production adapter builds Resources with
      ``_make_request_builder``, which binds every outgoing request to
      its own fresh ``AuthorizedHttp`` + ``httplib2.Http`` (the pattern
      googleapiclient's thread-safety guide prescribes). No request
      ever touches the Resource's shared baseline transport — including
      batch requests, whose ``execute()`` falls back to the FIRST
      request's (fresh) transport, not the Resource's.
    - Cache bookkeeping (lookup, insert, LRU reorder, eviction) runs
      under ``_lock``. The slow part — ``build()`` on a miss — runs
      OUTSIDE the lock so one user's cold build never blocks another
      user's cache hit.
    - Two threads can therefore race to build the same key. The first
      insert wins; the loser discards its build and returns the
      winner's Resource (both are valid; the duplicate build is the
      benign cost of not holding a lock across ``build()``).

    Only wrap adapters that return thread-shareable Resources (the
    production adapter does; ``InMemoryGoogleAPIClient`` stubs are
    test-only and tests own their own isolation).
    """

    def __init__(
        self,
        inner: GoogleAPIClient,
        *,
        max_entries: int | None = None,
    ) -> None:
        self._inner = inner
        self._max_entries = (
            _resolve_service_cache_max_entries()
            if max_entries is None
            else max_entries
        )
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, str], Resource] = OrderedDict()

    def get_service(
        self,
        service: str,
        version: str,
        *,
        credentials: Credentials,
    ) -> Resource:
        if self._max_entries <= 0:
            return self._inner.get_service(service, version, credentials=credentials)
        identity = _credential_cache_identity(credentials)
        if identity is None:
            # No stable identity — never guess. Build per call, exactly
            # like the pre-cache behavior.
            return self._inner.get_service(service, version, credentials=credentials)

        key = (service, version, identity)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        resource = self._inner.get_service(service, version, credentials=credentials)

        with self._lock:
            existing = self._cache.get(key)
            if existing is not None:
                # Lost a build race — return the winner so every caller
                # holding this key shares one Resource.
                self._cache.move_to_end(key)
                return existing
            self._cache[key] = resource
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        return resource


# ---------------------------------------------------------------------
# Module-level default + injection ergonomics
# ---------------------------------------------------------------------


# Process-wide active client. Production wires this to a
# ``RetryingGoogleApiClientAdapter`` composing the caching + production
# adapters at import time (PR-Δ3 / 2026-05-27; caching added for BUG 1b
# 2026-07-10). ``get_service`` flows retry -> cache -> build, so the
# existing call sites that just do ``get_service(...)`` are untouched
# and now hit the bounded Resource cache. The retry policy stays
# opt-in per-call-site via ``execute_with_retry`` (or the convenience
# facade defined below).
# Tests swap via ``set_google_api_client()`` or ``with_google_api_client()``
# — the InMemoryGoogleAPIClient adapter does NOT layer retry or caching
# (tests that need either wire the composing adapter explicitly around
# an InMemory inner).
_active_client: GoogleAPIClient = RetryingGoogleApiClientAdapter(
    CachingGoogleApiClientAdapter(GoogleApiClientAdapter())
)
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
