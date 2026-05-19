"""Title validation tests (v1.3.1).

Guards _validate_title which prevents control-char + oversize titles
from reaching Drive (which surfaces confusing 400 errors). Wired into
gdocs_make_tabbed_doc, gdocs_tab_existing_doc, gdocs_rename_tab,
gdocs_add_tabs.
"""
from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError


def test_accepts_normal_title():
    from google_docs_mcp.server import _validate_title
    _validate_title("My Document")  # no raise


def test_accepts_unicode_title():
    from google_docs_mcp.server import _validate_title
    _validate_title("Документ — Café — 文档")  # multi-script OK


def test_accepts_max_length():
    from google_docs_mcp.server import _validate_title, _TITLE_MAX_CHARS
    _validate_title("x" * _TITLE_MAX_CHARS)  # exactly at limit OK


def test_rejects_over_max_length():
    from google_docs_mcp.server import _validate_title, _TITLE_MAX_CHARS
    with pytest.raises(ToolError, match="max is"):
        _validate_title("x" * (_TITLE_MAX_CHARS + 1))


def test_rejects_empty_string():
    from google_docs_mcp.server import _validate_title
    with pytest.raises(ToolError, match="cannot be empty"):
        _validate_title("")


def test_rejects_non_string_int():
    from google_docs_mcp.server import _validate_title
    with pytest.raises(ToolError, match="must be a string"):
        _validate_title(42)  # type: ignore[arg-type]


def test_rejects_non_string_none():
    from google_docs_mcp.server import _validate_title
    with pytest.raises(ToolError, match="must be a string"):
        _validate_title(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("ch_code", [0x00, 0x01, 0x1F, 0x7F])
def test_rejects_control_chars(ch_code):
    """U+0000 through U+001F and U+007F are control chars Drive rejects."""
    from google_docs_mcp.server import _validate_title
    bad = f"Title with{chr(ch_code)}control char"
    with pytest.raises(ToolError, match="control character"):
        _validate_title(bad)


def test_rejects_embedded_newline():
    """Newline is U+000A, in the control-char range."""
    from google_docs_mcp.server import _validate_title
    with pytest.raises(ToolError, match="control character"):
        _validate_title("Line 1\nLine 2")


def test_rejects_tab_character():
    """Tab is U+0009, control char."""
    from google_docs_mcp.server import _validate_title
    with pytest.raises(ToolError, match="control character"):
        _validate_title("Has\ttab")


def test_field_name_propagates_in_error():
    """Custom field= kwarg used by per-tab validation should surface."""
    from google_docs_mcp.server import _validate_title
    with pytest.raises(ToolError, match=r"tabs\[3\]\.title"):
        _validate_title("", field="tabs[3].title")


def test_accepts_emoji():
    """Emoji are above U+007F and should pass."""
    from google_docs_mcp.server import _validate_title
    _validate_title("Hello 👋 World")  # no raise
