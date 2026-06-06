"""Declared tool surface for the drive service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. The 7 file-CRUD tools (tools.py wrapping api.py) + 3 sharing
tools (tools.py wrapping sharing.py) all carry ``service="drive"``.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gdocs_find_doc_by_title",
    "gdocs_move_to_folder",
    "gdocs_trash_file",
    "gdocs_untrash_file",
    # File-CRUD: folder creation (destination for move_to_folder).
    "gdocs_create_folder",
    # File-CRUD: export a Google-native file to a portable format
    # (files.export → PDF/Office/etc., stored back in Drive).
    "gdocs_export_doc",
    # File-CRUD: generalized find over app-accessible files of ANY type
    # (files.list with optional mime/fullText/folder filters). The
    # type-agnostic generalization of find_doc_by_title.
    "gdocs_find_file",
    # v2.3.0 — sharing sub-module (tools live in drive/tools.py,
    # delegating to drive/sharing.py).
    "gdocs_share_file",
    "gdocs_list_permissions",
    # Sharing: revoke a previously-granted permission (inverse of share).
    "gdocs_revoke_permission",
})
