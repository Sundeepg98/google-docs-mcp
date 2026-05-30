"""Coverage for the v2.6a google_clients facade + v2.1.2 M2 production adapter.

The facade ``google_clients.get_service`` is a pure passthrough today;
these tests pin down the behavioral invariants that any future caching /
retry / telemetry layer MUST preserve. In particular,
``test_distinct_credentials_get_distinct_resources`` is the
mutation guard for any future cache addition: a cache that forgets
credentials in its key would let one user's Drive Resource leak
to another, and that test fails immediately on such a regression.

**v2.1.2 (M2)**: the actual ``build()`` call moved into
``google_api_client.GoogleApiClientAdapter`` (the production adapter
behind the new Hex-style Port). The patch target updated from
``appscriptly.google_clients.build`` to
``appscriptly.google_api_client.build`` — both call shapes
are equivalent since the facade delegates straight through. Port-level
tests (Protocol satisfaction, InMemoryGoogleAPIClient behavior,
injection ergonomics) live in ``tests/unit/test_google_api_client.py``.

We mock ``googleapiclient.discovery.build`` so the tests are pure
unit isolation — no network, no Google SDK construction cost.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fake_credentials(label: str) -> MagicMock:
    """A creds-shaped sentinel. The label makes assertion failures
    legible without pulling in the real Credentials class."""
    c = MagicMock(name=f"creds-{label}")
    return c


def test_get_service_returns_what_build_returns():
    """Passthrough: get_service hands back exactly what build() returned.

    If a future caching layer wraps the Resource, this test still
    holds — the cache returns the cached Resource, which IS what
    build() returned the first time.
    """
    from appscriptly.google_clients import get_service

    with patch("appscriptly.google_api_client.build") as mk_build:
        sentinel_resource = MagicMock(name="drive-v3-resource")
        mk_build.return_value = sentinel_resource

        result = get_service("drive", "v3", credentials=_fake_credentials("alice"))

    assert result is sentinel_resource, (
        "get_service must return the Resource produced by build() — "
        "wrapping it would break callers that expect a Resource."
    )
    # Sanity check: build was actually called with the args we passed,
    # keyword-only for credentials.
    mk_build.assert_called_once()
    args, kwargs = mk_build.call_args
    assert args == ("drive", "v3")
    assert "credentials" in kwargs


def test_distinct_service_tuples_get_distinct_resources():
    """Calling for (docs, v1) vs (drive, v3) returns DIFFERENT objects.

    Today this is true trivially because pure passthrough always
    asks build() afresh. The test is here so a future cache that
    uses ``(service, version)`` as its key (forgetting credentials)
    is caught — see test_distinct_credentials_get_distinct_resources.
    Same shape, different axis.
    """
    from appscriptly.google_clients import get_service

    with patch("appscriptly.google_api_client.build") as mk_build:
        # Return a distinct Resource per call so we can tell them apart.
        mk_build.side_effect = lambda *args, **kwargs: MagicMock(
            name=f"resource-{args[0]}-{args[1]}",
        )

        creds = _fake_credentials("bob")
        docs_v1 = get_service("docs", "v1", credentials=creds)
        drive_v3 = get_service("drive", "v3", credentials=creds)

    assert docs_v1 is not drive_v3, (
        "Different (service, version) tuples MUST yield different "
        "Resources — a future cache keyed only on credentials would "
        "incorrectly collapse these and hand a Docs caller a Drive "
        "Resource (or vice versa)."
    )


def test_distinct_credentials_get_distinct_resources():
    """CRITICAL: two DIFFERENT Credentials objects MUST yield different
    Resources, even for the same (service, version).

    This is the mutation guard for ANY future cache addition. If a
    cache forgets credentials in its key, user-A's Drive Resource
    leaks to user-B — that user-B's tool calls would silently operate
    on user-A's Drive. The same class of bug as the v2.0.3 operator-
    secret leak (PR #47), just on the read side.

    Today this passes trivially because pure passthrough always
    asks build() afresh. Keep the test even when caching is added:
    the only correct cache key includes credentials (or a stable
    identity derived from them, like the refresh_token hash).
    """
    from appscriptly.google_clients import get_service

    with patch("appscriptly.google_api_client.build") as mk_build:
        # Return a distinct Resource per call so we can tell them apart.
        # If a future cache uses (service, version) as key and ignores
        # credentials, the SECOND call below would return the cached
        # MagicMock from the first call, and the assertion below would
        # fire with a clear message.
        mk_build.side_effect = lambda *args, **kwargs: MagicMock(
            name=f"resource-for-{kwargs['credentials']._mock_name}",
        )

        alice_creds = _fake_credentials("alice")
        bob_creds = _fake_credentials("bob")

        alice_drive = get_service("drive", "v3", credentials=alice_creds)
        bob_drive = get_service("drive", "v3", credentials=bob_creds)

    assert alice_drive is not bob_drive, (
        "Different Credentials MUST yield different Resources for the "
        "same (service, version). A future cache that omits credentials "
        "from its key would leak one user's Drive Resource to another — "
        "the same class of bug as the v2.0.3 operator-secret leak "
        "(PR #47), just on the read side. Cache keys MUST include "
        "credentials (or a stable identity hash like refresh_token)."
    )


def test_credentials_parameter_is_keyword_only():
    """Positional credentials would let a caller accidentally pass them
    in the wrong slot. Keyword-only makes the dependency explicit at
    every call site and reads as ``get_service(..., credentials=creds)``
    — which makes "is this user A's creds or user B's?" auditable on
    visual scan."""
    import inspect

    from appscriptly.google_clients import get_service

    sig = inspect.signature(get_service)
    param = sig.parameters["credentials"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"credentials parameter must be keyword-only, got kind={param.kind}. "
        "Positional creds let a caller mix up which user's identity is "
        "being used — a security-sensitive footgun in multi-tenant mode."
    )


def test_get_service_signature_does_not_silently_swallow_kwargs():
    """No **kwargs passthrough: every future option (cache disable,
    retry override, etc.) must be a named parameter so call sites
    are greppable. A **kwargs sink would let a caller silently pass
    a misspelled option that the wrapper ignores."""
    import inspect

    from appscriptly.google_clients import get_service

    sig = inspect.signature(get_service)
    kinds = {p.kind for p in sig.parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD not in kinds, (
        "get_service must NOT accept **kwargs — every option must be "
        "a named parameter so call sites can be grepped and reviewed. "
        "A **kwargs sink silently absorbs typos."
    )
    assert inspect.Parameter.VAR_POSITIONAL not in kinds, (
        "get_service must NOT accept *args either, for the same reason."
    )
