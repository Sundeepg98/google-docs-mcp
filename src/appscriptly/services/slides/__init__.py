"""Google Slides service (v2.3.2 — 3rd new service after Drive sharing + Sheets).

Mirrors the per-service folder layout established by:

  * Phase A (PR #94)        — services/docs/
  * Phase B (PR #96)        — services/drive/{api,tools}
  * Phase C (PR #109)       — services/gas_deploy/
  * Gap #7 (PR #113)        — services/admin/ (closes ISP asymmetry)
  * v2.3.0 (PR #117)        — services/drive/sharing.py (1st bolt-on)
  * v2.3.1 (PR #119)        — services/sheets/ (2nd new service)
  * v2.3.2 (this PR)        — services/slides/                ← here

Layout:

    services/slides/
    ├── __init__.py    — this file
    ├── api.py         — Slides REST wrapper (get_outline /
    │                     replace_all_text)
    └── tools.py       — @workspace_tool decorators (registered via
                          server.py's side-effect import)

**Minimal start** — same approach the Sheets PR took. Per the
multi-service feasibility audit (R33 agent ``a2d2492bbebb200a6``):

    "Slides → clean bolt-on. Page-based with objectId tracking is
    structurally similar to docs's tab-id tracking."

This PR ships outline-read + cross-slide find/replace. The Slides
``batchUpdate`` tagged-union (~40 request types — addSlide,
replaceImage, updateTextStyle, etc.) is DELIBERATELY DEFERRED to a
follow-up PR when actual usage drives the abstraction shape. Same
strategy as Sheets v2.3.1.

By landing the minimal shape we get a THIRD empirical pass on the
foundation:

1. PR #117 — Drive sharing — proved the sub-module bolt-on
   (services/drive/sharing.py alongside api.py).
2. PR #119 — Sheets — proved a whole NEW service folder (different
   Google API, different ``get_service("sheets","v4")`` key,
   different OAuth scope).
3. PR #120 — Slides (this) — confirms the 2nd-service experience
   reproduces. If Slides is as smooth as Sheets was, the foundation
   is triply validated and the remaining services (Gmail + Calendar)
   are low-risk template applications.

**Note on the ``replaceAllText`` carve-out.** The
``replace_all_text`` helper calls ``presentations.batchUpdate``
with a SINGLE request type (``replaceAllText``) — NOT the full
~40-type tagged-union surface. That's a clean carve-out of the
most common write use case without committing to the full
abstraction. Future PRs that add ``addSlide`` / ``replaceImage`` /
etc. will face the same single-request-type pattern; the
tagged-union design can wait until 3+ consumers exist (the M3 Phase
C extraction-trigger rule applied to API shapes).
"""
