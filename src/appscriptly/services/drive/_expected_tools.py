"""Declared tool surface for the drive service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. The 7 file-CRUD tools (tools.py wrapping api.py) + 3 sharing
tools (tools.py wrapping sharing.py) all carry ``service="drive"``.

**Namespace cleanup (chore/tool-namespace-cleanup).** These tools act on
Drive, not Docs, so the canonical names use the honest ``gdrive_``
prefix. Every old ``gdocs_`` name stays registered as a DEPRECATED ALIAS
(dual-registration; planned removal v3.0) so nothing breaks — so BOTH
the ``gdrive_*`` canonical AND the ``gdocs_*`` alias are declared here
(one underlying implementation, two registrations each), mirroring the
gas_deploy install-automation precedent.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    # --- canonical gdrive_* names ---
    "gdrive_find_doc_by_title",
    "gdrive_move_to_folder",
    "gdrive_trash_file",
    "gdrive_untrash_file",
    # File-CRUD: folder creation (destination for move_to_folder).
    "gdrive_create_folder",
    # File-CRUD: export a Google-native file to a portable format
    # (files.export → PDF/Office/etc., stored back in Drive).
    "gdrive_export_file",
    # File-CRUD: generalized find over app-accessible files of ANY type
    # (files.list with optional mime/fullText/folder filters). The
    # type-agnostic generalization of find_doc_by_title.
    "gdrive_find_file",
    # File-CRUD: rename in place (files.update name). BUG 2b of the
    # 2026-07 wave; canonical-only - never had a gdocs_ name, so NO
    # deprecated alias is registered for it.
    "gdrive_rename_file",
    # sharing sub-module (tools live in drive/tools.py, delegating to
    # drive/sharing.py).
    "gdrive_share_file",
    "gdrive_list_permissions",
    # Sharing: revoke a previously-granted permission (inverse of share).
    "gdrive_revoke_permission",
    # --- deprecated gdocs_* aliases (dual-registration; removal v3.0) ---
    "gdocs_find_doc_by_title",
    "gdocs_move_to_folder",
    "gdocs_trash_file",
    "gdocs_untrash_file",
    "gdocs_create_folder",
    "gdocs_export_doc",
    "gdocs_find_file",
    "gdocs_share_file",
    "gdocs_list_permissions",
    "gdocs_revoke_permission",
})
