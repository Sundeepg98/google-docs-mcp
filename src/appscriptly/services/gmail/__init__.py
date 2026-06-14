"""Google Gmail service (services/gmail/) — Gmail API v1 (send + labels).

A new Google service added by the CASA-free scope-growth PR. It follows
the per-service-folder pattern proven by the earlier services (docs /
drive / sheets / slides / forms / calendar / contacts / tasks).

Layout (mirrors services/contacts/ + services/tasks/):

    services/gmail/
    ├── __init__.py        — this file (re-exports the two scope constants)
    ├── scopes.py          — GMAIL_SEND_SCOPE + GMAIL_LABELS_SCOPE
    ├── api.py             — Gmail API v1 REST wrapper (users.messages.send
    │                        via RFC822/MIME; users.labels create/list/delete)
    ├── tools.py           — @workspace_tool decorators (registered via
    │                        server.py's auto-discovery walk)
    └── _expected_tools.py — declared tool surface (decentralized witness)

**Two scopes, two deliberately-narrow capabilities.**

  * ``https://www.googleapis.com/auth/gmail.send`` (SENSITIVE, NOT
    restricted → no CASA). Google's classification text: "Send email on
    your behalf." This is SEND-ONLY — it does NOT grant any mailbox READ.
    The full-mailbox / read / modify Gmail scopes (``mail.google.com``,
    ``gmail.readonly``, ``gmail.modify``, ``gmail.metadata``,
    ``gmail.insert``, ``gmail.compose``, ``gmail.settings.*``) ARE
    restricted and are deliberately NOT requested — keeping the project's
    "sensitive scopes only, no CASA" verification posture. Backs
    ``gmail_send_message`` (``users.messages.send`` with an RFC822/MIME
    payload).

  * ``https://www.googleapis.com/auth/gmail.labels`` (NON-sensitive — no
    verification or CASA at all). Google's classification text: "See and
    edit your email labels." IMPORTANT — this scope manages LABEL OBJECTS
    only (``users.labels.create`` / ``.list`` / ``.delete``). It does NOT
    permit reading message bodies, and it does NOT permit changing a
    message's labels (applying/removing a label to a message needs
    ``gmail.modify``, which is RESTRICTED and intentionally omitted). The
    label tools here are therefore strictly label-management-only. Backs
    ``gmail_create_label`` / ``gmail_list_labels`` / ``gmail_delete_label``.

Both scopes live in the single-source ``auth.WORKSPACE_SCOPES`` list (see
its comment block) and are therefore baseline-granted on first consent.

**Why no "send a reply in a thread" / "read my inbox" tool?** Those would
need ``gmail.readonly`` / ``gmail.modify`` (RESTRICTED → CASA). The send
+ label-management surface is the maximal CASA-FREE Gmail surface; the
restricted surface is out of scope for this verification track.

Re-export discipline: like ``gas_deploy/__init__.py`` /
``apps_script/__init__.py``, this re-exports the scope constants so callers
can ``from appscriptly.services.gmail import GMAIL_SEND_SCOPE``. Tool
REGISTRATION is NOT triggered here — it happens via server.py's
auto-discovery walk importing ``services.gmail.tools`` (the leaf name
doesn't start with ``_`` and isn't in the ``{api, scopes}`` denylist).
Importing this package does not import ``tools`` (so it does not reach
into the live ``mcp`` instance), keeping the import graph acyclic.
"""
from .scopes import GMAIL_LABELS_SCOPE, GMAIL_SEND_SCOPE

__all__ = ["GMAIL_LABELS_SCOPE", "GMAIL_SEND_SCOPE"]
