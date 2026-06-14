"""Google Contacts MCP tool registrations (People API v1 — new service).

Mirrors the layout established by ``services/sheets/tools.py`` and
``services/drive/tools.py``: ``@workspace_tool``-decorated functions that
register with the live ``mcp`` instance when this module is imported.
``server.py``'s auto-discovery walk imports it (the leaf name doesn't
start with ``_`` and isn't in the ``{api, scopes}`` denylist), so
registration is a side-effect of that walk — no central import edit.

**Tools registered here** (6 contacts-service tools):

1. ``gcontacts_list``    — list the user's contacts (paged)
2. ``gcontacts_search``  — prefix-search contacts by name/email/etc.
3. ``gcontacts_get``     — fetch one contact (surfaces its etag)
4. ``gcontacts_create``  — create a contact (name/email/phone/org)
5. ``gcontacts_update``  — update a contact (read-modify-write w/ etag)
6. ``gcontacts_delete``  — delete a contact

(Authoritative declaration: ``services/contacts/_expected_tools.py``.)

**Scope — ``CONTACTS_SCOPE`` (SENSITIVE, not restricted).** Every tool
declares ``scopes=[CONTACTS_SCOPE]`` so the per-tool scope intent is
machine-readable on ``ToolAnnotations.scopes`` (telemetry / dynamic
consent UI) AND the decorator asserts the scope at credential-resolution
time. The scope is ALSO baseline-granted via ``auth.WORKSPACE_SCOPES``
(the single source), so the assertion passes on the first call without a
second consent — exactly the redundant-but-documented pattern
``services/gas_deploy/tools.py`` uses with ``GAS_DEPLOY_SCOPES``.
``contacts`` is a Google-SENSITIVE scope, NOT restricted, so it adds no
CASA obligation.

**Import discipline.** Same as ``services/sheets/tools.py``:

- ``_get_credentials`` + ``_format_http_error`` imported directly from
  ``_tool_helpers`` (the M3 Phase C extraction).
- The api module is the standard ``from ... import api`` pattern.
- ``@workspace_tool(service="contacts", ...)`` carries the service=
  literal that drives the partition test + future telemetry.
"""
from __future__ import annotations

from appscriptly.auth import WORKSPACE_SCOPES
from appscriptly.decorators import workspace_tool
from appscriptly.services.contacts.api import (
    DEFAULT_OTHER_CONTACTS_READ_MASK,
    DEFAULT_PERSON_FIELDS,
    DEFAULT_SEARCH_READ_MASK,
    create_contact as _create_contact,
    delete_contact as _delete_contact,
    get_contact as _get_contact,
    list_contacts as _list_contacts,
    list_other_contacts as _list_other_contacts,
    search_contacts as _search_contacts,
    update_contact as _update_contact,
)
from appscriptly.tool_schemas import (
    GCONTACTS_CREATE_OUTPUT_SCHEMA,
    GCONTACTS_DELETE_OUTPUT_SCHEMA,
    GCONTACTS_GET_OUTPUT_SCHEMA,
    GCONTACTS_LIST_OTHER_CONTACTS_OUTPUT_SCHEMA,
    GCONTACTS_LIST_OUTPUT_SCHEMA,
    GCONTACTS_SEARCH_OUTPUT_SCHEMA,
    GCONTACTS_UPDATE_OUTPUT_SCHEMA,
)

# Imported for parity with services/sheets/tools.py; the contacts tools
# let HttpError propagate to the standard decorator envelope, so
# _format_http_error isn't called directly here. Kept as a top-level
# import so a future tool that DOES need it doesn't add an import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)


# The People API read/write scope. SENSITIVE (not restricted → no CASA).
# Single source of truth is ``auth.WORKSPACE_SCOPES``; we resolve it from
# there rather than re-typing the literal, so a future scope-string change
# (or removal) can't leave this per-tool declaration stale. Asserted at
# module import that the expected scope is present in the baseline so a
# silent drift fails fast (and obviously, right here) rather than at the
# first tool call.
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
assert CONTACTS_SCOPE in WORKSPACE_SCOPES, (
    "contacts scope missing from auth.WORKSPACE_SCOPES — the contacts "
    "service tools declare scopes=[CONTACTS_SCOPE] but the scope isn't "
    "baseline-granted. Add it back to the single-source list."
)

# The "other contacts" read-only scope (auto-saved contacts). SENSITIVE
# (not restricted → no CASA). Strictly narrower than CONTACTS_SCOPE; it
# only serves the read-only gcontacts_list_other_contacts tool. Same
# baseline-grant + assert-at-import discipline as CONTACTS_SCOPE above.
CONTACTS_OTHER_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/contacts.other.readonly"
)
assert CONTACTS_OTHER_READONLY_SCOPE in WORKSPACE_SCOPES, (
    "contacts.other.readonly scope missing from auth.WORKSPACE_SCOPES — "
    "gcontacts_list_other_contacts declares it but it isn't baseline-"
    "granted. Add it to the single-source list."
)


# ---------------------------------------------------------------------
# 1. gcontacts_list — connections.list (pure read, paged)
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="List the user's Google Contacts",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CONTACTS_SCOPE],
    output_schema=GCONTACTS_LIST_OUTPUT_SCHEMA,
)
def gcontacts_list(
    creds,
    page_size: int = 100,
    page_token: str | None = None,
    person_fields: str = DEFAULT_PERSON_FIELDS,
    sort_order: str | None = None,
) -> dict:
    """List the authenticated user's Google Contacts (one page per call).

    USE WHEN: you need to enumerate the user's contacts — "who's in my
    contacts", "list my contacts", or to find a contact's resourceName to
    feed gcontacts_get / gcontacts_update / gcontacts_delete. To find a
    SPECIFIC person by name/email, gcontacts_search is usually faster.

    Backed by People API ``people.connections.list`` over
    ``people/me``. Returns one page; pass the returned ``next_page_token``
    back as ``page_token`` to walk subsequent pages.

    Args:
        page_size: Contacts per page (1-1000; default 100). Out-of-range
            values are clamped.
        page_token: Token from a prior call's ``next_page_token`` to fetch
            the next page. Omit to start at the first page.
        person_fields: People API field mask (comma-separated, e.g.
            ``"names,emailAddresses,phoneNumbers"``). Defaults to a core
            set; ``metadata`` is always included so each contact's etag
            (needed for updates) is returned.
        sort_order: Optional ordering — ``LAST_MODIFIED_ASCENDING`` (the
            People API default), ``LAST_MODIFIED_DESCENDING``,
            ``FIRST_NAME_ASCENDING``, or ``LAST_NAME_ASCENDING``.

    Returns:
        ``{contacts, next_page_token, total_people}`` — ``contacts`` is a
        list of flat ``{resource_name, etag, display_name, emails,
        phones, organization, raw}`` dicts; ``next_page_token`` is null on
        the last page; ``total_people`` is the user's total contact count.

    Choreography: each contact's ``resource_name`` feeds gcontacts_get,
    gcontacts_update, and gcontacts_delete; its ``etag`` feeds
    gcontacts_update directly (skips the update's etag-fetch round-trip).
    """
    return _list_contacts(
        creds,
        page_size=page_size,
        page_token=page_token,
        person_fields=person_fields,
        sort_order=sort_order,
    )


# ---------------------------------------------------------------------
# 1b. gcontacts_list_other_contacts — otherContacts.list (pure read, paged)
#     CASA-free growth: contacts.other.readonly (SENSITIVE, no CASA).
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="List the user's auto-saved 'other' contacts",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CONTACTS_OTHER_READONLY_SCOPE],
    output_schema=GCONTACTS_LIST_OTHER_CONTACTS_OUTPUT_SCHEMA,
)
def gcontacts_list_other_contacts(
    creds,
    page_size: int = 100,
    page_token: str | None = None,
    read_mask: str = DEFAULT_OTHER_CONTACTS_READ_MASK,
) -> dict:
    """List the user's "other contacts" (auto-saved, never explicitly added).

    USE WHEN: you need addresses Google auto-saved from the user's
    interactions — people they emailed/met but never added to their main
    contacts. This is a SEPARATE collection from gcontacts_list; use this
    to surface "people I've corresponded with" who aren't saved contacts.

    Backed by People API ``otherContacts.list`` over the dedicated
    read-only scope ``contacts.other.readonly`` (SENSITIVE, not restricted
    → no CASA). Returns one page; pass the returned ``next_page_token``
    back as ``page_token`` to walk subsequent pages.

    Args:
        page_size: Contacts per page (1-1000; default 100). Out-of-range
            values are clamped.
        page_token: Token from a prior call's ``next_page_token`` to fetch
            the next page. Omit to start at the first page.
        read_mask: People API field mask (comma-separated). Defaults to a
            core set valid for "other contacts" (names, emailAddresses,
            phoneNumbers, metadata). NOTE: ``organizations`` is NOT
            available for other contacts (it needs a profile source the
            read-only scope doesn't cover), so ``organization`` comes back
            null.

    Returns:
        ``{contacts, next_page_token}`` — ``contacts`` is a list of the
        same flat ``{resource_name, etag, display_name, emails, phones,
        organization, raw}`` dicts gcontacts_list returns (``organization``
        is null here); ``next_page_token`` is null on the last page.

    NOTE: "other contacts" are READ-ONLY via this scope — there is no
    create/update/delete for them. To promote one to a saved contact, read
    its fields here and create it with gcontacts_create.
    """
    return _list_other_contacts(
        creds,
        page_size=page_size,
        page_token=page_token,
        read_mask=read_mask,
    )


# ---------------------------------------------------------------------
# 2. gcontacts_search — searchContacts (pure read, prefix match)
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="Search the user's Google Contacts",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CONTACTS_SCOPE],
    output_schema=GCONTACTS_SEARCH_OUTPUT_SCHEMA,
)
def gcontacts_search(
    creds,
    query: str,
    page_size: int = 10,
    read_mask: str = DEFAULT_SEARCH_READ_MASK,
) -> dict:
    """Search the user's Google Contacts by a name / email / etc. prefix.

    USE WHEN: you have a name or email fragment and want the matching
    contact(s) — "find Jane's contact", "what's Bob's number". Faster and
    more direct than paging gcontacts_list when you know who you're after.

    Backed by People API ``people.searchContacts`` (prefix match against
    the masked fields). A warmup request is sent automatically first per
    Google's caching requirement, so results are fresh on the first call.

    Args:
        query: Plain-text prefix to match (a name, email, etc.). Required
            and non-empty — to list ALL contacts use gcontacts_list.
        page_size: Max results (1-30; default 10). searchContacts does not
            paginate, so this is the hard result cap.
        read_mask: People API field mask for the returned persons
            (comma-separated). Defaults to a core set.

    Returns:
        ``{contacts, count}`` — ``contacts`` is a list of the same flat
        ``{resource_name, etag, display_name, emails, phones,
        organization, raw}`` dicts gcontacts_list returns; ``count`` is
        its length.

    Choreography: pick the right hit, then use its ``resource_name`` /
    ``etag`` with gcontacts_get / gcontacts_update / gcontacts_delete.
    """
    return _search_contacts(
        creds,
        query,
        page_size=page_size,
        read_mask=read_mask,
    )


# ---------------------------------------------------------------------
# 3. gcontacts_get — people.get (pure read, single contact)
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="Get a single Google Contact",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CONTACTS_SCOPE],
    output_schema=GCONTACTS_GET_OUTPUT_SCHEMA,
)
def gcontacts_get(
    creds,
    resource_name: str,
    person_fields: str = DEFAULT_PERSON_FIELDS,
) -> dict:
    """Fetch one Google Contact by its resourceName.

    USE WHEN: you have a contact's ``resource_name`` (from gcontacts_list
    / gcontacts_search / gcontacts_create) and want its full current
    details — or specifically its ``etag`` to drive an explicit
    gcontacts_update.

    Backed by People API ``people.get``.

    Args:
        resource_name: The contact's resourceName (``people/c123...``) or
            a bare id — both accepted (normalized to ``people/{id}``).
        person_fields: People API field mask. Defaults to a core set;
            ``metadata`` is always included so the returned ``etag`` can
            drive gcontacts_update.

    Returns:
        A flat ``{resource_name, etag, display_name, emails, phones,
        organization, raw}`` dict for the contact. ``raw`` is the
        untouched People API Person (widen ``person_fields`` to pull
        photos / addresses / birthdays / etc. into it).

    Choreography: ``etag`` feeds gcontacts_update (pass it to skip the
    update's own etag-fetch); ``resource_name`` feeds gcontacts_delete.
    """
    return _get_contact(
        creds,
        resource_name,
        person_fields=person_fields,
    )


# ---------------------------------------------------------------------
# 4. gcontacts_create — createContact (mutating, NOT idempotent)
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="Create a new Google Contact",
    readonly=False,
    destructive=False,
    # NOT idempotent — createContact makes a fresh contact each call
    # (People keys by server-assigned resourceName, not content), matching
    # gsheets_create_spreadsheet / gdocs_create_folder.
    idempotent=False,
    external=True,
    creds=True,
    scopes=[CONTACTS_SCOPE],
    output_schema=GCONTACTS_CREATE_OUTPUT_SCHEMA,
)
def gcontacts_create(
    creds,
    given_name: str | None = None,
    family_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    person_fields: str = DEFAULT_PERSON_FIELDS,
) -> dict:
    """Create a new Google Contact from name / email / phone / organization.

    USE WHEN: the user wants to add someone to their contacts — "save
    Jane Doe, jane@x.com to my contacts", "add this person".

    Backed by People API ``people.createContact``. At least one of the
    fields below must be supplied (an empty contact is rejected).

    Args:
        given_name: First name.
        family_name: Last name.
        email: A single email address.
        phone: A single phone number.
        organization: Company / organization name.
        job_title: Job title within the organization.
        person_fields: Field mask for the RETURNED contact (defaults to a
            core set; ``metadata`` folded in for the etag).

    Returns:
        A flat ``{resource_name, etag, display_name, emails, phones,
        organization, raw}`` dict for the newly created contact — its
        ``resource_name`` is the handle for later get / update / delete.

    NOTE: NOT idempotent — calling twice creates TWO contacts (People
    assigns a fresh resourceName each time; duplicate content is allowed).
    Track the returned ``resource_name`` rather than re-creating by name.
    """
    return _create_contact(
        creds,
        given_name=given_name,
        family_name=family_name,
        email=email,
        phone=phone,
        organization=organization,
        job_title=job_title,
        person_fields=person_fields,
    )


# ---------------------------------------------------------------------
# 5. gcontacts_update — updateContact (mutating, etag read-modify-write)
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="Update an existing Google Contact",
    readonly=False,
    destructive=False,
    # Semantically idempotent (re-applying the same values yields the same
    # state). The api layer does NOT blanket-retry the write (a replay
    # would re-use a consumed etag and 400) — see api.update_contact.
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CONTACTS_SCOPE],
    output_schema=GCONTACTS_UPDATE_OUTPUT_SCHEMA,
)
def gcontacts_update(
    creds,
    resource_name: str,
    given_name: str | None = None,
    family_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    etag: str | None = None,
    person_fields: str = DEFAULT_PERSON_FIELDS,
) -> dict:
    """Update fields on an existing Google Contact.

    USE WHEN: changing a contact's details — "update Jane's phone
    number", "change Bob's email", "set the company on this contact".

    Backed by People API ``people.updateContact``. Only the fields you
    supply are overwritten; unspecified fields are left untouched (the
    update mask is derived from what you pass). The People API requires
    the contact's ``etag`` for optimistic concurrency: if you don't pass
    one, the current contact is fetched to obtain it (one extra read). If
    the contact changed since it was read, Google returns a 400
    ``failedPrecondition`` — re-fetch via gcontacts_get and retry.

    Args:
        resource_name: The contact's resourceName (``people/c123...``) or
            a bare id — both accepted.
        given_name / family_name / email / phone / organization /
            job_title: New values for the fields to change. Supply only
            the ones to overwrite; at least one is required.
        etag: The contact's current etag (from gcontacts_get /
            gcontacts_list). Omit to have it fetched automatically (costs
            an extra read).
        person_fields: Field mask for the RETURNED updated contact.

    Returns:
        A flat ``{resource_name, etag, display_name, emails, phones,
        organization, raw}`` dict for the updated contact — ``etag`` is
        the NEW post-update value (the prior one is now consumed).

    NOTE: only overwrites the fields you pass. To clear a field, the
    People API needs the field present with an empty value — this tool
    does not currently model field-clearing (supply the field with a new
    value to change it).
    """
    return _update_contact(
        creds,
        resource_name,
        given_name=given_name,
        family_name=family_name,
        email=email,
        phone=phone,
        organization=organization,
        job_title=job_title,
        etag=etag,
        person_fields=person_fields,
    )


# ---------------------------------------------------------------------
# 6. gcontacts_delete — deleteContact (destructive)
# ---------------------------------------------------------------------


@workspace_tool(
    service="contacts",
    title="Delete a Google Contact",
    readonly=False,
    destructive=True,
    # Semantically idempotent (deleting an already-gone contact 404s
    # rather than double-deleting); the api layer keeps it a single
    # attempt (destructive-op safety floor).
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CONTACTS_SCOPE],
    output_schema=GCONTACTS_DELETE_OUTPUT_SCHEMA,
)
def gcontacts_delete(creds, resource_name: str) -> dict:
    """Delete a Google Contact by its resourceName.

    USE WHEN: removing someone from the user's contacts — "delete this
    contact", "remove Jane from my contacts".

    Backed by People API ``people.deleteContact``. The deletion is
    permanent from the API's perspective (it does not move to a trash the
    API can restore from), so confirm intent before calling.

    Args:
        resource_name: The contact's resourceName (``people/c123...``) or
            a bare id — both accepted (normalized to ``people/{id}``).
            From gcontacts_list / gcontacts_search / gcontacts_get.

    Returns:
        ``{resource_name, deleted: True}`` — ``resource_name`` echoes the
        normalized id that was removed.

    NOTE: there is no paired undelete tool — a deleted contact cannot be
    restored via this API. Be sure of the ``resource_name`` (use
    gcontacts_get first to confirm it's the right person) before deleting.
    """
    return _delete_contact(creds, resource_name)
