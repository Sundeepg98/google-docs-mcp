"""OAuth scope for the Google Tasks service.

``TASKS_SCOPE`` is the single full read/write Tasks scope. It is a
**SENSITIVE** scope (Google verification = brand/app review only), NOT
one of Google's **RESTRICTED** scopes — it is absent from the closed
restricted-scope list (Gmail / Drive / Fit / Chat / Data Portability /
Photos / Health), so it triggers NO CASA third-party security
assessment. Adding it keeps the base tier free-publish eligible.

The scope is part of the BASELINE consent set: it is declared in the
single source ``auth.WORKSPACE_SCOPES`` (the #187 derivation), which
feeds both ``auth.SCOPES`` (stdio) and ``oauth_google.GOOGLE_API_SCOPES``
(connector). It is therefore granted at first consent for every user —
the per-tool ``scopes=[TASKS_SCOPE]`` on each ``@workspace_tool`` is
redundant for resolution (``_check_scopes_or_raise`` passes immediately)
but kept for explicit documentation + machine-readable annotation, the
same convention ``gas_deploy`` follows for ``GAS_DEPLOY_SCOPES`` after
its scopes were promoted to baseline.

This module is named ``scopes`` so the auto-discovery walk skips it (the
discovery denylist excludes ``{api, scopes}``); it carries no
``@workspace_tool`` decorations.
"""

# https://developers.google.com/tasks/reference/rest/v1/tasks
# SENSITIVE (not restricted) — see module docstring.
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
