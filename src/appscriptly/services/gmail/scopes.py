"""OAuth scopes for the Google Gmail service.

Two scopes, each backing a deliberately-narrow, CASA-FREE capability:

  * ``GMAIL_SEND_SCOPE`` — ``gmail.send``. A Google **SENSITIVE** scope
    (verification = brand/app review only), NOT one of Google's
    **RESTRICTED** scopes — it is absent from the closed restricted list
    (``mail.google.com`` / ``gmail.readonly`` / ``gmail.modify`` /
    ``gmail.metadata`` / ``gmail.insert`` / ``gmail.compose`` /
    ``gmail.settings.*``). So it triggers NO CASA third-party security
    assessment. Send-only: it grants the ability to send mail on the
    user's behalf, NOT to read the mailbox.

  * ``GMAIL_LABELS_SCOPE`` — ``gmail.labels``. A Google **NON-sensitive**
    scope (no verification, no CASA). It manages LABEL OBJECTS only
    (create / list / delete labels); it does NOT permit reading messages
    or relabeling a message (that needs ``gmail.modify``, which IS
    restricted and is intentionally NOT requested).

Both are part of the BASELINE consent set: they are declared in the
single source ``auth.WORKSPACE_SCOPES`` (the #187 derivation), which feeds
both ``auth.SCOPES`` (stdio) and ``oauth_google.GOOGLE_API_SCOPES``
(connector). They are therefore granted at first consent for every user —
the per-tool ``scopes=[...]`` on each ``@workspace_tool`` is redundant for
resolution (``_check_scopes_or_raise`` passes immediately) but kept for
explicit documentation + the machine-readable ``tool.annotations.scopes``
field, the same convention ``gas_deploy`` / ``tasks`` follow.

This module is named ``scopes`` so the auto-discovery walk skips it (the
discovery denylist excludes ``{api, scopes}``); it carries no
``@workspace_tool`` decorations.

DEPLOY NOTE: the Gmail API must be enabled in the GCP project before the
``gmail_*`` tools work live.
"""

# https://developers.google.com/workspace/gmail/api/auth/scopes
# gmail.send  — SENSITIVE (not restricted) — "Send email on your behalf."
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# gmail.labels — NON-sensitive — "See and edit your email labels."
# Label-OBJECT management only (create/list/delete); NOT message read,
# NOT message relabel (that would need the RESTRICTED gmail.modify).
GMAIL_LABELS_SCOPE = "https://www.googleapis.com/auth/gmail.labels"

# Convenience union for a tool (gmail_send_message uses only SEND; the
# label tools use only LABELS — kept separate so each tool declares the
# minimal scope it actually exercises).
GMAIL_SCOPES = [GMAIL_SEND_SCOPE, GMAIL_LABELS_SCOPE]
