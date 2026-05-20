"""Live multi-tenant correctness: docs are owned by the authorizing user.

Per v1.1's multi-tenant architecture: each cloud-chat user's tool
calls run against THEIR OWN Google identity (via per-user OAuth in
``credentials.get_credentials_for_user``), so docs they create land
in THEIR Drive, not the operator's. This test asserts that property
end-to-end: create a doc with the loaded creds, fetch its Drive
metadata, and verify the owner email matches the authenticated
user's email.

Pre-v1.1, all cloud-chat-created docs landed in the operator's Drive
(the operator was the single shared identity). v1.1's
configure_auth_for_http() + per-user credentials resolver fixed
this; this test is the smoke check that the property holds.

Limitation: locally this runs as the operator's own OAuth token, so
"the authorizing user" == "the operator". Doesn't prove cross-user
ISOLATION — that would need a second account, which is out of scope.
What it DOES prove: ownership flows from the credentials handed to
the API call, not from some hardcoded app/service-account identity.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_doc_is_owned_by_authorizing_account(live_creds, created_docs):
    """A doc created via the loaded creds is owned by THAT user's email,
    not by any app service account or hardcoded operator.

    Uses drive.about().get() (not oauth2.userinfo) to identify the
    authenticated user — the former works on Drive scope alone, the
    latter requires userinfo.email which our runtime SCOPES don't
    include by default.
    """
    from google_docs_mcp.docs_api import make_doc_with_tabs
    from google_docs_mcp.google_clients import get_service

    drive = get_service("drive", "v3", credentials=live_creds)

    # Step 1: identify who the loaded creds belong to via Drive's
    # own "about" endpoint (no extra scopes needed).
    about = drive.about().get(fields="user(emailAddress,displayName)").execute()
    my_email = (about.get("user") or {}).get("emailAddress")
    assert my_email, f"drive.about returned no user.emailAddress: {about!r}"

    # Step 2: create a doc with the same creds.
    created = make_doc_with_tabs(
        live_creds, "test_multi_tenant_ownership",
        [{"title": "x", "content": "x"}],
    )
    doc_id = created["doc_id"]
    created_docs.append(doc_id)

    # Step 3: fetch the doc's Drive metadata and confirm ownership.
    meta = drive.files().get(
        fileId=doc_id, fields="id,name,owners(emailAddress,displayName)",
    ).execute()

    owners = meta.get("owners") or []
    owner_emails = [o.get("emailAddress") for o in owners]

    assert my_email in owner_emails, (
        f"Doc {doc_id} is NOT owned by the authorizing account.\n"
        f"  Authorizing user (from drive.about):  {my_email}\n"
        f"  Doc owners (from drive.files.get):   {owner_emails}\n"
        "This is the v1.1 multi-tenancy guarantee: docs created via "
        "a user's creds belong to THAT user, not to a shared operator "
        "or service account. If this fails, the per-user creds "
        "resolver in credentials.py is being bypassed somewhere."
    )

    # No app-bot or service-account owner masquerading.
    for email in owner_emails:
        assert not email.endswith(".gserviceaccount.com"), (
            f"Doc has a service account owner ({email}). The cloud MCP "
            "should never create docs under a service-account identity — "
            "user OAuth creds are the only path."
        )
