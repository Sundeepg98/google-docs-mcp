"""Schema contract tests for every MCP tool.

Guards against regressions where:
  - a tool gets renamed/removed but other tools still reference it
  - a tool ships without a description (degrades model routing)
  - a parameter loses its type annotation (the v0.19.0 file_id bug:
    untyped params produce permissive schemas that some clients coerce
    to string, breaking list inputs)
  - the trash/untrash file_id param fails to declare ``str | list[str]``
"""
from __future__ import annotations

import asyncio

import pytest


# Tools we expect to exist. If this set changes, the test fails — making
# additions/removals/renames a deliberate, reviewed change.
EXPECTED_TOOLS = {
    "gdocs_add_tabs",
    "gdocs_append_to_tab",
    "gdocs_delete_tab",
    "gdocs_find_doc_by_title",
    "gdocs_get_doc_outline",
    "gdocs_get_signed_upload_url",
    "gdocs_get_tab_url",
    "gdocs_make_tabbed_doc",
    "gdocs_move_to_folder",
    "gdocs_preview_tab_split",
    "gdocs_read_doc",
    "gdocs_rename_tab",
    "gdocs_replace_all_text",
    "gdocs_server_info",
    "gdocs_set_tab_icons",
    "gdocs_setup_apps_script",  # v1.1+: per-user Apps Script setup
    "gdocs_tab_existing_doc",
    "gdocs_trash_file",
    "gdocs_untrash_file",
}


@pytest.fixture(scope="module")
def all_tools():
    """Snapshot the live tool registry once per module."""
    from google_docs_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


def test_expected_tool_set_matches(all_tools):
    """All expected tools registered, no surprise extras."""
    actual = set(all_tools.keys())
    missing = EXPECTED_TOOLS - actual
    extra = actual - EXPECTED_TOOLS
    assert not missing, f"missing tools: {missing}"
    assert not extra, (
        f"unexpected new tools (update EXPECTED_TOOLS if intentional): {extra}"
    )


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_tool_has_description(all_tools, tool_name):
    """Every tool ships a non-empty description (routing depends on it)."""
    tool = all_tools[tool_name]
    desc = (tool.description or "").strip()
    assert desc, f"{tool_name}: description is empty/missing"
    assert len(desc) > 30, (
        f"{tool_name}: description '{desc[:60]}...' is too short — "
        "Anthropic recommends 3-4+ sentences for good tool routing"
    )


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_tool_has_input_schema(all_tools, tool_name):
    """Every tool ships an input schema with at least a type."""
    tool = all_tools[tool_name]
    schema = tool.parameters or {}
    assert schema.get("type") == "object", (
        f"{tool_name}: input schema missing or wrong type: {schema}"
    )


@pytest.mark.parametrize("tool_name", ["gdocs_trash_file", "gdocs_untrash_file"])
def test_trash_file_id_accepts_str_or_list(all_tools, tool_name):
    """0.19.0 regression guard: file_id MUST accept both str and array of str.

    When v0.19.0 shipped without an explicit type annotation, FastMCP
    generated a permissive schema that claude.ai's MCP client coerced
    to a string for array inputs, breaking batch mode. v0.19.2 fixed
    this by declaring ``file_id: str | list[str]``. Don't regress.
    """
    tool = all_tools[tool_name]
    props = (tool.parameters or {}).get("properties") or {}
    file_id_schema = props.get("file_id") or {}
    any_of = file_id_schema.get("anyOf") or []
    types_offered = {entry.get("type") for entry in any_of}
    assert "string" in types_offered, (
        f"{tool_name}.file_id must accept 'string' in anyOf; "
        f"got: {file_id_schema}"
    )
    assert "array" in types_offered, (
        f"{tool_name}.file_id must accept 'array' in anyOf to enable "
        f"batch mode; got: {file_id_schema}. "
        f"Likely cause: missing/wrong type annotation on the function."
    )


def test_tool_count_consistency(all_tools):
    """Server's view of its tool count must agree with the registry."""
    assert len(all_tools) == len(EXPECTED_TOOLS)
