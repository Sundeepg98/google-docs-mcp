"""Declared tool surface for the admin service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. The single "non-Google-API-service" bucket for the
admin / introspection / auth / signed-URL tools (Gap #7, v2.2.2).

**Namespace cleanup (chore/tool-namespace-cleanup).** These tools are
admin / introspection / auth (not Docs), so the canonical names use
honest prefixes (``server_`` / ``admin_`` / ``account_`` / ``gdrive_``
for the Drive-bound signed-upload URL). Every old ``gdocs_`` name stays
registered as a DEPRECATED ALIAS (dual-registration; planned removal
v3.0), so BOTH names are declared here. The ``service=`` tag stays
``"admin"`` for all of them (it follows the FOLDER, not the prefix) —
including ``gdrive_get_signed_upload_url``, which lives in this folder.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    # --- canonical names ---
    # admin / introspection / local-only
    "server_info",
    "server_test_manifest",
    "server_guide",
    "server_help",
    "admin_audit",
    # auth / signed URLs
    "gdrive_get_signed_upload_url",
    "account_reset_authorization",
    # --- deprecated gdocs_* aliases (dual-registration; removal v3.0) ---
    "gdocs_server_info",
    "gdocs_test_manifest",
    "gdocs_guide",
    "gdocs_help",
    "gdocs_admin_audit",
    "gdocs_get_signed_upload_url",
    "gdocs_reset_authorization",
})
