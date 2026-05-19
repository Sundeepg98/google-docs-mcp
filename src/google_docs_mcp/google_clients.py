"""Single chokepoint for googleapiclient.discovery.build() construction.

Owned wrapper around the vendor SDK so high-level code depends on
this module's abstraction, not on googleapiclient directly.

**Initial implementation: pure passthrough.** No caching, no retry,
no telemetry. The wrapper EXISTS so those concerns can be added
later as single-site changes inside this module instead of
23-site sweeps across the codebase. PR1 (v2.6a) ships the seam;
PR2 migrates the 23 existing call sites; later PRs add caching /
retry / telemetry behind this surface.

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

from googleapiclient.discovery import Resource, build  # noqa: TID251 — this file owns the legitimate use

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


def get_service(
    service: str,
    version: str,
    *,
    credentials: Credentials,
) -> Resource:
    """Return a Google API ``Resource`` for ``(service, version, credentials)``.

    Pure passthrough today — equivalent to
    ``googleapiclient.discovery.build(service, version, credentials=creds)``.
    Future enhancements (cache, retry, telemetry, async swap) land
    in this function without touching callers.

    The ``credentials`` parameter is keyword-only on purpose: any
    future caching layer MUST keep credentials in the cache key
    (different users get different Resources), and making this a
    named param keeps that property obvious at every call site.
    The matching regression guard is
    ``test_distinct_credentials_get_distinct_resources`` in
    ``tests/unit/test_google_clients.py`` — if you collapse the
    cache key across credentials, that test fails immediately.
    """
    return build(service, version, credentials=credentials)
