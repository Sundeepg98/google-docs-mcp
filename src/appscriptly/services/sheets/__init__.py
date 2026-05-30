"""Google Sheets service (v2.3.1 — 2nd new service after Drive sharing).

Mirrors the per-service folder layout proven by:

  * Phase A (PR #94)        — services/docs/
  * Phase B (PR #96)        — services/drive/{api,tools}
  * Phase C (PR #109)       — services/gas_deploy/
  * Gap #7 (PR #113)        — services/admin/ (closes ISP asymmetry)
  * v2.3.0 (PR #117)        — services/drive/sharing.py (1st bolt-on)
  * v2.3.1 (this PR)        — services/sheets/                ← here

Layout:

    services/sheets/
    ├── __init__.py    — this file
    ├── api.py         — Sheets REST wrapper (read_range / write_range)
    └── tools.py       — @workspace_tool decorators (registered via
                          server.py's side-effect import)

**Minimal start.** Per the multi-service feasibility audit (R33
agent ``a2d2492bbebb200a6``):

    "Sheets — pattern stretch. batchUpdate with ~40 request types as
    tagged union has no precedent in the foundation."

This PR ships only range-shaped reads / writes (the rectangular
``A1:Z1000`` paradigm). Cell formatting, sheet creation, chart
embedding, pivots, and the rest of the ``batchUpdate`` tagged-union
surface are DEFERRED to a follow-up PR when actual need emerges.

By landing the minimal shape first we get empirical evidence that:

1. The ``get_service`` chokepoint scales to a new Google service
   (``("sheets", "v4")``) without infra change.
2. The ``@workspace_tool`` decorator absorbs sheets tools as-is
   (no special-casing).
3. The partition test pattern absorbs an arbitrary nth service via
   a single frozenset addition (PR #117's papercut fix already
   removed the hardcoded count).
4. The ``InMemoryGoogleAPIClient`` test pattern stubs
   ``sheets.spreadsheets().values()`` exactly like it stubs
   ``drive.files()`` / ``drive.permissions()``.

Sheets is the 2nd new service after Drive sharing. If this lands
clean, Slides / Gmail / Calendar follow the same template with
confidence.
"""
