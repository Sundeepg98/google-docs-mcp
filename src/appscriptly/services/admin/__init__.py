"""``services/admin`` — admin / introspection / auth tools (v2.2.2).

Closes audit Gap #7 (Hex specialist 92% + SOLID specialist 78%):
the 7 stay-in-server tools that didn't fit a Google-API-service
folder previously lived in server.py. Hex called this a mild ISP
violation; SOLID called out the asymmetry of the per-service
folder layout being incomplete.

Layout:

- ``tools.py`` — ``@workspace_tool``-decorated MCP tool functions
  + admin-domain helpers (test-results parsing, mutation-check
  reading, admin-token gating). Imported explicitly from
  ``server.py`` AFTER the ``mcp`` instance is constructed
  (side-effect: tool registration).
- (no ``api.py``) — these tools either don't call Google APIs
  (``gdocs_help``, ``gdocs_guide``, ``gdocs_server_info``,
  ``gdocs_test_manifest``, ``gdocs_admin_audit``) or wrap
  non-Google-API operations (``gdocs_get_signed_upload_url``
  mints HMAC URLs locally; ``gdocs_reset_authorization`` clears
  cred state). No "Google Docs/Drive/Apps Script REST API"
  surface to wrap.

**Tools registered here** (7 admin-service tools):

1. ``gdocs_server_info``      — server identity + tool inventory + CI status
2. ``gdocs_test_manifest``    — full test inventory + per-test outcomes
3. ``gdocs_guide``            — orientation as a structured payload
4. ``gdocs_help``             — error-message recovery guidance
5. ``gdocs_get_signed_upload_url`` — mint one-shot signed upload URL
6. ``gdocs_reset_authorization``   — clear stored OAuth credentials
7. ``gdocs_admin_audit``      — forensic timeline (admin-token gated)

After this extraction, ``server.py`` contains NO tool definitions —
only the ``mcp`` instance + ``main()`` + CLI dispatch + decorator
wiring + side-effect imports for the 4 service folders. The audit's
"server.py still has 7 mixed-concern tools" finding is closed.
"""
