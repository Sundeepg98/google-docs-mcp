"""Co-located tests for services/contacts/api.py (People API v1).

Mirrors ``tests/unit/services/sheets/test_api.py``: exercise the module
via ``with_google_api_client(InMemoryGoogleAPIClient)`` so the real
``get_service`` chokepoint runs but the People API HTTP boundary is
stubbed. No real OAuth, no real People round-trip.

Tests cover:

1. **Module-level constants / helpers** — the default field masks,
   ``_normalize_resource_name``, ``_ensure_metadata``, the page-size
   clamps, and the ``_simplify_person`` / ``_build_person_body``
   projections (the load-bearing pure logic).
2. **Pre-API validation** — empty query (search), empty resource_name
   (get/update/delete), empty contact body (create), no-fields update.
3. **People API call shape** — the right method chain
   (``people.connections().list`` / ``searchContacts`` / ``get`` /
   ``createContact`` / ``updateContact`` / ``deleteContact``) receives the
   right kwargs (resourceName, field masks, updatePersonFields, body etag).
4. **Response envelope shape** — the flat projections the tool layer
   surfaces.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.contacts.api import (
    DEFAULT_PERSON_FIELDS,
    DEFAULT_SEARCH_READ_MASK,
    _build_person_body,
    _ensure_metadata,
    _normalize_resource_name,
    _simplify_person,
    create_contact,
    delete_contact,
    get_contact,
    list_contacts,
    search_contacts,
    update_contact,
)


# ---------------------------------------------------------------------
# Module-level constants — public surface canary
# ---------------------------------------------------------------------


def test_default_person_fields_includes_metadata_for_etag():
    """The default read mask MUST include metadata — that's where the
    etag lives, and the etag is required to update a contact. A stray
    edit dropping it would silently break the update read-modify-write."""
    assert "metadata" in DEFAULT_PERSON_FIELDS
    assert "names" in DEFAULT_PERSON_FIELDS
    assert "emailAddresses" in DEFAULT_PERSON_FIELDS


def test_default_search_read_mask_is_core_set():
    assert "names" in DEFAULT_SEARCH_READ_MASK
    assert "emailAddresses" in DEFAULT_SEARCH_READ_MASK


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------


def test_normalize_resource_name_accepts_full_form():
    assert _normalize_resource_name("people/c123") == "people/c123"


def test_normalize_resource_name_prefixes_bare_id():
    assert _normalize_resource_name("c123") == "people/c123"


def test_normalize_resource_name_strips_whitespace():
    assert _normalize_resource_name("  people/c9  ") == "people/c9"


def test_normalize_resource_name_rejects_empty():
    with pytest.raises(ValueError, match="resource_name cannot be empty"):
        _normalize_resource_name("   ")


def test_ensure_metadata_appends_when_missing():
    assert _ensure_metadata("names,emailAddresses").endswith(",metadata")


def test_ensure_metadata_no_duplicate_when_present():
    out = _ensure_metadata("names,metadata")
    assert out.count("metadata") == 1


def test_simplify_person_flattens_core_fields():
    person = {
        "resourceName": "people/c1",
        "etag": "ETAG1",
        "names": [{"displayName": "Jane Doe", "givenName": "Jane", "familyName": "Doe"}],
        "emailAddresses": [{"value": "jane@x.com"}, {"value": "j2@x.com"}],
        "phoneNumbers": [{"value": "+15551234"}],
        "organizations": [{"name": "Acme", "title": "Engineer"}],
    }
    out = _simplify_person(person)
    assert out["resource_name"] == "people/c1"
    assert out["etag"] == "ETAG1"
    assert out["display_name"] == "Jane Doe"
    assert out["emails"] == ["jane@x.com", "j2@x.com"]
    assert out["phones"] == ["+15551234"]
    assert out["organization"] == "Engineer, Acme"
    assert out["raw"] is person


def test_simplify_person_handles_sparse_contact():
    """A contact with only an email (no name/phone/org) still projects
    cleanly with nulls — not a KeyError."""
    out = _simplify_person({"resourceName": "people/c2", "emailAddresses": [{"value": "a@b.com"}]})
    assert out["display_name"] is None
    assert out["phones"] == []
    assert out["organization"] is None
    assert out["emails"] == ["a@b.com"]


def test_build_person_body_mask_lists_only_supplied_fields():
    """The update mask must name ONLY the fields actually set — so an
    update never clobbers an attribute the caller didn't mention."""
    body, fields = _build_person_body(email="new@x.com")
    assert body == {"emailAddresses": [{"value": "new@x.com"}]}
    assert fields == ["emailAddresses"]


def test_build_person_body_full_contact():
    body, fields = _build_person_body(
        given_name="Jane", family_name="Doe", email="j@x.com",
        phone="+1", organization="Acme", job_title="Eng",
    )
    assert set(fields) == {"names", "emailAddresses", "phoneNumbers", "organizations"}
    assert body["names"] == [{"givenName": "Jane", "familyName": "Doe"}]
    assert body["organizations"] == [{"name": "Acme", "title": "Eng"}]


def test_build_person_body_empty_when_nothing_supplied():
    body, fields = _build_person_body()
    assert body == {}
    assert fields == []


# ---------------------------------------------------------------------
# Fixtures — a People v1 Resource stub
# ---------------------------------------------------------------------


@pytest.fixture
def people_stub():
    """A People v1 Resource stub with the method chains pre-wired to
    plausible defaults. Individual tests override per-call as needed."""
    people = MagicMock(name="people-v1-stub")
    people.people().connections().list().execute.return_value = {
        "connections": [],
        "totalPeople": 0,
    }
    people.people().searchContacts().execute.return_value = {"results": []}
    people.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "ETAG1",
        "names": [{"displayName": "Jane"}],
    }
    people.people().createContact().execute.return_value = {
        "resourceName": "people/cNEW",
        "etag": "ETAGNEW",
        "names": [{"displayName": "Jane"}],
    }
    people.people().updateContact().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "ETAG2",
        "names": [{"displayName": "Jane"}],
    }
    people.people().deleteContact().execute.return_value = {}
    return people


@pytest.fixture
def with_people_stub(people_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({("people", "v1"): people_stub})):
        yield people_stub


def _last_real_call(mock_method, key="resourceName"):
    """Most recent call to a stubbed method that carried a real kwarg
    (the shared fixture pre-calls each chain arg-less during setup)."""
    for call in reversed(mock_method.call_args_list):
        if key in call.kwargs:
            return call
    raise AssertionError(f"no call captured kwarg {key!r}")


# ---------------------------------------------------------------------
# list_contacts — connections.list call shape + envelope
# ---------------------------------------------------------------------


def test_list_targets_people_me_with_metadata_mask(with_people_stub):
    list_contacts(MagicMock())
    call = _last_real_call(with_people_stub.people().connections().list)
    assert call.kwargs["resourceName"] == "people/me"
    assert "metadata" in call.kwargs["personFields"]
    assert call.kwargs["pageSize"] == 100


def test_list_clamps_oversized_page_size(with_people_stub):
    list_contacts(MagicMock(), page_size=99999)
    call = _last_real_call(with_people_stub.people().connections().list)
    assert call.kwargs["pageSize"] == 1000  # clamped to max


def test_list_forwards_page_token_and_sort_order(with_people_stub):
    list_contacts(MagicMock(), page_token="TOK", sort_order="LAST_NAME_ASCENDING")
    call = _last_real_call(with_people_stub.people().connections().list)
    assert call.kwargs["pageToken"] == "TOK"
    assert call.kwargs["sortOrder"] == "LAST_NAME_ASCENDING"


def test_list_returns_flat_envelope(with_people_stub):
    with_people_stub.people().connections().list().execute.return_value = {
        "connections": [
            {"resourceName": "people/c1", "etag": "E1", "names": [{"displayName": "A"}]},
        ],
        "nextPageToken": "NEXT",
        "totalPeople": 42,
    }
    out = list_contacts(MagicMock())
    assert out["next_page_token"] == "NEXT"
    assert out["total_people"] == 42
    assert out["contacts"][0]["resource_name"] == "people/c1"


# ---------------------------------------------------------------------
# search_contacts — searchContacts call shape, warmup, validation
# ---------------------------------------------------------------------


def test_search_rejects_empty_query(with_people_stub):
    with pytest.raises(ValueError, match="query cannot be empty"):
        search_contacts(MagicMock(), "   ")


def test_search_sends_warmup_then_real_query(with_people_stub):
    """Per Google's caching requirement, a warmup (empty query) is sent
    before the real query."""
    search_contacts(MagicMock(), "jane")
    calls = with_people_stub.people().searchContacts.call_args_list
    queries = [c.kwargs.get("query") for c in calls if "query" in c.kwargs]
    assert "" in queries, "warmup (empty query) request was not sent"
    assert "jane" in queries, "real query request was not sent"


def test_search_warmup_failure_does_not_abort(with_people_stub):
    """A failed warmup must not block the real search (best-effort)."""
    real_execute = MagicMock(return_value={"results": []})

    call_state = {"n": 0}

    def execute_side_effect():
        call_state["n"] += 1
        if call_state["n"] == 1:  # the warmup
            raise RuntimeError("warmup boom")
        return {"results": []}

    with_people_stub.people().searchContacts().execute.side_effect = execute_side_effect
    # Should NOT raise despite the warmup failing.
    out = search_contacts(MagicMock(), "jane")
    assert out == {"contacts": [], "count": 0}
    _ = real_execute  # silence unused


def test_search_clamps_page_size_to_30(with_people_stub):
    search_contacts(MagicMock(), "jane", page_size=999)
    real = [
        c for c in with_people_stub.people().searchContacts.call_args_list
        if c.kwargs.get("query") == "jane"
    ]
    assert real[-1].kwargs["pageSize"] == 30


def test_search_unwraps_results_person(with_people_stub):
    with_people_stub.people().searchContacts().execute.return_value = {
        "results": [
            {"person": {"resourceName": "people/c1", "names": [{"displayName": "Jane"}]}},
            {"person": {"resourceName": "people/c2", "names": [{"displayName": "Jan"}]}},
        ],
    }
    out = search_contacts(MagicMock(), "ja")
    assert out["count"] == 2
    assert {c["resource_name"] for c in out["contacts"]} == {"people/c1", "people/c2"}


# ---------------------------------------------------------------------
# get_contact — people.get call shape + normalization
# ---------------------------------------------------------------------


def test_get_normalizes_bare_id_and_forces_metadata(with_people_stub):
    get_contact(MagicMock(), "c123")
    call = _last_real_call(with_people_stub.people().get)
    assert call.kwargs["resourceName"] == "people/c123"
    assert "metadata" in call.kwargs["personFields"]


def test_get_returns_flat_projection(with_people_stub):
    out = get_contact(MagicMock(), "people/c1")
    assert out["resource_name"] == "people/c1"
    assert out["etag"] == "ETAG1"


# ---------------------------------------------------------------------
# create_contact — createContact body + validation
# ---------------------------------------------------------------------


def test_create_rejects_empty_contact(with_people_stub):
    with pytest.raises(ValueError, match="at least one of"):
        create_contact(MagicMock())


def test_create_builds_body_and_returns_new_contact(with_people_stub):
    out = create_contact(
        MagicMock(), given_name="Jane", family_name="Doe", email="jane@x.com",
    )
    call = _last_real_call(with_people_stub.people().createContact, key="body")
    body = call.kwargs["body"]
    assert body["names"] == [{"givenName": "Jane", "familyName": "Doe"}]
    assert body["emailAddresses"] == [{"value": "jane@x.com"}]
    assert "metadata" in call.kwargs["personFields"]
    assert out["resource_name"] == "people/cNEW"


# ---------------------------------------------------------------------
# update_contact — etag read-modify-write + mask + validation
# ---------------------------------------------------------------------


def test_update_rejects_no_fields(with_people_stub):
    with pytest.raises(ValueError, match="no fields to update"):
        update_contact(MagicMock(), "people/c1")


def test_update_fetches_etag_when_not_supplied(with_people_stub):
    """Without an explicit etag, update first reads the contact (for its
    etag) then writes with it."""
    with_people_stub.people().get().execute.return_value = {
        "resourceName": "people/c1", "etag": "FETCHED-ETAG",
    }
    update_contact(MagicMock(), "people/c1", email="new@x.com")
    # The get() (etag fetch) ran with a metadata mask.
    get_call = _last_real_call(with_people_stub.people().get)
    assert get_call.kwargs["personFields"] == "metadata"
    # The update body carries the fetched etag + the derived mask.
    upd_call = _last_real_call(with_people_stub.people().updateContact, key="body")
    assert upd_call.kwargs["body"]["etag"] == "FETCHED-ETAG"
    assert upd_call.kwargs["updatePersonFields"] == "emailAddresses"


def test_update_uses_supplied_etag_without_extra_fetch(with_people_stub):
    """A supplied etag skips the read — the get() chain is never called
    with a real resourceName for this update."""
    before = len([
        c for c in with_people_stub.people().get.call_args_list
        if "resourceName" in c.kwargs
    ])
    update_contact(MagicMock(), "people/c1", email="new@x.com", etag="MY-ETAG")
    after = len([
        c for c in with_people_stub.people().get.call_args_list
        if "resourceName" in c.kwargs
    ])
    assert after == before, "update fetched the etag despite one being supplied"
    upd_call = _last_real_call(with_people_stub.people().updateContact, key="body")
    assert upd_call.kwargs["body"]["etag"] == "MY-ETAG"


def test_update_returns_new_etag(with_people_stub):
    out = update_contact(MagicMock(), "people/c1", email="new@x.com", etag="OLD")
    assert out["etag"] == "ETAG2"  # the post-update etag from the stub


# ---------------------------------------------------------------------
# delete_contact — deleteContact call shape + envelope
# ---------------------------------------------------------------------


def test_delete_normalizes_and_echoes(with_people_stub):
    out = delete_contact(MagicMock(), "c777")
    call = _last_real_call(with_people_stub.people().deleteContact)
    assert call.kwargs["resourceName"] == "people/c777"
    assert out == {"resource_name": "people/c777", "deleted": True}


def test_delete_rejects_empty(with_people_stub):
    with pytest.raises(ValueError, match="resource_name cannot be empty"):
        delete_contact(MagicMock(), "")
