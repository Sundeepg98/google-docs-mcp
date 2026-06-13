"""Google Forms service (new service — sensitive scopes, no CASA).

Mirrors the per-service folder layout established by the earlier new
services:

  * v2.3.1 (PR #119) — services/sheets/  (1st full new-service template)
  * v2.3.2 (PR #120) — services/slides/  (2nd, reproduced the template)
  * this PR          — services/forms/   ← here

Layout:

    services/forms/
    ├── __init__.py      — this file
    ├── _expected_tools.py — declared tool surface (decentralized witness)
    ├── api.py           — Forms REST wrapper (create/get/batchUpdate +
    │                       responses list/get)
    └── tools.py         — @workspace_tool decorators (registered via
                            server.py's auto-discovery import)

**Scope expansion (operator-directed).** This service adds TWO
Google-SENSITIVE scopes (NOT restricted → no CASA) to the single-source
``auth.WORKSPACE_SCOPES``:

  * ``https://www.googleapis.com/auth/forms.body`` — create / edit forms.
  * ``https://www.googleapis.com/auth/forms.responses.readonly`` — read
    responses.

Both are SENSITIVE (require app verification) but NOT RESTRICTED (no CASA
security assessment — only restricted scopes trigger CASA). Adding them
to ``WORKSPACE_SCOPES`` updates BOTH consent sets (stdio ``auth.SCOPES``
and the HTTP connector ``oauth_google.GOOGLE_API_SCOPES``) by derivation;
the scope-union + base-tier scope tests pin the new exact sets.

Pairs with the form-submit reactive trigger
(``as_install_form_handler``, ROADMAP_SPECS #8): that tool runs a bound
Apps Script on submission; this service builds and reads the form itself
through the Forms REST API.

NOT auto-imported here — ``tools.py`` is discovered + imported by
``server.py`` AFTER the ``mcp`` instance is constructed (same
side-effect-registration discipline as every other service; importing it
here would risk a circular import).
"""
