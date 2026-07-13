"""Tests for the unified activation-UX helper (appscriptly/activation.py).

The single source of the canonical activation shape shared across the as_*
installer families (Stream 3). Pins the four-field contract and the
deep-link verdict (S0-6): the editor ROOT is the best available URL, since
no function-level deep link exists.
"""
from __future__ import annotations

from appscriptly import activation


def test_activation_editor_url_is_editor_root():
    """The activation URL is the editor root (/d/{scriptId}/edit) - the best
    deep link that exists (no function-select / Run URL parameter)."""
    assert activation.activation_editor_url("ABC123") == (
        "https://script.google.com/d/ABC123/edit"
    )


def test_build_activation_fields_shape():
    """The canonical four-field payload every Class D-H family now returns."""
    fields = activation.build_activation_fields(
        "SID9", "installTrigger", "Run installTrigger and Allow."
    )
    assert fields == {
        "activation_required": True,
        "activation_function": "installTrigger",
        "activation_url": "https://script.google.com/d/SID9/edit",
        "activation_instructions": "Run installTrigger and Allow.",
    }


def test_build_activation_fields_url_derives_from_script_id():
    """activation_url is always derived from script_id - the two never drift."""
    fields = activation.build_activation_fields("XYZ", "renderFrames", "go")
    assert fields["activation_url"].endswith("/d/XYZ/edit")


def test_build_activation_fields_always_requires_activation():
    """The builder is the 'still needs a manual step' shape: activation_required
    is unconditionally True (callers only build it when a Run + Allow remains)."""
    fields = activation.build_activation_fields("S", "gradeResponses", "x")
    assert fields["activation_required"] is True
