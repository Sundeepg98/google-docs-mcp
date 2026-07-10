"""BUG 1a/1b (2026-07-09 OOM) — static discovery pin + bounded Resource cache.

Three contract groups:

1. **Production adapter discovery flags** — ``build()`` is invoked with
   ``static_discovery=True`` + ``cache_discovery=False`` + a per-request
   ``requestBuilder``, and degrades to dynamic discovery ONLY when the
   library has no bundled document for the (service, version).
2. **Static coverage witness** — every ``(service, version)`` literal the
   codebase passes to ``get_service`` has a BUNDLED static discovery
   document in the pinned google-api-python-client, so the dynamic
   fallback is dead code in production. A dependency bump that drops a
   bundled doc (or a new service without one) fails HERE, not as a
   surprise network fetch on a 512MB machine.
3. **CachingGoogleApiClientAdapter** — bounded LRU keyed
   ``(service, version, credential identity)``: hit/miss discipline,
   per-credential isolation, re-grant invalidation, LRU bound, fail-open
   bypass for identity-less credentials, kill switch, thread smoke.

The cache tests build their OWN adapter instances — never the module
default — so no Resource leaks across test boundaries.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from googleapiclient.errors import UnknownApiNameOrVersion

from appscriptly import google_api_client as gac
from appscriptly.google_api_client import (
    CachingGoogleApiClientAdapter,
    GoogleApiClientAdapter,
    RetryingGoogleApiClientAdapter,
    _credential_cache_identity,
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "appscriptly"

# The service surface the repo builds today. The scan below must find at
# LEAST these — if a refactor hides the literals from the regex, this
# floor keeps the witness honest instead of silently passing on an
# empty set.
_KNOWN_SERVICE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("docs", "v1"),
        ("drive", "v3"),
        ("sheets", "v4"),
        ("slides", "v1"),
        ("forms", "v1"),
        ("calendar", "v3"),
        ("tasks", "v1"),
        ("people", "v1"),
        ("gmail", "v1"),
        ("script", "v1"),
    }
)


def _scan_service_pairs() -> set[tuple[str, str]]:
    """Every (service, version) literal passed to get_service in src/."""
    pattern = re.compile(
        r"""get_service\(\s*["']([a-z_]+)["']\s*,\s*["'](v\d+)["']"""
    )
    pairs: set[tuple[str, str]] = set()
    for py in _SRC_ROOT.rglob("*.py"):
        pairs.update(pattern.findall(py.read_text(encoding="utf-8")))
    return pairs


# ---------------------------------------------------------------------
# (1) Production adapter discovery flags
# ---------------------------------------------------------------------


def test_build_called_with_static_discovery_and_request_builder():
    fake_build = MagicMock(name="build", return_value=MagicMock(name="Resource"))
    with patch.object(gac, "build", fake_build):
        GoogleApiClientAdapter().get_service(
            "docs", "v1", credentials=MagicMock(name="creds")
        )

    fake_build.assert_called_once()
    args, kwargs = fake_build.call_args
    assert args == ("docs", "v1")
    assert kwargs.get("static_discovery") is True, (
        "build() must pin static_discovery=True — relying on the library "
        "default leaves the 512MB machine one dependency-default change "
        "away from a per-call network discovery fetch (BUG 1a)."
    )
    assert kwargs.get("cache_discovery") is False, (
        "cache_discovery must be False: the oauth2client-era file cache "
        "never works (log-spams 'file_cache is only supported with "
        "oauth2client<4.0.0' on every build) and our caching happens at "
        "the Resource layer instead."
    )
    assert callable(kwargs.get("requestBuilder")), (
        "build() must receive the per-request transport builder — without "
        "it a cached Resource would share ONE httplib2.Http across "
        "threads, which httplib2 does not support."
    )
    assert "credentials" not in kwargs


def test_missing_static_doc_falls_back_to_dynamic_discovery():
    sentinel = MagicMock(name="Resource")
    fake_build = MagicMock(
        name="build", side_effect=[UnknownApiNameOrVersion("nope"), sentinel]
    )
    with patch.object(gac, "build", fake_build):
        result = GoogleApiClientAdapter().get_service(
            "someglapi", "v9", credentials=MagicMock(name="creds")
        )

    assert result is sentinel
    assert fake_build.call_count == 2
    retry_kwargs = fake_build.call_args_list[1].kwargs
    assert retry_kwargs.get("static_discovery") is False, (
        "the fallback attempt must explicitly request dynamic discovery"
    )


def test_static_discovery_covers_every_service_we_build():
    """No production build should ever take the dynamic fallback."""
    from googleapiclient import discovery_cache

    pairs = _scan_service_pairs()
    assert pairs >= _KNOWN_SERVICE_PAIRS, (
        "source scan lost known get_service literals — update the scan "
        f"or the expectation; missing: {sorted(_KNOWN_SERVICE_PAIRS - pairs)}"
    )
    missing = sorted(
        f"{service}.{version}"
        for service, version in pairs
        if discovery_cache.get_static_doc(service, version) is None
    )
    assert not missing, (
        f"no bundled static discovery document for: {missing}. Either the "
        "google-api-python-client pin dropped it (check the dep bump) or "
        "a new service was added that the bundle lacks — production would "
        "silently degrade to per-call network discovery for these."
    )


def test_request_builder_gives_each_request_its_own_transport():
    """Real construction, zero network: two requests created from ONE
    Resource must carry two DISTINCT AuthorizedHttp transports (each with
    the socket deadline), neither being the Resource's shared baseline.
    This is the property that makes a cached Resource thread-safe to
    share — httplib2.Http instances must never cross threads."""
    from google.oauth2.credentials import Credentials

    creds = Credentials(token="unit-test-token")  # never used on the wire
    resource = GoogleApiClientAdapter().get_service(
        "docs", "v1", credentials=creds
    )

    req_a = resource.documents().get(documentId="A")
    req_b = resource.documents().get(documentId="B")

    assert req_a.http is not req_b.http, (
        "each HttpRequest must get a FRESH AuthorizedHttp; sharing one "
        "shares its httplib2 connection state across threads"
    )
    assert req_a.http is not resource._http
    assert req_b.http is not resource._http
    # The per-request transports keep the Hardening-P1 socket deadline.
    assert req_a.http.http.timeout == gac._resolve_http_timeout_seconds()
    # And they authorize as the SAME credentials object (the documented
    # googleapiclient thread-safety pattern shares credentials, never Http).
    assert req_a.http.credentials is creds
    assert req_b.http.credentials is creds


# ---------------------------------------------------------------------
# (2) Credential cache identity
# ---------------------------------------------------------------------


def test_identity_prefers_refresh_token_and_is_hashed():
    creds = SimpleNamespace(refresh_token="rt-alice", token="at-alice")
    identity = _credential_cache_identity(creds)
    assert identity is not None
    assert identity.startswith("refresh_token:")
    assert "rt-alice" not in identity, "raw token material must never be a key"


def test_identity_falls_back_to_access_token():
    creds = SimpleNamespace(refresh_token=None, token="at-bob")
    identity = _credential_cache_identity(creds)
    assert identity is not None
    assert identity.startswith("token:")
    assert "at-bob" not in identity


def test_identity_is_none_for_tokenless_credentials():
    assert _credential_cache_identity(SimpleNamespace()) is None
    assert (
        _credential_cache_identity(SimpleNamespace(refresh_token=None, token=None))
        is None
    )
    # MagicMock attrs are Mocks, not str — must NOT be treated as identity.
    assert _credential_cache_identity(MagicMock()) is None


# ---------------------------------------------------------------------
# (3) CachingGoogleApiClientAdapter behavior
# ---------------------------------------------------------------------


class _RecordingInner:
    """Inner adapter double: distinct sentinel Resource per build."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def get_service(self, service, version, *, credentials):
        self.calls.append((service, version, credentials))
        return MagicMock(name=f"resource-{service}-{version}-#{len(self.calls)}")


def _creds(refresh_token: str) -> SimpleNamespace:
    return SimpleNamespace(refresh_token=refresh_token, token="access")


def test_same_identity_hits_cache_even_across_fresh_credential_objects():
    """The load-bearing win: per-tool-call Credentials objects are FRESH
    instances for the same user; the cache must still hit."""
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=8)

    first = adapter.get_service("docs", "v1", credentials=_creds("rt-alice"))
    second = adapter.get_service("docs", "v1", credentials=_creds("rt-alice"))

    assert first is second
    assert len(inner.calls) == 1, "second call must be served from cache"


def test_distinct_users_never_share_a_resource():
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=8)

    alice = adapter.get_service("drive", "v3", credentials=_creds("rt-alice"))
    bob = adapter.get_service("drive", "v3", credentials=_creds("rt-bob"))

    assert alice is not bob, (
        "cross-tenant Resource sharing — the PR #47 anti-pattern the "
        "keyword-only credentials parameter exists to prevent"
    )
    assert len(inner.calls) == 2


def test_distinct_service_tuples_are_distinct_entries():
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=8)

    docs = adapter.get_service("docs", "v1", credentials=_creds("rt-alice"))
    drive = adapter.get_service("drive", "v3", credentials=_creds("rt-alice"))

    assert docs is not drive
    assert len(inner.calls) == 2


def test_regrant_with_new_refresh_token_rebuilds():
    """Revoke + re-authorize issues a NEW refresh token; the stale
    Resource (bound to the revoked credentials) must not be served."""
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=8)

    before = adapter.get_service("docs", "v1", credentials=_creds("rt-old-grant"))
    after = adapter.get_service("docs", "v1", credentials=_creds("rt-new-grant"))

    assert before is not after
    assert len(inner.calls) == 2


def test_identity_less_credentials_bypass_the_cache():
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=8)

    creds = MagicMock(name="opaque-creds")
    a = adapter.get_service("docs", "v1", credentials=creds)
    b = adapter.get_service("docs", "v1", credentials=creds)

    assert a is not b, "no stable identity means build per call, never guess"
    assert len(inner.calls) == 2


def test_lru_bound_evicts_least_recently_used():
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=2)

    a1 = adapter.get_service("docs", "v1", credentials=_creds("rt-a"))
    adapter.get_service("docs", "v1", credentials=_creds("rt-b"))
    # Touch A so B becomes least-recently-used, then insert C to evict B.
    assert adapter.get_service("docs", "v1", credentials=_creds("rt-a")) is a1
    c1 = adapter.get_service("docs", "v1", credentials=_creds("rt-c"))
    assert len(inner.calls) == 3  # a, b, c — the A touch was a hit

    # B was evicted, so it rebuilds (4th call) — and at bound 2 that
    # insert in turn evicts A (C is more recent).
    adapter.get_service("docs", "v1", credentials=_creds("rt-b"))
    assert len(inner.calls) == 4
    assert adapter.get_service("docs", "v1", credentials=_creds("rt-c")) is c1
    assert len(inner.calls) == 4  # C survived
    adapter.get_service("docs", "v1", credentials=_creds("rt-a"))
    assert len(inner.calls) == 5  # A was evicted by B's re-insert


def test_zero_max_entries_is_a_passthrough_kill_switch():
    inner = _RecordingInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=0)

    a = adapter.get_service("docs", "v1", credentials=_creds("rt-a"))
    b = adapter.get_service("docs", "v1", credentials=_creds("rt-a"))

    assert a is not b
    assert len(inner.calls) == 2


def test_max_entries_env_override_and_malformed_fallback(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_SERVICE_CACHE_MAX_ENTRIES", "7")
    assert gac._resolve_service_cache_max_entries() == 7
    monkeypatch.setenv("GOOGLE_API_SERVICE_CACHE_MAX_ENTRIES", "not-a-number")
    assert (
        gac._resolve_service_cache_max_entries()
        == gac._DEFAULT_SERVICE_CACHE_MAX_ENTRIES
    )
    monkeypatch.delenv("GOOGLE_API_SERVICE_CACHE_MAX_ENTRIES")
    assert (
        gac._resolve_service_cache_max_entries()
        == gac._DEFAULT_SERVICE_CACHE_MAX_ENTRIES
    )


def test_concurrent_same_key_converges_on_one_resource():
    """Thread smoke: build races are allowed (build runs outside the
    lock) but every caller must end up holding the SAME winning Resource
    once the dust settles, and the cache must contain exactly one entry."""
    barrier = threading.Barrier(8)

    class _SlowInner(_RecordingInner):
        def get_service(self, service, version, *, credentials):
            barrier.wait(timeout=5)  # maximize the miss race
            return super().get_service(service, version, credentials=credentials)

    inner = _SlowInner()
    adapter = CachingGoogleApiClientAdapter(inner, max_entries=8)
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        resource = adapter.get_service("docs", "v1", credentials=_creds("rt-a"))
        with lock:
            results.append(resource)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 8
    assert len({id(r) for r in results}) == 1, (
        "race losers must return the winning cache entry, not their own build"
    )
    follow_up = adapter.get_service("docs", "v1", credentials=_creds("rt-a"))
    assert follow_up is results[0]


def test_default_active_client_composes_retry_over_cache():
    """Wiring witness: production get_service flows retry -> cache -> build."""
    client = gac.get_active_client()
    assert isinstance(client, RetryingGoogleApiClientAdapter)
    assert isinstance(client._inner, CachingGoogleApiClientAdapter)
    assert isinstance(client._inner._inner, GoogleApiClientAdapter)
