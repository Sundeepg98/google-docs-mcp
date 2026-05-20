"""Per-service tool folders (M3 — Hex foundation refactor).

Each subpackage represents one external service the MCP server
exposes tools for:

- ``docs``  — Google Docs document operations (tabs, content, structure).
- ``drive`` — Drive file operations (find, trash, untrash, move). *NOT YET MIGRATED*.
- ``gas_deploy`` — Apps Script project provisioning. *NOT YET MIGRATED*.

Importing a service subpackage triggers ``@mcp.tool`` decoration of
its tool functions — registration is a side-effect of the import.
``server.py`` performs the imports in the right order (after
constructing the ``mcp`` instance) so this side-effect lands cleanly.

**M3 POC (v2.1.3) — ONLY ``docs`` is migrated.** ``drive`` and
``gas_deploy`` tools remain in ``server.py`` pending user review of
the POC. See ``docs/ARCHITECTURE.md`` §5.1 for the design rationale.
"""
