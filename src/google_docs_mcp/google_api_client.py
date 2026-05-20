"""Hex-style Port + Adapters for Google API client construction (v2.1.2 / M2).

The pre-v2.1.2 ``google_clients.py`` was a pure passthrough wrapper
around ``googleapiclient.discovery.build``. v2.1.2 promotes it into a
proper Port + Adapters shape, matching M1a's ``key_provider.py`` design:

- ``GoogleAPIClient`` Protocol — the port (interface every adapter satisfies).
- ``GoogleApiClientAdapter`` — production adapter; wraps the vendor SDK.
- ``InMemoryGoogleAPIClient`` — test-only adapter; returns pre-registered
  stubs from a ``{(service, version): Resource}`` registry.
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

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Protocol, runtime_checkable

from googleapiclient.discovery import Resource, build

if TYPE_CHECKING:
    # Base type — googleapiclient.build() accepts any Credentials subclass
    # at runtime (oauth2 user creds, service account, external account,
    # impersonated creds, etc.). Annotating the wrapper with the base
    # type lets every flow share this single chokepoint instead of
    # forcing per-flow wrappers or downstream casts.
    from google.auth.credentials import Credentials


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
        from google_docs_mcp.google_api_client import (
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
# Module-level default + injection ergonomics
# ---------------------------------------------------------------------


# Process-wide active client. Production wires this to
# ``GoogleApiClientAdapter`` at import time. Tests swap via
# ``set_google_api_client()`` or ``with_google_api_client()``.
_active_client: GoogleAPIClient = GoogleApiClientAdapter()
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

        from google_docs_mcp.google_api_client import (
            InMemoryGoogleAPIClient, with_google_api_client,
        )
        from google_docs_mcp import google_clients
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
