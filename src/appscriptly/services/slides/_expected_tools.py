"""Declared tool surface for the slides service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gslides_get_outline",
    "gslides_replace_all_text",
    "gslides_create_presentation",
    "gslides_add_slide",
    "gslides_create_image",
    "gslides_create_table",
    "gslides_create_shape",
    "gslides_create_line",
    "gslides_set_speaker_notes",
    "gslides_delete_object",
    "gslides_duplicate_object",
    "gslides_update_element_transform",
})
