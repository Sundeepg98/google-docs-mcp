"""Per-tool behavior tests for services/contacts/tools.py (People API v1).

Mirrors ``tests/unit/services/sheets/test_tools.py`` — per-tool
happy-path coverage at the decorator-envelope boundary, using the same
``InMemoryGoogleAPIClient`` stub pattern.

**Credential-resolution note (differs from the sheets test fixture).**
The contacts tools declare ``scopes=[CONTACTS_SCOPE]`` on
``@workspace_tool``. With a non-None ``scopes`` list, the decorator's
creds path goes through ``_resolve_credentials_for_scopes`` — which, in
stdio mode (``current_user_id_or_none() is None``), calls
``auth.load_credentials(default_data_dir(), extra_scopes=scopes)`` rather
than the ``_get_credentials_fn`` the no-scopes path uses. So this fixture
patches ``appscriptly.auth.load_credentials`` (and ``default_data_dir``),
which is what the decorator's deferred import resolves — NOT
``_get_credentials_fn`` (that branch is never reached for a scoped tool).

Per-API call-shape coverage (resourceName, field masks,
updatePersonFields, body etag) lives in ``test_api.py``; this file covers
the tool-layer envelope: the decorator resolves creds + injects them,
parameter forwarding from the decorated function into the api module, and
that validation errors propagate.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly import auth, decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.contacts import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch, tmp_path):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=[...]) envelope doesn't try real
    OAuth. The scoped path resolves via auth.load_credentials in stdio
    mode, so we patch THAT (not _get_credentials_fn).

    ``current_user_id_or_none`` is also pinned to None so the decorator
    takes the stdio branch deterministically (no dependence on ambient
    request context).
    """
    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(auth, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "appscriptly.credentials.current_user_id_or_none", lambda: None
    )


@pytest.fixture
def people_stub():
    """A People v1 Resource stub with method chains pre-wired to plausible
    defaults. Individual tests override per-call as needed."""
    people = MagicMock(name="people-v1-stub")
    people.people().connections().list().execute.return_value = {
        "connections": [], "totalPeople": 0,
    }
    people.people().searchContacts().execute.return_value = {"results": []}
    people.people().get().execute.return_value = {
        "resourceName": "people/c1", "etag": "E1", "names": [{"displayName": "Jane"}],
    }
    people.people().createContact().execute.return_value = {
        "resourceName": "people/cNEW", "etag": "ENEW", "names": [{"displayName": "Jane"}],
    }
    people.people().updateContact().execute.return_value = {
        "resourceName": "people/c1", "etag": "E2", "names": [{"displayName": "Jane"}],
    }
    people.people().deleteContact().execute.return_value = {}
    return people


@pytest.fixture
def with_people_stub(people_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({("people", "v1"): people_stub})):
        yield people_stub


def _last_real_call(mock_method, key="resourceName"):
    for call in reversed(mock_method.call_args_list):
        if key in call.kwargs:
            return call
    raise AssertionError(f"no call captured kwarg {key!r}")


# ---------------------------------------------------------------------
# Scope declaration — the per-tool scopes= annotation rides through
# ---------------------------------------------------------------------


def test_contacts_scope_constant_matches_baseline():
    """CONTACTS_SCOPE is the People API read/write scope and is present
    in the single-source baseline (so the per-tool assertion holds)."""
    from appscriptly.auth import WORKSPACE_SCOPES

    assert tools.CONTACTS_SCOPE == "https://www.googleapis.com/auth/contacts"
    assert tools.CONTACTS_SCOPE in WORKSPACE_SCOPES


# ---------------------------------------------------------------------
# 1. gcontacts_list
# ---------------------------------------------------------------------


def test_gcontacts_list_returns_envelope_for_empty(with_people_stub):
    result = tools.gcontacts_list()
    assert result == {"contacts": [], "next_page_token": None, "total_people": 0}


def test_gcontacts_list_forwards_paging(with_people_stub):
    tools.gcontacts_list(page_size=250, page_token="TOK")
    call = _last_real_call(with_people_stub.people().connections().list)
    assert call.kwargs["pageSize"] == 250
    assert call.kwargs["pageToken"] == "TOK"


def test_gcontacts_list_surfaces_contacts(with_people_stub):
    with_people_stub.people().connections().list().execute.return_value = {
        "connections": [
            {"resourceName": "people/c1", "etag": "E1", "names": [{"displayName": "A"}]},
        ],
        "nextPageToken": "N",
        "totalPeople": 1,
    }
    result = tools.gcontacts_list()
    assert result["contacts"][0]["resource_name"] == "people/c1"
    assert result["next_page_token"] == "N"


# ---------------------------------------------------------------------
# 2. gcontacts_search
# ---------------------------------------------------------------------


def test_gcontacts_search_happy_path(with_people_stub):
    with_people_stub.people().searchContacts().execute.return_value = {
        "results": [{"person": {"resourceName": "people/c1", "names": [{"displayName": "Jane"}]}}],
    }
    result = tools.gcontacts_search(query="jane")
    assert result["count"] == 1
    assert result["contacts"][0]["resource_name"] == "people/c1"


def test_gcontacts_search_validation_propagates(with_people_stub):
    with pytest.raises(ValueError, match="query cannot be empty"):
        tools.gcontacts_search(query="  ")


def test_gcontacts_search_forwards_query(with_people_stub):
    tools.gcontacts_search(query="bob", page_size=5)
    real = [
        c for c in with_people_stub.people().searchContacts.call_args_list
        if c.kwargs.get("query") == "bob"
    ]
    assert real, "real query not forwarded"
    assert real[-1].kwargs["pageSize"] == 5


# ---------------------------------------------------------------------
# 3. gcontacts_get
# ---------------------------------------------------------------------


def test_gcontacts_get_happy_path(with_people_stub):
    result = tools.gcontacts_get(resource_name="people/c1")
    assert result["resource_name"] == "people/c1"
    assert result["etag"] == "E1"


def test_gcontacts_get_normalizes_bare_id(with_people_stub):
    tools.gcontacts_get(resource_name="c999")
    call = _last_real_call(with_people_stub.people().get)
    assert call.kwargs["resourceName"] == "people/c999"


def test_gcontacts_get_validation_propagates(with_people_stub):
    with pytest.raises(ValueError, match="resource_name cannot be empty"):
        tools.gcontacts_get(resource_name="")


# ---------------------------------------------------------------------
# 4. gcontacts_create
# ---------------------------------------------------------------------


def test_gcontacts_create_happy_path(with_people_stub):
    result = tools.gcontacts_create(
        given_name="Jane", family_name="Doe", email="jane@x.com",
    )
    assert result["resource_name"] == "people/cNEW"
    call = _last_real_call(with_people_stub.people().createContact, key="body")
    assert call.kwargs["body"]["emailAddresses"] == [{"value": "jane@x.com"}]


def test_gcontacts_create_validation_propagates(with_people_stub):
    with pytest.raises(ValueError, match="at least one of"):
        tools.gcontacts_create()


# ---------------------------------------------------------------------
# 5. gcontacts_update
# ---------------------------------------------------------------------


def test_gcontacts_update_happy_path_with_etag(with_people_stub):
    result = tools.gcontacts_update(
        resource_name="people/c1", email="new@x.com", etag="MY-ETAG",
    )
    assert result["etag"] == "E2"
    call = _last_real_call(with_people_stub.people().updateContact, key="body")
    assert call.kwargs["body"]["etag"] == "MY-ETAG"
    assert call.kwargs["updatePersonFields"] == "emailAddresses"


def test_gcontacts_update_fetches_etag_when_omitted(with_people_stub):
    with_people_stub.people().get().execute.return_value = {
        "resourceName": "people/c1", "etag": "FETCHED",
    }
    tools.gcontacts_update(resource_name="people/c1", phone="+1")
    call = _last_real_call(with_people_stub.people().updateContact, key="body")
    assert call.kwargs["body"]["etag"] == "FETCHED"


def test_gcontacts_update_validation_propagates(with_people_stub):
    with pytest.raises(ValueError, match="no fields to update"):
        tools.gcontacts_update(resource_name="people/c1")


# ---------------------------------------------------------------------
# 6. gcontacts_delete
# ---------------------------------------------------------------------


def test_gcontacts_delete_happy_path(with_people_stub):
    result = tools.gcontacts_delete(resource_name="people/c1")
    assert result == {"resource_name": "people/c1", "deleted": True}


def test_gcontacts_delete_normalizes_bare_id(with_people_stub):
    tools.gcontacts_delete(resource_name="c42")
    call = _last_real_call(with_people_stub.people().deleteContact)
    assert call.kwargs["resourceName"] == "people/c42"


def test_gcontacts_delete_validation_propagates(with_people_stub):
    with pytest.raises(ValueError, match="resource_name cannot be empty"):
        tools.gcontacts_delete(resource_name="   ")


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: scoped creds resolution is invoked
# ---------------------------------------------------------------------


def test_gcontacts_list_resolves_scoped_credentials(with_people_stub, monkeypatch):
    """The @workspace_tool(creds=True, scopes=[CONTACTS_SCOPE]) decorator
    MUST resolve creds via the scoped path (auth.load_credentials with
    extra_scopes) before delegating. If a refactor rewires it, this
    fires."""
    seen = {"n": 0, "scopes": None}

    def counting_load(_data_dir, extra_scopes=None):
        seen["n"] += 1
        seen["scopes"] = extra_scopes
        return MagicMock(name="scoped-creds")

    monkeypatch.setattr(auth, "load_credentials", counting_load)
    tools.gcontacts_list()
    assert seen["n"] == 1, "scoped creds resolution was not invoked exactly once"
    assert seen["scopes"] == [tools.CONTACTS_SCOPE], (
        "the contacts scope was not threaded into the credential resolution"
    )
