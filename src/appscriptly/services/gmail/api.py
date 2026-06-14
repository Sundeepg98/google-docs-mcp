"""Google Gmail API v1 wrapper — send mail + manage labels (CASA-free).

The Gmail surface this MCP exposes, mapped to Gmail API v1 methods:

  * ``send_message``  — ``users.messages.send``. Builds an RFC822/MIME
    message (via Python's stdlib ``email`` module), base64url-encodes it,
    and sends it as the authenticated user. Supports plain-text or HTML
    bodies plus optional cc / bcc.
  * ``create_label``  — ``users.labels.create`` (a new user label object).
  * ``list_labels``   — ``users.labels.list`` (system + user labels).
  * ``delete_label``  — ``users.labels.delete`` (remove a user label).

**Scopes — two, each the minimal CASA-FREE scope for its capability.**

  * Sending requires ``https://www.googleapis.com/auth/gmail.send`` — a
    Google SENSITIVE scope, NOT restricted (no CASA). It is SEND-ONLY: it
    grants no mailbox READ. The restricted Gmail scopes
    (``mail.google.com`` / ``gmail.readonly`` / ``gmail.modify`` / etc.)
    are deliberately NOT requested.
  * Label management requires ``https://www.googleapis.com/auth/gmail.labels``
    — a Google NON-sensitive scope (no verification, no CASA). It manages
    LABEL OBJECTS only. It does NOT permit reading message bodies, and it
    does NOT permit applying/removing a label on a message (that is a
    message *modify*, which needs the RESTRICTED ``gmail.modify`` — out of
    scope here). The label tools are therefore label-management-only.

See ``services/gmail/__init__.py`` + the ``auth.WORKSPACE_SCOPES`` comment
for the verification posture.

**The authenticated user is always ``"me"``.** Gmail API uses the special
``userId="me"`` alias for the OAuth-authenticated mailbox; there is no
cross-user access on these scopes.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Gmail API addresses the authenticated mailbox via this alias — there is
# no other valid userId on the gmail.send / gmail.labels scopes.
_ME = "me"


def _require(value: str | None, field: str) -> str:
    """Return a stripped non-empty ``value`` or raise ValueError.

    Cheap client-side rejection so an empty required field fails with a
    clear message rather than a less-clear Gmail 400.
    """
    if not value or not value.strip():
        raise ValueError(
            f"{field} cannot be empty — it is required to send a message."
        )
    return value.strip()


def _build_mime_message(
    *,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False,
) -> dict[str, str]:
    """Build the base64url-encoded RFC822 payload Gmail's send API expects.

    ``users.messages.send`` takes ``{"raw": <base64url(RFC822 bytes)>}``.
    We assemble the message with the stdlib ``email`` module (correct
    header encoding, MIME structure) rather than hand-formatting strings —
    that handles non-ASCII subjects/bodies and multi-recipient headers
    correctly.

    Args:
        to: recipient address(es). Comma-separated for multiple.
        subject: the Subject header.
        body: the message body text.
        cc / bcc: optional carbon-copy / blind-carbon-copy address(es)
            (comma-separated for multiple). ``bcc`` is set as a header;
            Gmail strips it from the delivered message but honors it for
            delivery.
        html: when True, the body is sent as ``text/html``; otherwise
            ``text/plain``.

    Returns:
        ``{"raw": "<base64url string>"}`` — the request body for
        ``users.messages.send``.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc

    if html:
        # Provide a minimal plain-text fallback then the HTML alternative
        # so the message is well-formed multipart/alternative.
        msg.set_content("This message requires an HTML-capable mail client.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    # Gmail wants base64URL (RFC 4648 §5), not standard base64.
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return {"raw": raw}


def send_message(
    creds: Credentials,
    *,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False,
) -> dict:
    """Send an email as the authenticated user via ``users.messages.send``.

    Args:
        creds: OAuth credentials carrying the ``gmail.send`` scope.
        to: recipient address(es) — comma-separated for multiple. Required.
        subject: the Subject line. Required (an empty subject is rejected
            client-side; pass a single space if a truly blank subject is
            intended).
        body: the message body. Required.
        cc / bcc: optional carbon-copy / blind-carbon-copy address(es).
        html: send the body as HTML (``text/html``) when True; plain text
            otherwise.

    Returns:
        ``{id, thread_id, label_ids}`` — ``id`` is the sent message's id,
        ``thread_id`` the conversation it belongs to, ``label_ids`` the
        labels Gmail assigned (typically ``["SENT"]``).

    Raises:
        ValueError: a required field (to / subject / body) is empty.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated to the
            tool-layer envelope.

    Note:
        Sending is NOT idempotent — each call delivers a NEW message
        (Gmail assigns a fresh message id every time; there is no
        content-based de-dup). The call is therefore NOT wrapped in
        ``execute_with_retry`` — a transient retry after a send that
        actually landed would deliver a duplicate email (same posture as
        the create_* tools across the other services).
    """
    to = _require(to, "to")
    subject = _require(subject, "subject")
    body = _require(body, "body")

    raw_body = _build_mime_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        html=html,
    )

    gmail = get_service("gmail", "v1", credentials=creds)
    # NOT idempotent — single attempt (a retry would double-send). No
    # execute_with_retry here, matching create_contact / create_spreadsheet.
    resp = gmail.users().messages().send(userId=_ME, body=raw_body).execute()
    return {
        "id": resp.get("id", ""),
        "thread_id": resp.get("threadId"),
        "label_ids": resp.get("labelIds", []) or [],
    }


def _simplify_label(label: dict) -> dict:
    """Flatten a Gmail ``Label`` resource into a compact dict.

    The raw label carries counts + color objects + visibility enums; for
    an MCP tool the load-bearing fields are the id (the handle the delete
    tool consumes), the name, and the type (``system`` vs ``user`` — only
    ``user`` labels are deletable).
    """
    return {
        "id": label.get("id", ""),
        "name": label.get("name"),
        "type": label.get("type"),
        "message_list_visibility": label.get("messageListVisibility"),
        "label_list_visibility": label.get("labelListVisibility"),
    }


def create_label(
    creds: Credentials,
    name: str,
    *,
    message_list_visibility: str | None = None,
    label_list_visibility: str | None = None,
) -> dict:
    """Create a new user label via ``users.labels.create``.

    Args:
        creds: OAuth credentials carrying the ``gmail.labels`` scope.
        name: the label name (e.g. ``"Invoices"`` or a nested
            ``"Clients/Acme"`` using ``/`` for hierarchy). Required.
        message_list_visibility: ``"show"`` or ``"hide"`` — whether
            messages with this label show in the message list. Optional
            (Gmail default ``"show"``).
        label_list_visibility: ``"labelShow"`` / ``"labelShowIfUnread"`` /
            ``"labelHide"`` — where the label appears in the label list.
            Optional (Gmail default ``"labelShow"``).

    Returns:
        The flattened ``{id, name, type, message_list_visibility,
        label_list_visibility}`` dict for the created label. ``id`` is the
        handle ``delete_label`` consumes.

    Raises:
        ValueError: empty ``name``.
        HttpError: from the underlying SDK — e.g. 409 if a label with that
            name already exists. Propagated to the tool-layer envelope.

    Note:
        NOT idempotent — creating the same name twice returns a 409
        (Gmail enforces name uniqueness) rather than a second label. The
        call is NOT wrapped in ``execute_with_retry`` (a retry of a landed
        create would surface the 409 on the replay). This matches the
        create_* posture elsewhere.
    """
    label_name = _require(name, "name")
    body: dict[str, Any] = {"name": label_name}
    if message_list_visibility is not None:
        body["messageListVisibility"] = message_list_visibility
    if label_list_visibility is not None:
        body["labelListVisibility"] = label_list_visibility

    gmail = get_service("gmail", "v1", credentials=creds)
    # NOT idempotent — single attempt (a retry of a landed create 409s).
    resp = gmail.users().labels().create(userId=_ME, body=body).execute()
    return _simplify_label(resp)


def list_labels(creds: Credentials) -> dict:
    """List the mailbox's labels via ``users.labels.list``.

    Returns BOTH system labels (INBOX, SENT, SPAM, TRASH, etc.) and the
    user's own labels. ``users.labels.list`` is not paginated — it returns
    the full set in one call.

    Args:
        creds: OAuth credentials carrying the ``gmail.labels`` scope.

    Returns:
        ``{labels, count}`` — ``labels`` is a list of the flattened
        ``{id, name, type, ...}`` dicts; ``count`` is its length. Each
        entry's ``type`` is ``"system"`` or ``"user"`` (only ``user``
        labels can be deleted); each ``id`` feeds ``delete_label``.

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    gmail = get_service("gmail", "v1", credentials=creds)
    # Pure read (idempotent) — safe to retry on a transient 429/5xx.
    resp = execute_with_retry(
        lambda: gmail.users().labels().list(userId=_ME).execute(),
        idempotent=True,
        op_name="gmail.users.labels.list",
    )
    labels = resp.get("labels", []) or []
    simplified = [_simplify_label(label) for label in labels]
    return {"labels": simplified, "count": len(simplified)}


def delete_label(creds: Credentials, label_id: str) -> dict:
    """Delete a user label via ``users.labels.delete``.

    Args:
        creds: OAuth credentials carrying the ``gmail.labels`` scope.
        label_id: the label's id (from ``list_labels`` / ``create_label``).
            Must be a USER label — Gmail rejects deletion of system labels
            (INBOX / SENT / etc.) with a 400. Required.

    Returns:
        ``{label_id, deleted: True}`` — ``label_id`` echoes the removed id
        (``users.labels.delete`` returns an empty body).

    Raises:
        ValueError: empty ``label_id``.
        HttpError: from the underlying SDK — e.g. 400 when targeting a
            system label, 404 for an unknown id. Propagated.

    Note:
        DESTRUCTIVE (the label is removed; messages keep existing but lose
        the label). NOT wrapped in ``execute_with_retry`` — the
        destructive-op safety floor keeps the delete a single attempt.
        Removing a label does NOT delete the messages that carried it.
    """
    lid = _require(label_id, "label_id")
    gmail = get_service("gmail", "v1", credentials=creds)
    # Destructive — single attempt, not retried (safety floor).
    gmail.users().labels().delete(userId=_ME, id=lid).execute()
    return {"label_id": lid, "deleted": True}
