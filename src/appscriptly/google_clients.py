"""Single chokepoint for googleapiclient.discovery.build() construction.

Owned wrapper around the vendor SDK so high-level code depends on
this module's abstraction, not on googleapiclient directly.

**v2.1.2 M2 refactor.** The mechanism layer has been promoted to
``google_api_client.py`` — Protocol + 2 adapters (production
``GoogleApiClientAdapter`` + test-only ``InMemoryGoogleAPIClient``)
+ injection ergonomics matching ``StorageBackend`` (user_store.py)
and ``KeyProvider`` (key_provider.py). This module remains the public
facade: every pre-v2.1.2 import path continues to work. The refactor
is purely internal — no behavior change for callers.

See ``google_api_client.py`` module docstring for the Hex-style
port-and-adapters rationale and the M1b-skipped / M2-chosen verdict.

**Why this defends against the v2.0.3 anti-pattern (PR #47).** That
bug was an existing wrapper (``user_store.save_credentials_json``)
being bypassed by a direct call to the underlying primitive
(``user_store.save_state``). A purely-additive wrapper module
solves nothing on its own — the lint rule registered in
``pyproject.toml`` (ruff TID251) is what prevents the same
bypass pattern here. Removing the lint rule undoes the defense.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Re-export Resource for callers that import it from here. The actual
# construction lives in ``google_api_client.GoogleApiClientAdapter``
# (which still does ``from googleapiclient.discovery import build``).
# This module is the public facade; the TID251 exemption now applies
# to ``google_api_client.py`` instead.
from googleapiclient.discovery import Resource

from .google_api_client import get_service as _delegate_get_service

if TYPE_CHECKING:
    # Base type — googleapiclient.build() accepts any Credentials subclass
    # at runtime (oauth2 user creds, service account, external account,
    # impersonated creds, etc.). Annotating the wrapper with the base
    # type lets every flow share this single chokepoint instead of
    # forcing per-flow wrappers or downstream casts.
    from google.auth.credentials import Credentials


def get_service(
    service: str,
    version: str,
    *,
    credentials: Credentials,
) -> Resource:
    """Return a Google API ``Resource`` for ``(service, version, credentials)``.

    **v2.1.2**: delegates to ``google_api_client.get_service``, which
    routes to the active ``GoogleAPIClient`` adapter (production
    default = ``GoogleApiClientAdapter``, byte-equivalent to pre-v2.1.2
    behavior). Tests that want to swap the backend use
    ``google_api_client.with_google_api_client(InMemoryGoogleAPIClient(...))``
    instead of ``patch("appscriptly.google_clients.build")``.

    The ``credentials`` parameter is keyword-only on purpose: any
    future caching layer MUST keep credentials in the cache key
    (different users get different Resources), and making this a
    named param keeps that property obvious at every call site.
    The matching regression guard is
    ``test_distinct_credentials_get_distinct_resources`` in
    ``tests/unit/test_google_clients.py`` — if you collapse the
    cache key across credentials, that test fails immediately.
    """
    return _delegate_get_service(service, version, credentials=credentials)


__all__ = ["Resource", "get_service"]
