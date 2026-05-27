"""GoogleAPIClient Port + Adapters tests (v2.1.2 / M2).

Exercises the new Hex-style port shape in isolation from
``google_clients.py``:

- Protocol conformance for both production + InMemory adapters
- InMemoryGoogleAPIClient happy path + unknown-registry KeyError
- with_google_api_client context manager (test-injection ergonomics)
- Facade delegation: google_clients.get_service routes through the
  active client (proves the seam works end-to-end)

Per M1a precedent (PR #88 + #90), this file is the **port-level**
contract test. Wrapper-contract tests on the production adapter
(passthrough invariants, cache-key safety) live in
``test_google_clients.py`` — they patch the actual ``build()`` call
to verify the production adapter's behavior, not the port shape.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from google_docs_mcp.google_api_client import (
    GoogleApiClientAdapter,
    GoogleAPIClient,
    InMemoryGoogleAPIClient,
    get_active_client,
    set_google_api_client,
    with_google_api_client,
)


# ---------------------------------------------------------------------
# Protocol conformance — both adapters satisfy GoogleAPIClient at runtime
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_factory",
    [
        lambda: GoogleApiClientAdapter(),
        lambda: InMemoryGoogleAPIClient(),
        lambda: InMemoryGoogleAPIClient({("drive", "v3"): MagicMock()}),
    ],
)
def test_adapter_satisfies_google_api_client_protocol(adapter_factory):
    adapter = adapter_factory()
    assert isinstance(adapter, GoogleAPIClient)


# ---------------------------------------------------------------------
# InMemoryGoogleAPIClient
# ---------------------------------------------------------------------


def test_inmemory_returns_registered_stub():
    stub = MagicMock(name="drive-v3-stub")
    client = InMemoryGoogleAPIClient({("drive", "v3"): stub})
    fake_creds = MagicMock(name="creds")

    result = client.get_service("drive", "v3", credentials=fake_creds)

    assert result is stub


def test_inmemory_raises_keyerror_for_unregistered_service():
    """A missing stub MUST fail loudly. Pre-v2.1.2 ``patch("...build")``
    silently returned a default MagicMock, which masked tests that
    needed a real stub for assertions. InMemoryGoogleAPIClient's
    strictness is the explicit upgrade."""
    client = InMemoryGoogleAPIClient({("drive", "v3"): MagicMock()})
    fake_creds = MagicMock()

    with pytest.raises(KeyError, match="No stub registered"):
        client.get_service("docs", "v1", credentials=fake_creds)


def test_inmemory_keyerror_lists_registered_services():
    """The error message includes the registered set so the author of
    the failing test sees what they need to register."""
    client = InMemoryGoogleAPIClient({
        ("drive", "v3"): MagicMock(),
        ("docs", "v1"): MagicMock(),
    })
    fake_creds = MagicMock()

    with pytest.raises(KeyError) as exc_info:
        client.get_service("script", "v1", credentials=fake_creds)
    msg = str(exc_info.value)
    assert "('docs', 'v1')" in msg
    assert "('drive', 'v3')" in msg


def test_inmemory_register_adds_stubs_incrementally():
    """Tests that build a base registry in a fixture + add a test-specific
    stub in the body need the incremental register() helper."""
    client = InMemoryGoogleAPIClient({("drive", "v3"): MagicMock(name="drive")})
    docs_stub = MagicMock(name="docs")
    client.register("docs", "v1", docs_stub)

    assert client.get_service("docs", "v1", credentials=MagicMock()) is docs_stub


def test_inmemory_credentials_keyword_is_ignored_by_lookup():
    """Pre-v2.1.2 ``patch("...build")`` mocks looked at the credentials
    kwarg via ``mock.call_args``. InMemoryGoogleAPIClient does NOT key
    on credentials — tests that need per-user stubs should register
    distinct stubs per credential explicitly. Documented behavior."""
    stub = MagicMock(name="shared-stub")
    client = InMemoryGoogleAPIClient({("drive", "v3"): stub})

    alice = client.get_service("drive", "v3", credentials=MagicMock(name="alice"))
    bob = client.get_service("drive", "v3", credentials=MagicMock(name="bob"))

    assert alice is bob is stub


# ---------------------------------------------------------------------
# GoogleApiClientAdapter — production adapter
# ---------------------------------------------------------------------


def test_production_adapter_delegates_to_build():
    """The production adapter is a thin wrapper around
    ``googleapiclient.discovery.build``. Patch the upstream and verify
    the adapter hands back exactly what build() returned."""
    from unittest.mock import patch

    adapter = GoogleApiClientAdapter()
    sentinel_resource = MagicMock(name="drive-v3-resource")
    fake_creds = MagicMock(name="creds")

    with patch("google_docs_mcp.google_api_client.build") as mk_build:
        mk_build.return_value = sentinel_resource
        result = adapter.get_service("drive", "v3", credentials=fake_creds)

    assert result is sentinel_resource
    mk_build.assert_called_once_with("drive", "v3", credentials=fake_creds)


# ---------------------------------------------------------------------
# with_google_api_client — injection ergonomics + restore-on-exit
# ---------------------------------------------------------------------


def test_with_google_api_client_swaps_active():
    from google_docs_mcp.google_api_client import RetryingGoogleApiClientAdapter

    stub_drive = MagicMock(name="drive-stub")
    injected = InMemoryGoogleAPIClient({("drive", "v3"): stub_drive})

    with with_google_api_client(injected):
        assert get_active_client() is injected

    # After exit: default restored. PR-Δ3 made the production default
    # a ``RetryingGoogleApiClientAdapter`` composed over the bare
    # ``GoogleApiClientAdapter`` — assert the OUTER composing type,
    # since that's what production now wires by default.
    after = get_active_client()
    assert after is not injected
    assert isinstance(after, RetryingGoogleApiClientAdapter)


def test_with_google_api_client_restores_on_exception():
    """A test failure inside the with-block must NOT leak the injection
    into subsequent tests."""
    before = get_active_client()
    injected = InMemoryGoogleAPIClient()

    with pytest.raises(RuntimeError, match="boom"):
        with with_google_api_client(injected):
            assert get_active_client() is injected
            raise RuntimeError("boom")

    assert get_active_client() is before


def test_set_google_api_client_returns_previous_for_manual_restore():
    """``set_google_api_client`` returns the prior client so tests that
    can't use the context-manager idiom (e.g. session-scoped fixtures
    with cleanup in finalizers) can save+restore manually."""
    original = get_active_client()
    new_client = InMemoryGoogleAPIClient()

    previous = set_google_api_client(new_client)
    assert previous is original
    assert get_active_client() is new_client

    # Restore for subsequent tests.
    set_google_api_client(original)
    assert get_active_client() is original


# ---------------------------------------------------------------------
# Facade end-to-end: google_clients.get_service routes through the port
# ---------------------------------------------------------------------


def test_facade_delegation_routes_to_active_client():
    """Proves the seam: ``google_clients.get_service`` (the pre-v2.1.2
    public entry point) now delegates to the active GoogleAPIClient.
    A test that injects an InMemoryGoogleAPIClient must see its stubs
    returned through the legacy import path."""
    from google_docs_mcp import google_clients

    stub = MagicMock(name="docs-v1-stub")
    fake_creds = MagicMock()

    with with_google_api_client(InMemoryGoogleAPIClient({("docs", "v1"): stub})):
        result = google_clients.get_service("docs", "v1", credentials=fake_creds)

    assert result is stub


def test_facade_keeps_resource_reexport_for_backward_compat():
    """Pre-v2.1.2 code that did ``from google_docs_mcp.google_clients
    import Resource`` for type hints must continue to work."""
    from google_docs_mcp.google_clients import Resource

    # Resource is the googleapiclient.discovery.Resource class itself.
    # We can't easily isinstance-check a real Resource (it's dynamically
    # built), but we can check it's the same object as the canonical one.
    from googleapiclient.discovery import Resource as CanonicalResource

    assert Resource is CanonicalResource
