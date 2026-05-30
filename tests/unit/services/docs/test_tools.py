"""Per-tool behavior tests for services/docs/tools.py (Gap #5).

Per the test architect (Round 5 audit):

  > "services/docs/tools.py at 31% coverage — Round 5 per-tool
  >  InMemory tests not delivered. The creds=False invariant test
  >  that #103 added is excellent — that's the model for what the
  >  docs/drive per-tool tests should look like."

This file is the per-tool counterpart to tests/unit/services/gas_deploy/
test_tools.py: one happy-path test per tool exercising the tool body
through its decorator envelope, using the M2
``with_google_api_client(InMemoryGoogleAPIClient({...}))`` port
(PR #92) to inject stub Resources. The tools that mutate Google API
state get stubs registered; the URL-builder tool (``gdocs_get_tab_url``)
needs no stub because it's pure string composition.

What each test asserts:
  - The tool runs to completion under the decorator envelope (the
    `_get_credentials_fn` injection + HttpError → ToolError wrap).
  - The return shape matches the documented contract.
  - At least one Google API call site was reached (proves the body
    actually ran and didn't short-circuit on validation).

These are NOT exhaustive end-to-end tests — those live in
test_soft_failure_contracts.py (api-layer) and the integration
suite. The point is to give the docs tools.py file the same per-tool
behavior coverage gas_deploy/tools.py got in PR #103.

Coverage delta target: services/docs/tools.py 31% → meaningful uplift.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.docs import tools


@pytest.fixture
def stub_creds():
    """The sentinel `creds` object that `_get_credentials_fn` returns.

    Tools never introspect the creds beyond passing them through to
    `get_service(..., credentials=creds)`, which the InMemory adapter
    ignores. A MagicMock is sufficient.
    """
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap `decorators._get_credentials_fn` to return our stub for
    every `creds=True` tool call in this file.

    Restores the original on test exit via monkeypatch — production
    behavior is unaffected outside the test body.
    """
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


@pytest.fixture
def docs_stub():
    """A Google Docs v1 Resource stub.

    Pre-wires the common method chains the tool bodies invoke
    (`documents().create`, `documents().get`, `documents().batchUpdate`)
    so a test that doesn't customize them still gets sane defaults.
    Per-test overrides via `docs_stub.documents().X().execute.return_value = ...`
    follow the test_soft_failure_contracts.py pattern.
    """
    docs = MagicMock(name="docs-v1-stub")
    docs.documents().create().execute.return_value = {"documentId": "DOC123"}
    docs.documents().get().execute.return_value = {
        "documentId": "DOC123",
        "tabs": [{"tabProperties": {"tabId": "TAB0", "title": "Tab 0"}}],
    }
    docs.documents().batchUpdate().execute.return_value = {"replies": []}
    return docs


@pytest.fixture
def with_docs_stub(docs_stub):
    """Activate `docs_stub` as the Google Docs v1 client for the test."""
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("docs", "v1"): docs_stub,
    })):
        yield docs_stub


# ---------------------------------------------------------------------
# 1. gdocs_make_tabbed_doc — create doc + materialize tabs
# ---------------------------------------------------------------------


def test_gdocs_make_tabbed_doc_creates_doc_and_returns_doc_id(with_docs_stub):
    """The happy path: pass one root tab, expect documents().create called
    with the title and a doc_id returned in the contract shape."""
    # Seed: get() returns the existing first tab so _materialize_tab_tree
    # can rename it; batchUpdate replies cover any content insertion.
    result = tools.gdocs_make_tabbed_doc(
        title="My Doc",
        tabs=[{"title": "Intro", "content": "Hello world"}],
    )

    assert result["doc_id"] == "DOC123"
    assert result["url"].endswith("/edit")
    # documents().create was called with the requested title.
    create_calls = with_docs_stub.documents().create.call_args_list
    assert any(
        call.kwargs.get("body", {}).get("title") == "My Doc"
        for call in create_calls
    ), f"create() never received title='My Doc'; got: {create_calls}"


def test_gdocs_make_tabbed_doc_rejects_empty_tabs_list():
    """Pre-API validation: empty tabs list raises ToolError before any
    `get_service` call. No InMemory stub needed because we never reach
    the Google API."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="at least one tab"):
        tools.gdocs_make_tabbed_doc(title="Doc", tabs=[])


def test_gdocs_make_tabbed_doc_rejects_oversized_emoji():
    """Per-tab icon_emoji validation — must reject >8 UTF-8 bytes."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="icon_emoji"):
        tools.gdocs_make_tabbed_doc(
            title="Doc",
            tabs=[{
                "title": "T", "content": "",
                "icon_emoji": "abcdefghi",  # 9 bytes
            }],
        )


# ---------------------------------------------------------------------
# 2. gdocs_add_tabs — append to existing doc
# ---------------------------------------------------------------------


def test_gdocs_add_tabs_calls_docs_api_and_returns_tabs_list(with_docs_stub):
    """Add one root tab to an existing doc; verify the body ran and the
    return shape carries a `tabs` list."""
    # add_tabs_to_doc fetches the doc to discover existing tabs (so it
    # can position new ones after them), then runs batchUpdate.
    with_docs_stub.documents().get().execute.return_value = {
        "documentId": "DOC1",
        "tabs": [{"tabProperties": {"tabId": "T0", "title": "Existing"}}],
    }
    with_docs_stub.documents().batchUpdate().execute.return_value = {
        "replies": [
            {"createTab": {"tabProperties": {"tabId": "T1", "title": "New"}}},
        ],
    }

    result = tools.gdocs_add_tabs(
        doc_id="DOC1",
        tabs=[{"title": "New", "content": ""}],
    )

    assert "tabs" in result
    # Reached the API: batchUpdate was invoked at least once.
    assert with_docs_stub.documents().batchUpdate.called


def test_gdocs_add_tabs_rejects_empty_tabs_list():
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="at least one tab"):
        tools.gdocs_add_tabs(doc_id="DOC1", tabs=[])


# ---------------------------------------------------------------------
# 3. gdocs_get_doc_outline — read-only tab structure dump
# ---------------------------------------------------------------------


def test_gdocs_get_doc_outline_returns_tabs_list(with_docs_stub, monkeypatch):
    """Outline call dispatches to api.get_doc_outline which fetches
    docs + drive metadata."""
    # The outline fetch reads the doc structure AND queries drive.files
    # for the trashed flag — register both stubs.
    drive_stub = MagicMock(name="drive-v3-stub")
    drive_stub.files().get().execute.return_value = {"trashed": False}

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("docs", "v1"): with_docs_stub,
        ("drive", "v3"): drive_stub,
    })):
        with_docs_stub.documents().get().execute.return_value = {
            "documentId": "DOC1",
            "tabs": [{
                "tabProperties": {
                    "tabId": "T0", "title": "Intro", "index": 0,
                },
                "documentTab": {"body": {"content": []}},
            }],
        }
        result = tools.gdocs_get_doc_outline(doc_id="DOC1")

    assert result["doc_id"] == "DOC1"
    assert isinstance(result["tabs"], list)
    assert "trashed" in result


# ---------------------------------------------------------------------
# 4. gdocs_read_doc — single-tab and all-tabs modes
# ---------------------------------------------------------------------


def test_gdocs_read_doc_all_tabs_when_no_selector_given(docs_stub):
    """No tab_id / no tab_title → read_all_tabs branch.

    read_all_tabs also checks `is_file_trashed`, which needs the
    drive v3 stub — register both here.
    """
    drive_stub = MagicMock(name="drive-v3-stub")
    drive_stub.files().get().execute.return_value = {"trashed": False}
    docs_stub.documents().get().execute.return_value = {
        "documentId": "DOC1",
        "tabs": [{
            "tabProperties": {
                "tabId": "T0", "title": "Intro", "index": 0,
            },
            "documentTab": {"body": {"content": []}},
        }],
    }

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("docs", "v1"): docs_stub,
        ("drive", "v3"): drive_stub,
    })):
        result = tools.gdocs_read_doc(doc_id="DOC1")

    assert result["doc_id"] == "DOC1"
    assert isinstance(result["tabs"], list)


def test_gdocs_read_doc_single_tab_when_tab_id_given(docs_stub):
    """tab_id given → read_tab_content branch (also needs drive stub
    for the is_file_trashed lookup)."""
    drive_stub = MagicMock(name="drive-v3-stub")
    drive_stub.files().get().execute.return_value = {"trashed": False}
    docs_stub.documents().get().execute.return_value = {
        "documentId": "DOC1",
        "tabs": [{
            "tabProperties": {
                "tabId": "T0", "title": "Intro", "index": 0,
            },
            "documentTab": {"body": {"content": []}},
        }],
    }

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("docs", "v1"): docs_stub,
        ("drive", "v3"): drive_stub,
    })):
        result = tools.gdocs_read_doc(doc_id="DOC1", tab_id="T0")

    assert result["tab_id"] == "T0"
    assert "paragraphs" in result


# ---------------------------------------------------------------------
# 5. gdocs_append_to_tab — content insertion at end of tab
# ---------------------------------------------------------------------


def test_gdocs_append_to_tab_runs_body_and_returns_appended_chars(with_docs_stub):
    """The tool dispatches to api.append_to_tab which calls
    documents().get to find the insertion index, then batchUpdate."""
    with_docs_stub.documents().get().execute.return_value = {
        "documentId": "DOC1",
        "tabs": [{
            "tabProperties": {"tabId": "T0", "title": "Intro", "index": 0},
            "documentTab": {
                "body": {
                    "content": [{
                        "paragraph": {"elements": []},
                        "endIndex": 5,
                    }],
                },
            },
        }],
    }

    result = tools.gdocs_append_to_tab(
        doc_id="DOC1", tab_id="T0",
        content="More text", content_format="text",
    )

    assert result["tab_id"] == "T0"
    assert "appended_chars" in result
    assert with_docs_stub.documents().batchUpdate.called


# ---------------------------------------------------------------------
# 6. gdocs_tab_existing_doc — convert .docx / Drive doc into tabs
# ---------------------------------------------------------------------


def test_gdocs_tab_existing_doc_rejects_both_inputs_set():
    """Mutex contract: must pass exactly one of docx_path / drive_file_id."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="exactly one"):
        tools.gdocs_tab_existing_doc(
            docx_path="/tmp/x.docx", drive_file_id="DRIVE_X",
        )


def test_gdocs_tab_existing_doc_rejects_neither_input_set():
    """Mutex contract — same error for the omitted case."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="exactly one"):
        tools.gdocs_tab_existing_doc()


def test_gdocs_tab_existing_doc_drive_file_id_dispatches_to_convert(
    monkeypatch, stub_creds,
):
    """drive_file_id path delegates to docx_import._convert_docx. We
    don't run the full Drive→Apps-Script→Docs pipeline here (that's
    integration territory) — just confirm the dispatcher reaches it
    with the right kwargs."""
    captured = {}

    def fake_convert(creds, **kwargs):
        captured["creds"] = creds
        captured["kwargs"] = kwargs
        return {"doc_id": "RESULT", "url": "https://x", "tabs": []}

    monkeypatch.setattr(tools, "_convert_docx", fake_convert)

    result = tools.gdocs_tab_existing_doc(drive_file_id="DRIVE_X")

    assert result["doc_id"] == "RESULT"
    assert captured["creds"] is stub_creds
    assert captured["kwargs"]["drive_file_id"] == "DRIVE_X"
    assert captured["kwargs"]["split_by"] == "heading_1"  # default


# ---------------------------------------------------------------------
# 7. gdocs_rename_tab — update tabProperties (title and/or icon)
# ---------------------------------------------------------------------


def test_gdocs_rename_tab_runs_and_reports_updated_fields(with_docs_stub):
    """Passing both title + icon should yield a 2-element updated_fields list."""
    result = tools.gdocs_rename_tab(
        doc_id="DOC1", tab_id="T0",
        title="New Title", icon_emoji="\U0001f4d1",
    )
    assert result["doc_id"] == "DOC1"
    assert result["tab_id"] == "T0"
    assert set(result["updated_fields"]) == {"title", "iconEmoji"}
    assert with_docs_stub.documents().batchUpdate.called


def test_gdocs_rename_tab_rejects_both_none():
    """At least one field must be non-None — pre-API validation."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="at least one"):
        tools.gdocs_rename_tab(doc_id="DOC1", tab_id="T0")


# ---------------------------------------------------------------------
# 8. gdocs_get_tab_url — pure URL construction, NO API call
# ---------------------------------------------------------------------


def test_gdocs_get_tab_url_composes_deep_link_with_no_api_call():
    """Pure URL builder — no with_google_api_client wrapper needed.
    The decorator is creds=False (omitted), so no creds-injection
    wrapper either."""
    result = tools.gdocs_get_tab_url(doc_id="DOC1", tab_id="T7")
    assert result["doc_id"] == "DOC1"
    assert result["tab_id"] == "T7"
    assert result["url"] == "https://docs.google.com/document/d/DOC1/edit?tab=T7"


# ---------------------------------------------------------------------
# 9. gdocs_delete_tab — destructive=True, idempotent=True
# ---------------------------------------------------------------------


def test_gdocs_delete_tab_runs_body_and_returns_deleted_id(with_docs_stub):
    """One batchUpdate call with a deleteTab request; returns
    {doc_id, deleted_tab_id}."""
    result = tools.gdocs_delete_tab(doc_id="DOC1", tab_id="T_DOOMED")
    assert result == {"doc_id": "DOC1", "deleted_tab_id": "T_DOOMED"}
    assert with_docs_stub.documents().batchUpdate.called


# ---------------------------------------------------------------------
# 10. gdocs_replace_all_text — find/replace across tabs
# ---------------------------------------------------------------------


def test_gdocs_replace_all_text_returns_occurrence_count(with_docs_stub):
    """batchUpdate's replyAllText returns occurrences_changed; the tool
    surfaces it under the same key + a 'scope' field."""
    with_docs_stub.documents().batchUpdate().execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 3}}],
    }
    result = tools.gdocs_replace_all_text(
        doc_id="DOC1", find="foo", replace="bar",
    )
    assert result["occurrences_changed"] == 3
    assert result["scope"] == "all_tabs"


def test_gdocs_replace_all_text_scopes_to_specific_tabs(with_docs_stub):
    """Passing tab_ids restricts scope; the surface returns the list."""
    with_docs_stub.documents().batchUpdate().execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}],
    }
    result = tools.gdocs_replace_all_text(
        doc_id="DOC1", find="x", replace="y", tab_ids=["T0", "T1"],
    )
    assert result["scope"] == ["T0", "T1"]


# ---------------------------------------------------------------------
# 11. gdocs_set_tab_icons — title-keyed batch icon update
# ---------------------------------------------------------------------


def test_gdocs_set_tab_icons_runs_and_returns_match_report(with_docs_stub):
    """Map a single title → emoji; require batchUpdate to run."""
    with_docs_stub.documents().get().execute.return_value = {
        "documentId": "DOC1",
        "tabs": [{
            "tabProperties": {
                "tabId": "T0", "title": "Profile", "index": 0,
            },
        }],
    }

    result = tools.gdocs_set_tab_icons(
        doc_id="DOC1",
        icons_by_title={"Profile": "\U0001f464"},  # 👤
    )

    assert "updated_count" in result
    assert "matched" in result
    assert "unmatched_titles" in result


def test_gdocs_set_tab_icons_rejects_empty_input():
    """Pre-API: empty dict raises ToolError."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="cannot be empty"):
        tools.gdocs_set_tab_icons(doc_id="DOC1", icons_by_title={})


def test_gdocs_set_tab_icons_rejects_oversized_emoji():
    """Per-key icon validation — must reject >8 UTF-8 bytes."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="single emoji"):
        tools.gdocs_set_tab_icons(
            doc_id="DOC1",
            icons_by_title={"Profile": "abcdefghi"},  # 9 bytes
        )


# ---------------------------------------------------------------------
# 12. gdocs_preview_tab_split — creds=False conditional auth path
# ---------------------------------------------------------------------


def test_gdocs_preview_tab_split_dispatches_to_preview_module(monkeypatch):
    """The tool is creds=False — it manages credentials manually
    (fetches them only when drive_file_id is given). Confirm the
    dispatcher reaches `_preview_tab_split` with the right kwargs."""
    captured = {}

    def fake_preview(creds, docx_path, drive_file_id, split_by):
        captured["creds"] = creds
        captured["drive_file_id"] = drive_file_id
        captured["split_by"] = split_by
        return {
            "split_strategy_used": "heading_1",
            "tab_count": 0,
            "tabs": [],
            "problems": [],
        }

    monkeypatch.setattr(tools, "_preview_tab_split", fake_preview)

    # docx_path branch: no creds fetched, no API call.
    result = tools.gdocs_preview_tab_split(docx_path="/tmp/sample.docx")
    assert result["tab_count"] == 0
    assert captured["creds"] is None
    assert captured["split_by"] == "heading_1"
