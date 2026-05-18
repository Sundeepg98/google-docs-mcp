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
    "gdocs_reset_authorization",  # v1.1.1+: force re-consent / recovery
    "gdocs_server_info",
    "gdocs_test_manifest",  # v1.1.3+: surface test inventory + outcomes
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


def test_tool_discoverability_via_server_info(all_tools):
    """v1.1.1 regression guard. gdocs_server_info MUST match
    mcp.list_tools() exactly. The Issue D bug was that a tool
    (gdocs_reset_authorization) was visible in server_info but
    undiscoverable via tool_search — root cause was thin description
    text. This guard catches the structural shape (count + names);
    test_tool_descriptions_truthful catches the description-thinness
    that drives ranker discoverability.
    """
    import asyncio

    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    info_tools = set(info["tools"])
    list_tools = set(all_tools.keys())

    assert info_tools == list_tools, (
        f"gdocs_server_info.tools and mcp.list_tools() disagree:\n"
        f"  in server_info but not list_tools: {info_tools - list_tools}\n"
        f"  in list_tools but not server_info: {list_tools - info_tools}\n"
        "An agent that trusts server_info but the search ranker uses "
        "list_tools (or vice versa) will see a tool they can't call."
    )
    assert info["tool_count"] == len(list_tools), (
        f"server_info.tool_count ({info['tool_count']}) != "
        f"len(list_tools) ({len(list_tools)})"
    )


# Tools that explicitly DO need OAuth (everything that touches Google
# APIs). The only exception is gdocs_get_signed_upload_url which uses
# bearer-token auth (no OAuth grant required). For ALL others, the
# description must NOT claim the tool works "without setup" or
# "without authorization" unqualified — that conflates the
# Apps-Script-Web-App setup (which only gdocs_tab_existing_doc needs)
# with the base OAuth grant (which everything needs).
_DOES_NOT_NEED_OAUTH = {"gdocs_get_signed_upload_url"}

_MISLEADING_PHRASES = [
    "without setup",
    "without authorization",
    "without auth",
    "no setup needed",
    "no setup required",
    "no auth needed",
    "no auth required",
]


@pytest.mark.parametrize(
    "tool_name",
    sorted(EXPECTED_TOOLS - _DOES_NOT_NEED_OAUTH),
)
def test_tool_descriptions_truthful(all_tools, tool_name):
    """v1.1.1 regression guard. Issue A from cloud-chat testing:
    gdocs_setup_apps_script's docstring said other tools "don't need
    it and work without setup." That conflated two prerequisites:
    (a) Apps Script Web App setup, (b) base Google OAuth grant.
    Tools don't need (a) but ALL tools need (b). Saying "without
    setup" unqualified misleads the model into trying calls that
    will return needs_authorization.

    For every tool that needs Google OAuth (i.e. all of them except
    the bearer-authed signed-upload-URL tool), assert the description
    doesn't contain any misleading "no setup / no auth needed"
    phrasing unqualified.
    """
    tool = all_tools[tool_name]
    desc = (tool.description or "").lower()

    for phrase in _MISLEADING_PHRASES:
        if phrase in desc:
            # Phrase found — must be qualified within ~150 chars by
            # a clarifying word (oauth, authoriz, consent, sign-in)
            # so the model gets the right mental model.
            idx = desc.find(phrase)
            window = desc[max(0, idx - 100):idx + 150]
            qualifying = any(
                q in window for q in (
                    "oauth", "authoriz", "consent", "sign in", "sign-in",
                    "needs_authorization", "google account",
                )
            )
            assert qualifying, (
                f"{tool_name}: description contains misleading phrase "
                f"'{phrase}' without a nearby OAuth clarifier. "
                f"Context: ...{window}...\n"
                "This is the v1.1.1 Issue A bug pattern. Either qualify "
                f"the phrase (e.g. 'works without {phrase.split()[1]} "
                "but requires the one-time Google OAuth grant'), or "
                "remove the phrase entirely."
            )


def test_tab_nesting_depth_cap_enforced():
    """Part 1 contract guard. Google Docs UI hard-limits tab nesting
    to 3 levels (root + 2 child levels). make_doc_with_tabs must
    reject deeper inputs BEFORE creating the doc, otherwise we leak
    an empty doc + the user gets a confusing API error from Google.
    """
    from unittest.mock import MagicMock
    from google_docs_mcp.docs_api import make_doc_with_tabs

    # 4-level nesting: root → child → grandchild → great-grandchild.
    # Should raise ValueError before any Google API call (so the
    # MagicMock creds never actually hit the wire).
    too_deep = [{
        "title": "L0", "content": "",
        "children": [{
            "title": "L1", "content": "",
            "children": [{
                "title": "L2", "content": "",
                "children": [{"title": "L3-too-deep", "content": ""}],
            }],
        }],
    }]

    with pytest.raises(ValueError, match="Max nesting depth"):
        make_doc_with_tabs(MagicMock(), "test", too_deep)


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_tool_input_schema_non_empty(all_tools, tool_name):
    """v1.1.1 regression guard for the tool_search discoverability
    bug. A tool with type=object but no properties is technically
    valid but indexes poorly — the ranker has no signal about what
    the tool DOES from its schema. Either the tool takes no args
    (then properties={} is fine, but the description has to carry
    the load), or it has properties (which should be non-empty).

    This catches future tools that get a schema-stripping decorator
    applied wrong, leaving them with bare type=object.
    """
    tool = all_tools[tool_name]
    schema = tool.parameters or {}
    properties = schema.get("properties") or {}

    # Tools that legitimately take no arguments. server_info is pure
    # introspection. setup_apps_script identifies the calling user
    # via OAuth context (get_access_token claims) so needs no kwargs;
    # the deploy is parameter-less by design.
    no_arg_tools = {"gdocs_server_info", "gdocs_setup_apps_script", "gdocs_test_manifest"}
    if tool_name in no_arg_tools:
        return  # empty properties is fine for these

    assert properties, (
        f"{tool_name}: input schema has no properties — that signals "
        "to ranker that the tool takes no input, which mismatches "
        "the actual description. Likely cause: a wrapper decorator "
        "stripped the function signature incorrectly."
    )
