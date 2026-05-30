"""``services/drive`` — Google Drive file-management tools.

Layout:

- ``api.py``   — pure-function helpers wrapping the Google Drive API
                 (relocated from top-level ``drive_api.py`` in M3 Phase B).
- ``tools.py`` — ``@gdocs_tool``-decorated MCP tool functions. Imported
                 explicitly from ``server.py`` AFTER the ``mcp`` instance
                 is constructed (side-effect: tool registration).

**M3 Phase B (v2.1.4)**: this is the second per-service folder
(after ``services/docs`` in Phase A). ``services/gas_deploy`` follows
in Phase C, pending user review.

See ``docs/ARCHITECTURE.md`` §5.1 for the migration plan.
"""
