"""Declared tool surface for the admin service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. The single "non-Google-API-service" bucket for the
admin / introspection / auth / signed-URL tools (Gap #7, v2.2.2).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    # admin / introspection / local-only
    "gdocs_server_info",
    "gdocs_test_manifest",
    "gdocs_guide",
    "gdocs_help",
    "gdocs_admin_audit",
    # auth / signed URLs
    "gdocs_get_signed_upload_url",
    "gdocs_reset_authorization",
})
