"""Google Contacts service (services/contacts/) — People API v1.

The 5th new Google service after the per-service-folder pattern was
proven by:

  * Phase A (PR #94)        — services/docs/
  * Phase B (PR #96)        — services/drive/{api,tools}
  * Phase C (PR #109)       — services/gas_deploy/
  * Gap #7 (PR #113)        — services/admin/
  * v2.3.0 (PR #117)        — services/drive/sharing.py (1st bolt-on)
  * v2.3.1 (PR #119)        — services/sheets/
  * v2.3.2 (PR #...)        — services/slides/
  * this PR                 — services/contacts/            ← here

Layout (mirrors services/sheets/ exactly):

    services/contacts/
    ├── __init__.py        — this file
    ├── api.py             — People API v1 REST wrapper (connections.list /
    │                        otherContacts.list / searchContacts / get /
    │                        createContact / updateContact / deleteContact)
    ├── tools.py           — @workspace_tool decorators (registered via
    │                        server.py's auto-discovery walk)
    └── _expected_tools.py — declared tool surface (decentralized witness)

**CASA-free growth.** A 7th tool ``gcontacts_list_other_contacts``
(People API ``otherContacts.list``) was added using the dedicated
read-only scope ``contacts.other.readonly`` (SENSITIVE, not restricted →
no CASA) — it reads the auto-saved "other contacts" collection, distinct
from the main address book the other six tools operate on.

**The People API ("people" v1).** Unlike the Docs/Sheets/Slides/Drive
services (which target a single Drive-resident document by ID), Contacts
operates on the authenticated user's personal address book —
``resourceName="people/me"`` for the connections list, ``people/{id}``
for an individual contact. Every read takes a ``personFields`` /
``readMask`` field mask (the People API has no "return everything"
default for reads); every write takes an ``updatePersonFields`` mask
naming exactly which person attributes to mutate.

**Scope — ``https://www.googleapis.com/auth/contacts`` (SENSITIVE, not
restricted).** The FULL read/write contacts scope is required because
``gcontacts_create`` / ``gcontacts_update`` / ``gcontacts_delete``
mutate the address book (the narrower ``contacts.readonly`` would only
serve the three read tools). Google classifies ``contacts`` as a
SENSITIVE scope, NOT a RESTRICTED one — so adding it needs
sensitive-scope OAuth verification but does NOT trigger a CASA security
assessment (CASA is gated on the RESTRICTED scopes — full Gmail/Drive,
etc.). This preserves the project's "sensitive scopes only, no CASA"
verification posture; the scope lives in the single-source
``auth.WORKSPACE_SCOPES`` list (see its comment block).

**Optimistic concurrency on update.** ``people.updateContact`` requires
the contact's ``etag`` in the request body. A stale etag (the contact
changed since it was read) returns a 400 ``failedPrecondition`` — so
``gcontacts_update`` is a read-modify-write: fetch the contact (which
yields its current etag), apply the requested field changes, send it
back with that etag. ``gcontacts_get`` surfaces the etag so a caller can
also drive the cycle explicitly.
"""
