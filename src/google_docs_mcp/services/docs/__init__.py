"""``services/docs`` — Google Docs document tools.

Layout:

- ``api.py``   — pure-function helpers wrapping the Google Docs API
                 (relocated from top-level ``docs_api.py`` in M3 POC).
- ``tools.py`` — ``@gdocs_tool``-decorated MCP tool functions. Imported
                 explicitly from ``server.py`` AFTER the ``mcp`` instance
                 is constructed (side-effect: tool registration). NOT
                 auto-imported from this ``__init__.py`` — that would
                 create a circular import (server → services.docs.api →
                 services/docs/__init__ → tools → server).
"""
