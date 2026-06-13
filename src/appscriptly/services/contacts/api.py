"""Google People API v1 wrapper — personal contacts read/write.

The contact surface, mapped to People API v1 methods:

  * ``list_contacts``    — ``people.connections.list`` (resourceName
    ``people/me``, paged via pageToken; personFields field mask)
  * ``search_contacts``  — ``people.searchContacts`` (prefix query +
    readMask; warmup-cache request sent first per Google's guidance)
  * ``get_contact``      — ``people.get`` (resourceName ``people/{id}``;
    personFields field mask; surfaces the etag for update flows)
  * ``create_contact``   — ``people.createContact`` (Person body from
    name / email / phone / organization)
  * ``update_contact``   — ``people.updateContact`` (read-modify-write
    with the contact's etag; updatePersonFields mask)
  * ``delete_contact``   — ``people.deleteContact`` (resourceName
    ``people/{id}``)

**Scope.** Calls require ``https://www.googleapis.com/auth/contacts`` in
the OAuth consent (the FULL read/write scope — create/update/delete
mutate). This scope is Google-SENSITIVE, not restricted, so it adds no
CASA obligation (see ``services/contacts/__init__.py`` + the
``auth.WORKSPACE_SCOPES`` comment).

**Field masks are mandatory on the People API.** There is no
"return all fields" default for reads — ``personFields`` (get / list) and
``readMask`` (search) MUST be supplied. We default both to a useful core
set (names, emailAddresses, phoneNumbers, organizations, metadata) so a
zero-config call returns the fields an agent almost always wants;
``metadata`` is always folded in so the etag (needed for updates) comes
back. Writes take ``updatePersonFields`` naming exactly which attributes
to overwrite — we derive that mask from whichever of name/email/phone/org
the caller actually supplied so an update never clobbers a field the
caller didn't mean to touch.

**Resource names.** A contact's stable id is its ``resourceName``
(``people/c12345...``), returned by every read. The tools accept either
the full ``people/...`` form or a bare id and normalize to the canonical
``people/{id}`` (``_normalize_resource_name``) so an agent can pass back
whatever it last saw.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Default field mask for reads (get / list). The People API has no
# "return everything" default, so this is the useful core an agent
# almost always wants. ``metadata`` is ALWAYS included downstream (see
# ``_ensure_metadata``) so the etag — required to update a contact —
# round-trips even if a caller passes a narrower mask.
DEFAULT_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,metadata"

# Default readMask for searchContacts. Same core set; searchContacts'
# readMask does NOT accept ``metadata`` as a top-level value the way
# personFields does, so the search mask omits it (the search response
# still carries each person's resourceName + etag regardless of mask).
DEFAULT_SEARCH_READ_MASK = "names,emailAddresses,phoneNumbers,organizations"

# People API list page size bounds (connections.list): 1-1000, default
# 100. We clamp into range so an out-of-bounds request gets a clean
# result instead of a Google 400.
_LIST_PAGE_SIZE_MIN = 1
_LIST_PAGE_SIZE_MAX = 1000
_LIST_PAGE_SIZE_DEFAULT = 100

# searchContacts page size bounds: 1-30, default 10. Larger values are
# capped by Google; we clamp client-side for a predictable result count.
_SEARCH_PAGE_SIZE_MIN = 1
_SEARCH_PAGE_SIZE_MAX = 30
_SEARCH_PAGE_SIZE_DEFAULT = 10


def _normalize_resource_name(resource_name: str) -> str:
    """Normalize a contact id to the canonical ``people/{id}`` form.

    Accepts either the full ``people/c123...`` resourceName (what every
    read returns) or a bare ``c123...`` id, so a caller can pass back
    whatever they last saw. Empty / whitespace is rejected — the People
    API would 400 on it with a less clear message.
    """
    if not resource_name or not resource_name.strip():
        raise ValueError(
            "resource_name cannot be empty — pass a contact resourceName "
            "like 'people/c12345' (the id returned by gcontacts_list / "
            "gcontacts_search / gcontacts_create)."
        )
    rn = resource_name.strip()
    if rn.startswith("people/"):
        return rn
    return f"people/{rn}"


def _ensure_metadata(person_fields: str) -> str:
    """Ensure the read mask includes ``metadata`` (carries the etag).

    The etag is required to update a contact, and lives under
    ``metadata``. Folding it into every read mask means a contact fetched
    for display can be updated without a second round-trip. De-dupes so
    we never send ``metadata`` twice.
    """
    fields = [f.strip() for f in person_fields.split(",") if f.strip()]
    if "metadata" not in fields:
        fields.append("metadata")
    return ",".join(fields)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _simplify_person(person: dict) -> dict:
    """Flatten a People API ``Person`` into a compact, agent-friendly dict.

    The raw Person is deeply nested (every field is a list of objects with
    per-value ``metadata``). For an MCP tool a flat shape is far more
    useful: the resourceName + etag (the handles the other tools consume),
    the display name, and the primary email / phone / organization. The
    full raw People API value lists are NOT discarded silently — callers
    that need the complete structure can widen the field mask and read
    ``raw`` (always included) for the untouched Person.
    """
    names = person.get("names") or []
    display_name = names[0].get("displayName") if names else None
    if not display_name and names:
        # Fall back to given+family if displayName wasn't in the mask.
        given = names[0].get("givenName", "")
        family = names[0].get("familyName", "")
        display_name = (f"{given} {family}").strip() or None

    emails = [e.get("value") for e in (person.get("emailAddresses") or []) if e.get("value")]
    phones = [p.get("value") for p in (person.get("phoneNumbers") or []) if p.get("value")]
    orgs = person.get("organizations") or []
    organization = None
    if orgs:
        org_name = orgs[0].get("name")
        org_title = orgs[0].get("title")
        if org_name and org_title:
            organization = f"{org_title}, {org_name}"
        else:
            organization = org_name or org_title

    return {
        "resource_name": person.get("resourceName", ""),
        "etag": person.get("etag"),
        "display_name": display_name,
        "emails": emails,
        "phones": phones,
        "organization": organization,
        # Always carry the untouched Person so a caller who widened the
        # field mask (photos, addresses, birthdays, …) isn't forced back
        # through the flat projection.
        "raw": person,
    }


def _build_person_body(
    *,
    given_name: str | None = None,
    family_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
) -> tuple[dict, list[str]]:
    """Build a People API ``Person`` body + the field-mask it populates.

    Returns ``(person_body, update_person_fields)``. The mask lists ONLY
    the top-level Person fields actually set from the supplied args — so a
    create requests exactly what it sends, and an update (which reuses
    this) overwrites ONLY those fields, never clobbering an attribute the
    caller didn't mention.

    Mask field names are the top-level Person keys
    (``names`` / ``emailAddresses`` / ``phoneNumbers`` / ``organizations``)
    as the People API expects, NOT the leaf sub-fields.
    """
    body: dict[str, Any] = {}
    fields: list[str] = []

    if given_name is not None or family_name is not None:
        name_obj: dict[str, str] = {}
        if given_name is not None:
            name_obj["givenName"] = given_name
        if family_name is not None:
            name_obj["familyName"] = family_name
        body["names"] = [name_obj]
        fields.append("names")

    if email is not None:
        body["emailAddresses"] = [{"value": email}]
        fields.append("emailAddresses")

    if phone is not None:
        body["phoneNumbers"] = [{"value": phone}]
        fields.append("phoneNumbers")

    if organization is not None or job_title is not None:
        org_obj: dict[str, str] = {}
        if organization is not None:
            org_obj["name"] = organization
        if job_title is not None:
            org_obj["title"] = job_title
        body["organizations"] = [org_obj]
        fields.append("organizations")

    return body, fields


def list_contacts(
    creds: Credentials,
    *,
    page_size: int = _LIST_PAGE_SIZE_DEFAULT,
    page_token: str | None = None,
    person_fields: str = DEFAULT_PERSON_FIELDS,
    sort_order: str | None = None,
) -> dict:
    """List the user's contacts via ``people.connections.list``.

    Always targets ``resourceName="people/me"`` (the only valid value).
    One page per call; pass the returned ``next_page_token`` back as
    ``page_token`` to walk the rest.

    Args:
        creds: OAuth credentials carrying the ``contacts`` scope.
        page_size: Contacts per page (clamped to 1-1000; People API
            default 100).
        page_token: Token from a prior call's ``next_page_token`` to
            fetch the next page. ``None`` (default) starts at the first.
        person_fields: Comma-separated People API field mask. Defaults to
            ``DEFAULT_PERSON_FIELDS``; ``metadata`` is always folded in so
            each contact's etag (needed for updates) comes back.
        sort_order: Optional ordering — one of
            ``LAST_MODIFIED_ASCENDING`` / ``LAST_MODIFIED_DESCENDING`` /
            ``FIRST_NAME_ASCENDING`` / ``LAST_NAME_ASCENDING``. ``None``
            uses the People API default (LAST_MODIFIED_ASCENDING).

    Returns:
        ``{contacts: [...], next_page_token, total_people}`` —
        ``contacts`` is the flattened ``_simplify_person`` list,
        ``next_page_token`` is ``None`` on the last page, ``total_people``
        is the People API's reported total contact count (``None`` if
        omitted).

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx — propagated to
            the tool-layer envelope.
    """
    people = get_service("people", "v1", credentials=creds)
    mask = _ensure_metadata(person_fields)
    size = _clamp(int(page_size), _LIST_PAGE_SIZE_MIN, _LIST_PAGE_SIZE_MAX)

    list_kwargs: dict[str, Any] = {
        "resourceName": "people/me",
        "personFields": mask,
        "pageSize": size,
    }
    if page_token:
        list_kwargs["pageToken"] = page_token
    if sort_order:
        list_kwargs["sortOrder"] = sort_order

    # connections.list is a pure read (idempotent) — safe to retry on a
    # transient 429/5xx.
    resp = execute_with_retry(
        lambda: people.people().connections().list(**list_kwargs).execute(),
        idempotent=True,
        op_name="people.connections.list",
    )
    connections = resp.get("connections", []) or []
    return {
        "contacts": [_simplify_person(p) for p in connections],
        "next_page_token": resp.get("nextPageToken"),
        "total_people": resp.get("totalPeople"),
    }


def search_contacts(
    creds: Credentials,
    query: str,
    *,
    page_size: int = _SEARCH_PAGE_SIZE_DEFAULT,
    read_mask: str = DEFAULT_SEARCH_READ_MASK,
) -> dict:
    """Search the user's contacts via ``people.searchContacts``.

    Prefix-matches ``query`` against the masked person fields. Per
    Google's documented requirement, a WARMUP request (empty query) is
    sent first to populate the server-side search cache — without it the
    first real search can return stale / empty results. The warmup is
    best-effort: its failure does not abort the real search.

    Args:
        creds: OAuth credentials carrying the ``contacts`` scope.
        query: Plain-text prefix query. Required + non-empty (an empty
            query is the warmup signal, not a user search).
        page_size: Results to return (clamped to 1-30; People API default
            10, max 30).
        read_mask: Comma-separated People API field mask for the returned
            persons. Defaults to ``DEFAULT_SEARCH_READ_MASK``.

    Returns:
        ``{contacts: [...], count}`` — ``contacts`` is the flattened
        ``_simplify_person`` list (each entry still carries resourceName
        + etag regardless of mask), ``count`` is its length. searchContacts
        does NOT paginate (no pageToken), so there is no next-page token.

    Raises:
        ValueError: empty / whitespace ``query`` (the warmup-vs-search
            distinction — reject an empty user query client-side).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not query or not query.strip():
        raise ValueError(
            "query cannot be empty — pass a prefix to match (e.g. a name "
            "or email fragment). To LIST all contacts use gcontacts_list."
        )

    people = get_service("people", "v1", credentials=creds)
    size = _clamp(int(page_size), _SEARCH_PAGE_SIZE_MIN, _SEARCH_PAGE_SIZE_MAX)

    # Warmup: Google's docs require sending an empty-query request first
    # to update the search cache. Best-effort — a failed warmup must not
    # block the real search, so we swallow its errors. NOT retried (it's
    # a cache-priming side request, not the user's actual query).
    try:
        people.people().searchContacts(query="", readMask=read_mask).execute()
    except Exception:  # noqa: BLE001 — warmup is advisory; never fatal
        pass

    # The real search is a pure read (idempotent) — safe to retry.
    resp = execute_with_retry(
        lambda: people.people().searchContacts(
            query=query.strip(),
            readMask=read_mask,
            pageSize=size,
        ).execute(),
        idempotent=True,
        op_name="people.searchContacts",
    )
    # searchContacts nests each hit under ``results[].person``.
    results = resp.get("results", []) or []
    contacts = [
        _simplify_person(r["person"])
        for r in results
        if isinstance(r, dict) and r.get("person")
    ]
    return {"contacts": contacts, "count": len(contacts)}


def get_contact(
    creds: Credentials,
    resource_name: str,
    *,
    person_fields: str = DEFAULT_PERSON_FIELDS,
) -> dict:
    """Fetch one contact via ``people.get``.

    Args:
        creds: OAuth credentials carrying the ``contacts`` scope.
        resource_name: The contact's resourceName (``people/c123...``) or
            a bare id — normalized to ``people/{id}``. From
            ``gcontacts_list`` / ``gcontacts_search`` / ``gcontacts_create``.
        person_fields: Comma-separated People API field mask. Defaults to
            ``DEFAULT_PERSON_FIELDS``; ``metadata`` is always folded in so
            the returned ``etag`` can drive ``gcontacts_update``.

    Returns:
        The flattened ``_simplify_person`` dict (``resource_name``,
        ``etag``, ``display_name``, ``emails``, ``phones``,
        ``organization``, ``raw``).

    Raises:
        ValueError: empty ``resource_name`` (from ``_normalize_resource_name``).
        HttpError: from the underlying SDK — propagated (e.g. 404 for an
            unknown / deleted contact).
    """
    rn = _normalize_resource_name(resource_name)
    people = get_service("people", "v1", credentials=creds)
    mask = _ensure_metadata(person_fields)

    resp = execute_with_retry(
        lambda: people.people().get(
            resourceName=rn,
            personFields=mask,
        ).execute(),
        idempotent=True,
        op_name="people.get",
    )
    return _simplify_person(resp)


def create_contact(
    creds: Credentials,
    *,
    given_name: str | None = None,
    family_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    person_fields: str = DEFAULT_PERSON_FIELDS,
) -> dict:
    """Create a new contact via ``people.createContact``.

    At least one of name / email / phone / organization must be supplied
    — an entirely empty contact is a caller bug (People API would create a
    blank, un-findable contact).

    Args:
        creds: OAuth credentials carrying the ``contacts`` scope.
        given_name / family_name: The contact's name parts. Either or both.
        email: A single email address.
        phone: A single phone number.
        organization: Company / organization name.
        job_title: Job title within the organization.
        person_fields: Field mask for the RETURNED contact (defaults to
            ``DEFAULT_PERSON_FIELDS``; ``metadata`` folded in for the etag).

    Returns:
        The flattened ``_simplify_person`` dict for the created contact
        (carries its new ``resource_name`` + ``etag``).

    Raises:
        ValueError: no contact data supplied at all.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        ``createContact`` is NOT idempotent — calling twice creates two
        distinct contacts (People keys contacts by server-assigned
        resourceName, not by content). The tool wrapper is annotated
        ``idempotent=False`` and the call is NOT wrapped in
        ``execute_with_retry`` — a transient retry after a request that
        actually landed would duplicate the contact (matches the
        create_spreadsheet / create_folder posture).
    """
    body, fields = _build_person_body(
        given_name=given_name,
        family_name=family_name,
        email=email,
        phone=phone,
        organization=organization,
        job_title=job_title,
    )
    if not fields:
        raise ValueError(
            "a contact needs at least one of: given_name, family_name, "
            "email, phone, organization, job_title. An empty contact "
            "would be created blank and un-findable."
        )

    people = get_service("people", "v1", credentials=creds)
    mask = _ensure_metadata(person_fields)
    # NOT idempotent — single attempt (matches create_spreadsheet /
    # create_folder). No execute_with_retry: replaying a landed create
    # spawns a duplicate contact.
    resp = people.people().createContact(
        body=body,
        personFields=mask,
    ).execute()
    return _simplify_person(resp)


def update_contact(
    creds: Credentials,
    resource_name: str,
    *,
    given_name: str | None = None,
    family_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    etag: str | None = None,
    person_fields: str = DEFAULT_PERSON_FIELDS,
) -> dict:
    """Update an existing contact via ``people.updateContact``.

    Read-modify-write with the People API's required etag (optimistic
    concurrency). When ``etag`` is not supplied, the current contact is
    fetched first to obtain it (one extra read); a supplied ``etag`` skips
    that fetch. A STALE etag (the contact changed since it was read)
    returns a 400 ``failedPrecondition`` from Google — surfaced verbatim
    so the caller knows to re-read and retry.

    ``updatePersonFields`` is derived from whichever of name / email /
    phone / organization the caller actually supplies, so the update
    overwrites ONLY those fields and never clobbers attributes left
    unspecified.

    Args:
        creds: OAuth credentials carrying the ``contacts`` scope.
        resource_name: The contact's resourceName (``people/c123...``) or
            a bare id — normalized to ``people/{id}``.
        given_name / family_name / email / phone / organization /
            job_title: New values for the fields to change. Supply only
            the ones to overwrite; at least one is required.
        etag: The contact's current etag (from a prior ``gcontacts_get`` /
            ``gcontacts_list``). ``None`` (default) triggers a fetch to
            obtain it — convenient but costs an extra read.
        person_fields: Field mask for the RETURNED updated contact.

    Returns:
        The flattened ``_simplify_person`` dict for the updated contact
        (carries the NEW post-update ``etag``).

    Raises:
        ValueError: empty ``resource_name``, or no field to update supplied.
        HttpError: from the underlying SDK — notably 400 ``failedPrecondition``
            on a stale etag, or 404 for an unknown contact. Propagated to
            the tool-layer envelope.

    Note:
        Annotated ``idempotent=True`` semantically — re-applying the same
        field values yields the same contact state. But the WRITE is NOT
        wrapped in ``execute_with_retry``: a blanket retry would re-send
        the update with a now-consumed etag (the first attempt rotated it)
        and 400 on the replay. The read-side etag fetch IS retried (pure
        read). This matches the "don't blanket-retry the mutating leg"
        safety floor used across the other services' writes.
    """
    rn = _normalize_resource_name(resource_name)
    body, fields = _build_person_body(
        given_name=given_name,
        family_name=family_name,
        email=email,
        phone=phone,
        organization=organization,
        job_title=job_title,
    )
    if not fields:
        raise ValueError(
            "no fields to update — supply at least one of: given_name, "
            "family_name, email, phone, organization, job_title."
        )

    people = get_service("people", "v1", credentials=creds)
    mask = _ensure_metadata(person_fields)

    # The People API requires the contact's etag in the update body. If
    # the caller didn't pass one, fetch it (pure read — retryable).
    if not etag:
        current = execute_with_retry(
            lambda: people.people().get(
                resourceName=rn,
                personFields="metadata",
            ).execute(),
            idempotent=True,
            op_name="people.get.for_update_etag",
        )
        etag = current.get("etag")
    body["etag"] = etag

    # The mutating leg is NOT retried — a retry would re-use a consumed
    # etag and 400 (see the Note). Let HttpError (incl. a stale-etag
    # failedPrecondition) propagate to the tool-layer envelope.
    resp = people.people().updateContact(
        resourceName=rn,
        updatePersonFields=",".join(fields),
        personFields=mask,
        body=body,
    ).execute()
    return _simplify_person(resp)


def delete_contact(creds: Credentials, resource_name: str) -> dict:
    """Delete a contact via ``people.deleteContact``.

    Args:
        creds: OAuth credentials carrying the ``contacts`` scope.
        resource_name: The contact's resourceName (``people/c123...``) or
            a bare id — normalized to ``people/{id}``.

    Returns:
        ``{resource_name, deleted: True}`` — ``resource_name`` echoes the
        normalized id that was removed.

    Raises:
        ValueError: empty ``resource_name``.
        HttpError: from the underlying SDK — e.g. 404 if the contact is
            already gone. Propagated to the tool-layer envelope.

    Note:
        ``deleteContact`` is DESTRUCTIVE (the contact is removed).
        Annotated ``idempotent=True`` semantically (deleting an
        already-deleted contact 404s rather than double-deleting), but the
        call is NOT wrapped in ``execute_with_retry`` — the destructive-op
        safety floor keeps the delete a single attempt.
    """
    rn = _normalize_resource_name(resource_name)
    people = get_service("people", "v1", credentials=creds)
    # Destructive — single attempt, not retried (safety floor).
    people.people().deleteContact(resourceName=rn).execute()
    return {"resource_name": rn, "deleted": True}
