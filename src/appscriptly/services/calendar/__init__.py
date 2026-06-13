"""Google Calendar service (4th new service after Sheets + Slides).

Mirrors the per-service folder layout proven by:

  * Phase A (PR #94)        — services/docs/
  * Phase B (PR #96)        — services/drive/{api,tools}
  * Phase C (PR #109)       — services/gas_deploy/
  * Gap #7 (PR #113)        — services/admin/ (closes ISP asymmetry)
  * v2.3.0 (PR #117)        — services/drive/sharing.py (1st bolt-on)
  * v2.3.1 (PR #119)        — services/sheets/   (1st new-service proof)
  * v2.3.2                  — services/slides/   (2nd new-service proof)
  * v2.4.0 (this PR)        — services/calendar/                ← here

Layout:

    services/calendar/
    ├── __init__.py        — this file
    ├── _expected_tools.py — the decentralized tool-surface witness
    ├── api.py             — Calendar v3 REST wrapper (events / freebusy)
    └── tools.py           — @workspace_tool decorators (registered via
                              server.py's auto-discovery import walk)

**Scope — SENSITIVE, not restricted (no CASA).** This service requests
``https://www.googleapis.com/auth/calendar`` (read/write events and
calendar metadata). Per Google's OAuth scope classification, the full
``/auth/calendar`` scope is a **SENSITIVE** scope, NOT a RESTRICTED one
— it does NOT trigger the CASA (Cloud Application Security Assessment)
third-party security review that restricted scopes (gmail.*,
drive[full]/.readonly, etc.) require. Adding it keeps verification on
the same "sensitive scopes only" track as the rest of this MCP. None of
the tools here need a restricted scope.

The scope is added ONCE to ``auth.WORKSPACE_SCOPES`` (the single source
of truth post-#187), so it flows automatically into both ``auth.SCOPES``
(stdio/baseline) and ``oauth_google.GOOGLE_API_SCOPES`` (HTTP/connector)
— no twin-list drift. Existing user grants pick it up on next token
refresh via the ``include_granted_scopes=true`` incremental-consent flow
(same pattern that absorbed the Sheets / Slides scope additions); no
forced re-consent.

**Tool surface.** Ergonomic event + availability operations over the
Calendar API v3 — list / get / create / update / delete events on a
calendar (``calendarId`` defaults to ``"primary"``), list the user's
calendars, and a free/busy availability query. The granularity matches
the existing services (a small, validated, tagged set; the long tail of
Calendar's surface — ACLs, recurrence exceptions, settings, watch
channels — is DEFERRED to a follow-up when concrete need emerges).

By landing this service we get empirical evidence that the
``get_service`` chokepoint + ``@workspace_tool`` decorator + per-service
witnesses absorb a 4th Google service (``("calendar", "v3")``) without
infra change — the same proof Sheets and Slides already established.
"""
