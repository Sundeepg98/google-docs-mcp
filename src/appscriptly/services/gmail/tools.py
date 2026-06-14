"""Google Gmail MCP tool registrations (Gmail API v1 — new service).

Mirrors the layout established by ``services/contacts/tools.py`` and
``services/tasks/tools.py``: ``@workspace_tool``-decorated functions that
register with the live ``mcp`` instance when this module is imported.
``server.py``'s auto-discovery walk (``pkgutil.walk_packages`` over
``services/``) imports this leaf module as a side effect — no central
import edit needed.

**Tools registered here** (4 gmail-service tools):

1. ``gmail_send_message`` — send an email (the headline; gmail.send)
2. ``gmail_create_label`` — create a user label (gmail.labels)
3. ``gmail_list_labels``  — list system + user labels (gmail.labels)
4. ``gmail_delete_label`` — delete a user label (gmail.labels)

(Authoritative declaration: ``services/gmail/_expected_tools.py``.)

**Scopes — minimal per tool.** ``gmail_send_message`` declares
``scopes=[GMAIL_SEND_SCOPE]`` (SENSITIVE, not restricted → no CASA). The
three label tools declare ``scopes=[GMAIL_LABELS_SCOPE]`` (NON-sensitive).
Both scopes are baseline-granted via ``auth.WORKSPACE_SCOPES`` (the single
source), so the per-tool declaration is redundant for resolution
(``_check_scopes_or_raise`` passes immediately) but kept for explicit
documentation + the machine-readable ``tool.annotations.scopes`` field —
the same convention ``gas_deploy`` / ``tasks`` follow.

IMPORTANT capability boundary: the label tools manage LABEL OBJECTS only.
They do NOT read messages and do NOT relabel a message — applying or
removing a label on a message requires the RESTRICTED ``gmail.modify``
scope, which is deliberately NOT requested (it would trigger CASA). See
``services/gmail/scopes.py``.

**Import discipline.** Same as ``services/contacts/tools.py``:
- the api module is imported via the standard ``from ... import`` aliases;
- ``@workspace_tool(service="gmail", ...)`` carries the service= literal
  that drives the partition test + telemetry.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError

from appscriptly.decorators import workspace_tool
from appscriptly.services.gmail.api import (
    create_label as _create_label,
    delete_label as _delete_label,
    list_labels as _list_labels,
    send_message as _send_message,
)
from appscriptly.services.gmail.scopes import GMAIL_LABELS_SCOPE, GMAIL_SEND_SCOPE
from appscriptly.tool_schemas import (
    GMAIL_CREATE_LABEL_OUTPUT_SCHEMA,
    GMAIL_DELETE_LABEL_OUTPUT_SCHEMA,
    GMAIL_LIST_LABELS_OUTPUT_SCHEMA,
    GMAIL_SEND_MESSAGE_OUTPUT_SCHEMA,
)


# ---------------------------------------------------------------------
# 1. gmail_send_message — users.messages.send (the headline)
# ---------------------------------------------------------------------


@workspace_tool(
    service="gmail",
    title="Send an email via Gmail",
    readonly=False,
    destructive=False,
    # Each call delivers a NEW message (Gmail assigns a fresh id; no
    # content de-dup), so re-running sends another email. NOT idempotent —
    # same convention as the create_* tools. The api layer does NOT
    # retry the send accordingly.
    idempotent=False,
    external=True,
    creds=True,
    scopes=[GMAIL_SEND_SCOPE],
    output_schema=GMAIL_SEND_MESSAGE_OUTPUT_SCHEMA,
)
def gmail_send_message(
    creds,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False,
) -> dict:
    """Send an email on the user's behalf via Gmail.

    USE WHEN: the user wants to send a message — "email Jane the summary",
    "send this to the team", "reply to bob@x.com with ...". This is the
    Gmail SEND tool.

    Backed by the Gmail API ``users.messages.send`` (the message is built
    as RFC822/MIME and sent as the authenticated user). Uses the
    ``gmail.send`` scope — a Google SENSITIVE scope, NOT restricted (no
    CASA). This tool can SEND but cannot READ the mailbox; there is no
    inbox-read tool (that would need a restricted scope).

    Args:
        to: recipient address(es). Comma-separate for multiple
            (``"a@x.com, b@y.com"``). Required.
        subject: the Subject line. Required (pass a single space for a
            deliberately blank subject).
        body: the message body. Required. Plain text unless ``html=True``.
        cc: optional carbon-copy address(es) (comma-separated).
        bcc: optional blind-carbon-copy address(es) (comma-separated).
        html: when True, ``body`` is sent as HTML (``text/html``);
            otherwise as plain text. Default False.

    Returns:
        ``{id, thread_id, label_ids}`` — ``id`` is the sent message's id,
        ``thread_id`` the conversation, ``label_ids`` the labels Gmail
        assigned (usually ``["SENT"]``).

    NOTE: NOT idempotent — calling twice sends TWO emails. There is no
    undo. Confirm recipient + content before sending.
    """
    try:
        return _send_message(
            creds,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 2. gmail_create_label — users.labels.create
# ---------------------------------------------------------------------


@workspace_tool(
    service="gmail",
    title="Create a Gmail label",
    readonly=False,
    destructive=False,
    # Re-running with the same name 409s (Gmail enforces name uniqueness)
    # rather than creating a second label. NOT idempotent — matches the
    # create_* convention; the api layer does not retry.
    idempotent=False,
    external=True,
    creds=True,
    scopes=[GMAIL_LABELS_SCOPE],
    output_schema=GMAIL_CREATE_LABEL_OUTPUT_SCHEMA,
)
def gmail_create_label(
    creds,
    name: str,
    message_list_visibility: str | None = None,
    label_list_visibility: str | None = None,
) -> dict:
    """Create a new Gmail label (a label OBJECT — not a message change).

    USE WHEN: the user wants a new label / folder to organize mail —
    "make a label called Invoices", "create a Clients/Acme label".

    Backed by the Gmail API ``users.labels.create`` (``gmail.labels``
    scope — NON-sensitive, no CASA). This manages the LABEL itself; it
    does NOT apply the label to any message (relabeling a message needs
    the restricted ``gmail.modify`` scope, which this app does not
    request).

    Args:
        name: the label name. Use ``/`` for nesting (``"Clients/Acme"``).
            Required and must be unique (a duplicate name errors).
        message_list_visibility: ``"show"`` or ``"hide"`` — whether
            messages with this label appear in the message list. Optional
            (Gmail default ``"show"``).
        label_list_visibility: ``"labelShow"`` / ``"labelShowIfUnread"`` /
            ``"labelHide"`` — where the label shows in the sidebar.
            Optional (Gmail default ``"labelShow"``).

    Returns:
        ``{id, name, type, message_list_visibility, label_list_visibility}``
        for the created label. ``type`` is ``"user"``; ``id`` is the handle
        for ``gmail_delete_label``.

    Choreography: list existing labels first with ``gmail_list_labels`` to
    avoid a duplicate-name error. The returned ``id`` feeds
    ``gmail_delete_label``.
    """
    try:
        return _create_label(
            creds,
            name,
            message_list_visibility=message_list_visibility,
            label_list_visibility=label_list_visibility,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 3. gmail_list_labels — users.labels.list (pure read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="gmail",
    title="List Gmail labels",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[GMAIL_LABELS_SCOPE],
    output_schema=GMAIL_LIST_LABELS_OUTPUT_SCHEMA,
)
def gmail_list_labels(creds) -> dict:
    """List the mailbox's Gmail labels (system + user).

    USE WHEN: the agent needs to see what labels exist — to summarize, to
    find a label's id for ``gmail_delete_label``, or to avoid a
    duplicate-name error before ``gmail_create_label``.

    Backed by the Gmail API ``users.labels.list`` (``gmail.labels`` scope
    — NON-sensitive, no CASA). Returns BOTH Gmail's system labels (INBOX,
    SENT, SPAM, TRASH, etc.) and the user's own labels. This reads label
    OBJECTS only — it does NOT read any message content.

    Returns:
        ``{labels, count}`` — ``labels`` is a list of
        ``{id, name, type, message_list_visibility, label_list_visibility}``
        dicts; ``count`` is its length. ``type`` is ``"system"`` or
        ``"user"`` (only ``user`` labels are deletable).

    Choreography: a returned ``user``-type ``id`` feeds
    ``gmail_delete_label``; the name set helps ``gmail_create_label`` skip
    duplicates.
    """
    # No ValueError path (no args) — let HttpError flow to the envelope.
    return _list_labels(creds)


# ---------------------------------------------------------------------
# 4. gmail_delete_label — users.labels.delete (destructive)
# ---------------------------------------------------------------------


@workspace_tool(
    service="gmail",
    title="Delete a Gmail label",
    readonly=False,
    # Removing a label deletes the label object — genuinely destructive
    # (matches gcontacts_delete / gtasks_delete_task). Messages that
    # carried it keep existing but lose the label.
    destructive=True,
    # Deleting an already-gone label 404s rather than double-deleting, so
    # the OUTCOME is idempotent in intent; annotated True to match the
    # other delete tools. The api layer dispatches non-retried (the
    # destructive-op safety floor).
    idempotent=True,
    external=True,
    creds=True,
    scopes=[GMAIL_LABELS_SCOPE],
    output_schema=GMAIL_DELETE_LABEL_OUTPUT_SCHEMA,
)
def gmail_delete_label(creds, label_id: str) -> dict:
    """Delete a Gmail user label by its id.

    USE WHEN: removing a label the user no longer wants — "delete the
    Invoices label". DESTRUCTIVE: the label object is removed (messages
    that had it keep existing but lose the label). Only USER labels can be
    deleted — Gmail rejects deleting system labels (INBOX / SENT / etc.).

    Backed by the Gmail API ``users.labels.delete`` (``gmail.labels``
    scope — NON-sensitive, no CASA). Removing a label does NOT delete the
    messages that carried it.

    Args:
        label_id: the label's id (from ``gmail_list_labels`` /
            ``gmail_create_label``). Must be a USER label. Required.

    Returns:
        ``{label_id, deleted: True}`` — echoes the removed id
        (``users.labels.delete`` returns an empty body).

    Choreography: get the ``id`` from ``gmail_list_labels`` first (filter
    to ``type == "user"`` — system labels can't be deleted).
    """
    try:
        return _delete_label(creds, label_id)
    except ValueError as e:
        raise ToolError(str(e)) from e
