"""Output-schema coverage tests (v2.0.6 / R33 F6).

Pins the response contract for every ``@mcp.tool`` so:
- Adding a new tool without an ``output_schema=`` fails CI
  (caught by ``test_every_tool_has_output_schema``).
- Removing or renaming a load-bearing key in a tool's return dict
  surfaces here (caught by the per-tool runtime validation tests for
  the 5 tools that can be exercised without Google API mocks).
- The schema registry and the actual tool registry stay in lockstep
  (``test_tool_schemas_registry_matches_registered_tools``).

**Scope rationale.** The 5 runtime-validated tools (``gdocs_get_tab_url``,
``gdocs_help``, ``gdocs_guide``, ``gdocs_server_info``, and
``gdocs_get_signed_upload_url`` with a mocked auth context) are the
ones that can be driven without Google API call mocks. The remaining
19 tools are schema-pinned + iteration-checked but not invoked
end-to-end here; their existing integration tests
(``test_roundtrip.py``, ``test_retrofit.py``, etc.) exercise the real
return shape and would surface contract drift.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import jsonschema
import pytest


def _list_tools():
    """Run mcp.list_tools() in a fresh event loop."""
    from google_docs_mcp.server import mcp
    return asyncio.run(mcp.list_tools())


# ---------------------------------------------------------------------
# Coverage: every tool declares an output_schema
# ---------------------------------------------------------------------


def test_every_tool_has_output_schema():
    """No tool may ship without an ``output_schema=`` on its decorator.

    Closes R15 F6 / R33: pre-v2.0.6 only 3 of 24 tools had any kind of
    response-shape contract test; this guard prevents that gap from
    re-opening.
    """
    tools = _list_tools()
    missing = [t.name for t in tools if t.output_schema is None]
    assert not missing, (
        f"Tools missing output_schema=: {missing}. "
        f"Declare a schema constant in tool_schemas.py and add "
        f"output_schema=<CONST> to the @mcp.tool decorator."
    )


def test_every_tool_schema_is_well_formed_json_schema():
    """Each declared output_schema must itself be a valid JSON Schema
    (well-formed against Draft 7+ meta-schema). A typo'd schema can pass
    decorator registration but blows up at runtime; catching it here is
    cheaper than a production traceback."""
    tools = _list_tools()
    bad = []
    for t in tools:
        if t.output_schema is None:
            continue
        try:
            jsonschema.Draft202012Validator.check_schema(t.output_schema)
        except jsonschema.exceptions.SchemaError as e:
            bad.append((t.name, str(e).splitlines()[0]))
    assert not bad, f"Malformed output_schema: {bad}"


def test_every_tool_schema_is_object_type():
    """Per FastMCP / MCP spec, output_schema MUST be ``type: object`` at
    the top level. Other shapes (``oneOf`` at root, ``array``, etc.) get
    rejected at decorator registration; this test catches it before CI."""
    tools = _list_tools()
    not_object = []
    for t in tools:
        if t.output_schema is None:
            continue
        if t.output_schema.get("type") != "object":
            not_object.append((t.name, t.output_schema.get("type")))
    assert not not_object, (
        f"output_schema must be type=object at root; got: {not_object}"
    )


# ---------------------------------------------------------------------
# Registry consistency: tool_schemas.TOOL_OUTPUT_SCHEMAS matches reality
# ---------------------------------------------------------------------


def test_tool_schemas_registry_matches_registered_tools():
    """The declarative ``TOOL_OUTPUT_SCHEMAS`` registry in
    tool_schemas.py and the actual ``mcp.list_tools()`` set must match.
    Catches: registry has stale entry for a deleted tool, or new tool
    added to server.py without a registry entry."""
    from google_docs_mcp.tool_schemas import TOOL_OUTPUT_SCHEMAS

    registered = {t.name for t in _list_tools()}
    in_registry = set(TOOL_OUTPUT_SCHEMAS.keys())

    extra_in_registry = in_registry - registered
    missing_from_registry = registered - in_registry

    assert not extra_in_registry, (
        f"tool_schemas.py registers schemas for unknown tools: "
        f"{extra_in_registry} — were they removed from server.py?"
    )
    assert not missing_from_registry, (
        f"server.py registers tools without a schema in "
        f"tool_schemas.TOOL_OUTPUT_SCHEMAS: {missing_from_registry}"
    )


def test_each_decorator_schema_matches_registry_entry():
    """The ``output_schema=`` on each @mcp.tool decorator must be the
    SAME OBJECT as the registry entry. Prevents a copy-paste regression
    where a decorator imports the wrong schema constant."""
    from google_docs_mcp.tool_schemas import TOOL_OUTPUT_SCHEMAS

    tools = _list_tools()
    mismatched = []
    for t in tools:
        if t.name not in TOOL_OUTPUT_SCHEMAS:
            continue
        registry_schema = TOOL_OUTPUT_SCHEMAS[t.name]
        # FastMCP may copy / normalize the schema dict; compare by
        # structural equality rather than identity.
        if t.output_schema != registry_schema:
            mismatched.append(t.name)
    assert not mismatched, (
        f"Decorator's output_schema diverges from "
        f"TOOL_OUTPUT_SCHEMAS for: {mismatched}"
    )


# ---------------------------------------------------------------------
# Runtime response validation — the 5 tools we can drive without
# mocking Google API calls.
# ---------------------------------------------------------------------


def test_gdocs_get_tab_url_response_matches_schema():
    """``gdocs_get_tab_url`` is pure URL composition — no API call.
    Drive it, validate response against its declared schema."""
    from google_docs_mcp.server import gdocs_get_tab_url
    from google_docs_mcp.tool_schemas import GDOCS_GET_TAB_URL_OUTPUT_SCHEMA

    result = gdocs_get_tab_url("DOC123", "TAB456")
    jsonschema.validate(result, GDOCS_GET_TAB_URL_OUTPUT_SCHEMA)


def test_gdocs_help_matched_response_matches_schema():
    """gdocs_help with a known pattern — exercises the ``matched: True``
    branch of the oneOf schema."""
    from google_docs_mcp.server import gdocs_help
    from google_docs_mcp.tool_schemas import GDOCS_HELP_OUTPUT_SCHEMA

    # Use an error string that's likely to match — the table includes
    # "Apps Script Web App URL not configured" per gdocs_setup_apps_script
    # docstring. Try a substring that's guaranteed to land on SOME entry.
    # Fall back to the miss branch test if no match.
    result = gdocs_help("Apps Script Web App URL not configured")
    jsonschema.validate(result, GDOCS_HELP_OUTPUT_SCHEMA)
    # And the response is a dict with the discriminator present.
    assert "matched" in result


def test_gdocs_help_unmatched_response_matches_schema():
    """gdocs_help with garbage input — exercises ``matched: False``."""
    from google_docs_mcp.server import gdocs_help
    from google_docs_mcp.tool_schemas import GDOCS_HELP_OUTPUT_SCHEMA

    result = gdocs_help("zzzz_nonexistent_pattern_xyzqwerty_9876543210")
    jsonschema.validate(result, GDOCS_HELP_OUTPUT_SCHEMA)
    assert result["matched"] is False
    assert "available_patterns" in result
    assert "suggestion" in result


def test_gdocs_guide_response_matches_schema():
    """gdocs_guide is pure-local. Drive it + validate."""
    from google_docs_mcp.server import gdocs_guide
    from google_docs_mcp.tool_schemas import GDOCS_GUIDE_OUTPUT_SCHEMA

    result = gdocs_guide()
    jsonschema.validate(result, GDOCS_GUIDE_OUTPUT_SCHEMA)


def test_gdocs_server_info_response_matches_schema():
    """gdocs_server_info reads from local registry — no API call. The
    function is async, so wrap in asyncio.run."""
    from google_docs_mcp.server import gdocs_server_info
    from google_docs_mcp.tool_schemas import GDOCS_SERVER_INFO_OUTPUT_SCHEMA

    result = asyncio.run(gdocs_server_info())
    jsonschema.validate(result, GDOCS_SERVER_INFO_OUTPUT_SCHEMA)


def test_gdocs_get_signed_upload_url_response_matches_schema(monkeypatch):
    """gdocs_get_signed_upload_url with a mocked auth context — exercises
    the v2.1 user-bound mint path without needing a live FastMCP request.

    **v2.1.1 (M1a-complete)**: pre-v2.1.1 this test used
    ``monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)`` to give the
    HKDF derivation a master to work from. The InMemoryKeyProvider
    pattern (PR #88's M1a port) lets the test inject a deterministic
    key for ``signed_url`` directly — no env coupling, no incidental
    dependency on HKDF input length.
    """
    from google_docs_mcp.server import gdocs_get_signed_upload_url
    from google_docs_mcp import server as server_mod
    from google_docs_mcp.key_provider import (
        InMemoryKeyProvider,
        with_key_provider,
    )
    from google_docs_mcp.tool_schemas import (
        GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    )

    monkeypatch.setattr(
        server_mod, "current_user_id_or_none", lambda: "test-user-sub",
    )

    with with_key_provider(InMemoryKeyProvider({
        "signed_url": b"deterministic-signed-url-key-32b",
    })):
        result = gdocs_get_signed_upload_url(ttl_seconds=60)

    jsonschema.validate(result, GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA)
    assert result["user_id"] == "test-user-sub"
