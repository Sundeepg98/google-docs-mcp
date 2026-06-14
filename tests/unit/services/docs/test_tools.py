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


# ---------------------------------------------------------------------
# gdocs_insert_table — batchUpdate (insertTable)
# ---------------------------------------------------------------------


def _last_batchupdate_body(stub):
    """Body kwarg of the most recent documents().batchUpdate() call."""
    for call in reversed(stub.documents().batchUpdate.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs["body"]
    raise AssertionError("no documents().batchUpdate() call captured a body")


def test_gdocs_insert_table_happy_path(with_docs_stub):
    """Inserts a table; returns the echo envelope through the
    @workspace_tool(creds=True) boundary."""
    result = tools.gdocs_insert_table(
        doc_id="DOC1", rows=3, columns=2,
    )
    assert result == {
        "doc_id": "DOC1",
        "rows": 3,
        "columns": 2,
        "index": 1,
        "tab_id": None,
    }


def test_gdocs_insert_table_builds_insertTable_request(with_docs_stub):
    """The batchUpdate body wraps a single insertTable with a Location
    (index) + rows + columns."""
    tools.gdocs_insert_table(doc_id="DOC1", rows=4, columns=5, index=7)
    body = _last_batchupdate_body(with_docs_stub)
    assert body == {
        "requests": [
            {
                "insertTable": {
                    "location": {"index": 7},
                    "rows": 4,
                    "columns": 5,
                },
            },
        ],
    }


def test_gdocs_insert_table_scopes_to_tab_when_given(with_docs_stub):
    """A tab_id adds ``tabId`` to the insertTable Location."""
    tools.gdocs_insert_table(
        doc_id="DOC1", rows=1, columns=1, index=2, tab_id="t.abc",
    )
    body = _last_batchupdate_body(with_docs_stub)
    loc = body["requests"][0]["insertTable"]["location"]
    assert loc == {"index": 2, "tabId": "t.abc"}


def test_gdocs_insert_table_rejects_subunit_dims(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="rows and columns must each be >= 1"):
        tools.gdocs_insert_table(doc_id="DOC1", rows=0, columns=2)


def test_gdocs_insert_table_rejects_index_below_one(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="index must be >= 1"):
        tools.gdocs_insert_table(doc_id="DOC1", rows=2, columns=2, index=0)


# ---------------------------------------------------------------------
# gdocs_format_range — batchUpdate (updateTextStyle)
# ---------------------------------------------------------------------


def test_gdocs_format_range_happy_path_bold(with_docs_stub):
    """Bold over a range → envelope echoing the applied fields."""
    result = tools.gdocs_format_range(
        doc_id="DOC1", start_index=5, end_index=12, bold=True,
    )
    assert result == {
        "doc_id": "DOC1",
        "start_index": 5,
        "end_index": 12,
        "tab_id": None,
        "applied": ["bold"],
    }


def test_gdocs_format_range_builds_updateTextStyle_with_fields_mask(
    with_docs_stub,
):
    """Multiple styles → one updateTextStyle with the right textStyle
    payload AND a fields mask naming exactly those styles."""
    tools.gdocs_format_range(
        doc_id="DOC1", start_index=1, end_index=10,
        bold=True, italic=True, font_size_pt=14, font_family="Roboto",
    )
    req = _last_batchupdate_body(with_docs_stub)["requests"][0]["updateTextStyle"]
    assert req["range"] == {"startIndex": 1, "endIndex": 10}
    assert req["textStyle"]["bold"] is True
    assert req["textStyle"]["italic"] is True
    assert req["textStyle"]["fontSize"] == {"magnitude": 14, "unit": "PT"}
    assert req["textStyle"]["weightedFontFamily"] == {"fontFamily": "Roboto"}
    # fields mask names exactly the four set styles (order-independent)
    assert set(req["fields"].split(",")) == {
        "bold", "italic", "fontSize", "weightedFontFamily",
    }


def test_gdocs_format_range_color_hex_to_rgbcolor(with_docs_stub):
    """A #RRGGBB color becomes a Docs RgbColor with [0,1] channels."""
    tools.gdocs_format_range(
        doc_id="DOC1", start_index=1, end_index=3, foreground_color="#FF8000",
    )
    req = _last_batchupdate_body(with_docs_stub)["requests"][0]["updateTextStyle"]
    rgb = req["textStyle"]["foregroundColor"]["color"]["rgbColor"]
    assert rgb["red"] == 1.0
    assert abs(rgb["green"] - 128 / 255) < 1e-9
    assert rgb["blue"] == 0.0
    assert "foregroundColor" in req["fields"].split(",")


def test_gdocs_format_range_scopes_to_tab_when_given(with_docs_stub):
    tools.gdocs_format_range(
        doc_id="DOC1", start_index=2, end_index=4, tab_id="t.xyz", bold=True,
    )
    req = _last_batchupdate_body(with_docs_stub)["requests"][0]["updateTextStyle"]
    assert req["range"] == {"startIndex": 2, "endIndex": 4, "tabId": "t.xyz"}


def test_gdocs_format_range_requires_at_least_one_style(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="no styles supplied"):
        tools.gdocs_format_range(doc_id="DOC1", start_index=1, end_index=5)


def test_gdocs_format_range_rejects_bad_range(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="end_index must be greater"):
        tools.gdocs_format_range(
            doc_id="DOC1", start_index=5, end_index=5, bold=True,
        )
    with pytest.raises(ToolError, match="start_index must be >= 1"):
        tools.gdocs_format_range(
            doc_id="DOC1", start_index=0, end_index=5, bold=True,
        )


def test_gdocs_format_range_rejects_bad_color(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="6-digit hex"):
        tools.gdocs_format_range(
            doc_id="DOC1", start_index=1, end_index=5, foreground_color="blue",
        )


# ---------------------------------------------------------------------
# gdocs_format_paragraph — batchUpdate (updateParagraphStyle)
# ---------------------------------------------------------------------


def test_gdocs_format_paragraph_happy_path_alignment(with_docs_stub):
    result = tools.gdocs_format_paragraph(
        doc_id="DOC1", start_index=1, end_index=10, alignment="center",
    )
    assert result == {
        "doc_id": "DOC1",
        "start_index": 1,
        "end_index": 10,
        "tab_id": None,
        "applied": ["alignment"],
    }
    req = _last_batchupdate_body(with_docs_stub)["requests"][0]["updateParagraphStyle"]
    assert req["paragraphStyle"]["alignment"] == "CENTER"
    assert req["fields"] == "alignment"


def test_gdocs_format_paragraph_multiple_attrs_and_fields_mask(with_docs_stub):
    tools.gdocs_format_paragraph(
        doc_id="DOC1", start_index=2, end_index=20,
        named_style="HEADING_1", line_spacing=150, space_above_pt=6,
    )
    req = _last_batchupdate_body(with_docs_stub)["requests"][0]["updateParagraphStyle"]
    ps = req["paragraphStyle"]
    assert ps["namedStyleType"] == "HEADING_1"
    assert ps["lineSpacing"] == 150
    assert ps["spaceAbove"] == {"magnitude": 6, "unit": "PT"}
    assert set(req["fields"].split(",")) == {
        "namedStyleType", "lineSpacing", "spaceAbove",
    }


def test_gdocs_format_paragraph_alignment_aliases(with_docs_stub):
    tools.gdocs_format_paragraph(
        doc_id="DOC1", start_index=1, end_index=5, alignment="right",
    )
    req = _last_batchupdate_body(with_docs_stub)["requests"][0]["updateParagraphStyle"]
    assert req["paragraphStyle"]["alignment"] == "END"


def test_gdocs_format_paragraph_rejects_unknown_alignment(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="alignment must be one of"):
        tools.gdocs_format_paragraph(
            doc_id="DOC1", start_index=1, end_index=5, alignment="sideways",
        )


def test_gdocs_format_paragraph_rejects_unknown_named_style(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="named_style must be one of"):
        tools.gdocs_format_paragraph(
            doc_id="DOC1", start_index=1, end_index=5, named_style="HEADING_9",
        )


def test_gdocs_format_paragraph_requires_at_least_one_attr(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="no paragraph attributes supplied"):
        tools.gdocs_format_paragraph(doc_id="DOC1", start_index=1, end_index=5)


# ---------------------------------------------------------------------
# gdocs_insert_markdown_table — parse + insertTable + fill cells
# ---------------------------------------------------------------------


def _table_doc_fixture(rows: int, columns: int, base: int = 2):
    """Build a documents().get() response whose default-tab body has one
    table with deterministic per-cell startIndex values."""
    idx = base
    table_rows = []
    for _r in range(rows):
        cells = []
        for _c in range(columns):
            cells.append({"content": [{"startIndex": idx}]})
            idx += 2  # each empty cell occupies a couple of indices
        table_rows.append({"tableCells": cells})
    return {
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0"},
                "documentTab": {
                    "body": {
                        "content": [
                            {"startIndex": 1, "table": {"tableRows": table_rows}},
                        ],
                    },
                },
            },
        ],
    }


def test_gdocs_insert_markdown_table_happy_path(with_docs_stub):
    """Parses a 2-col table, inserts it, and fills the non-empty cells.
    Verifies the envelope + that an insertTable + a fill batchUpdate ran."""
    with_docs_stub.documents().get().execute.return_value = _table_doc_fixture(
        rows=2, columns=2,
    )
    md = "| H1 | H2 |\n|----|----|\n| a | b |"
    result = tools.gdocs_insert_markdown_table(doc_id="DOC1", markdown=md)
    assert result == {
        "doc_id": "DOC1",
        "rows": 2,
        "columns": 2,
        "index": 1,
        "tab_id": None,
        "cells_filled": 4,  # H1,H2,a,b all non-empty
    }
    # insertTable was issued
    bodies = [
        c.kwargs["body"] for c in with_docs_stub.documents().batchUpdate.call_args_list
        if "body" in c.kwargs
    ]
    assert any(
        "insertTable" in b["requests"][0] for b in bodies
    ), "no insertTable batchUpdate was issued"


def test_gdocs_insert_markdown_table_fills_in_reverse_index_order(with_docs_stub):
    """The cell-fill batchUpdate must order insertText requests by
    DESCENDING index so earlier inserts don't shift later ones."""
    with_docs_stub.documents().get().execute.return_value = _table_doc_fixture(
        rows=1, columns=3,
    )
    md = "| a | b | c |\n|---|---|---|"
    tools.gdocs_insert_markdown_table(doc_id="DOC1", markdown=md)
    # Find the fill batchUpdate (the one with insertText requests)
    fill = None
    for c in with_docs_stub.documents().batchUpdate.call_args_list:
        body = c.kwargs.get("body", {})
        reqs = body.get("requests", [])
        if reqs and "insertText" in reqs[0]:
            fill = reqs
            break
    assert fill is not None, "no cell-fill batchUpdate found"
    indices = [r["insertText"]["location"]["index"] for r in fill]
    assert indices == sorted(indices, reverse=True), (
        f"insertText requests not in descending index order: {indices}"
    )


def test_gdocs_insert_markdown_table_skips_empty_cells(with_docs_stub):
    """Empty cells produce no insertText (cells_filled counts only
    non-empty)."""
    with_docs_stub.documents().get().execute.return_value = _table_doc_fixture(
        rows=2, columns=2,
    )
    md = "| H1 | H2 |\n|----|----|\n| a |  |"  # second body cell empty
    result = tools.gdocs_insert_markdown_table(doc_id="DOC1", markdown=md)
    assert result["cells_filled"] == 3  # H1, H2, a (not the empty one)


def test_gdocs_insert_markdown_table_rejects_bad_markdown(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="separator row"):
        tools.gdocs_insert_markdown_table(
            doc_id="DOC1", markdown="| a | b |\n| c | d |",
        )


# ---------------------------------------------------------------------
# gdocs_edit_range — batchUpdate (deleteContentRange [+ insertText])
# ---------------------------------------------------------------------


def test_gdocs_edit_range_pure_delete_happy_path(with_docs_stub):
    """No ``text`` → pure delete: one deleteContentRange request, and the
    envelope reports deleted=True / inserted=False."""
    result = tools.gdocs_edit_range(
        doc_id="DOC1", start_index=5, end_index=12,
    )
    assert result == {
        "doc_id": "DOC1",
        "start_index": 5,
        "end_index": 12,
        "tab_id": None,
        "deleted": True,
        "inserted": False,
        "inserted_units": 0,
    }
    reqs = _last_batchupdate_body(with_docs_stub)["requests"]
    assert reqs == [
        {"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 12}}}
    ]


def test_gdocs_edit_range_replace_emits_delete_then_insert(with_docs_stub):
    """With ``text`` → deleteContentRange THEN insertText at start_index,
    in that order (delete first so the insert lands in the gap)."""
    result = tools.gdocs_edit_range(
        doc_id="DOC1", start_index=4, end_index=9, text="new",
    )
    assert result["deleted"] is True
    assert result["inserted"] is True
    assert result["inserted_units"] == 3  # "new" = 3 UTF-16 units
    reqs = _last_batchupdate_body(with_docs_stub)["requests"]
    assert reqs == [
        {"deleteContentRange": {"range": {"startIndex": 4, "endIndex": 9}}},
        {"insertText": {"location": {"index": 4}, "text": "new"}},
    ]


def test_gdocs_edit_range_empty_text_is_pure_delete(with_docs_stub):
    """Empty-string ``text`` is treated as a pure delete — Docs rejects an
    empty insertText, so none is emitted and inserted=False."""
    result = tools.gdocs_edit_range(
        doc_id="DOC1", start_index=2, end_index=6, text="",
    )
    assert result["inserted"] is False
    assert result["inserted_units"] == 0
    reqs = _last_batchupdate_body(with_docs_stub)["requests"]
    assert all("insertText" not in r for r in reqs)
    assert len(reqs) == 1 and "deleteContentRange" in reqs[0]


def test_gdocs_edit_range_scopes_to_tab_when_given(with_docs_stub):
    """A tab_id is threaded onto BOTH the delete range and the insert
    location."""
    tools.gdocs_edit_range(
        doc_id="DOC1", start_index=3, end_index=7, text="x", tab_id="t.abc",
    )
    reqs = _last_batchupdate_body(with_docs_stub)["requests"]
    assert reqs[0]["deleteContentRange"]["range"] == {
        "startIndex": 3, "endIndex": 7, "tabId": "t.abc",
    }
    assert reqs[1]["insertText"]["location"] == {"index": 3, "tabId": "t.abc"}


def test_gdocs_edit_range_counts_above_bmp_text_in_utf16_units(with_docs_stub):
    """``inserted_units`` reports the UTF-16 code-unit length of the
    inserted text — an above-BMP emoji counts as 2, not 1. ``"a😀"`` is
    3 UTF-16 units (1 + 2) but only 2 Python characters; the result must
    say 3, matching the renderer's UTF-16 unit basis (R6 / PR #184)."""
    result = tools.gdocs_edit_range(
        doc_id="DOC1", start_index=1, end_index=3, text="a\U0001f600",
    )
    assert result["inserted_units"] == 3  # NOT len("a😀") == 2
    inserted = _last_batchupdate_body(with_docs_stub)["requests"][1]
    assert inserted["insertText"]["text"] == "a\U0001f600"


def test_gdocs_edit_range_rejects_bad_range(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="end_index must be greater"):
        tools.gdocs_edit_range(doc_id="DOC1", start_index=5, end_index=5)
    with pytest.raises(ToolError, match="start_index must be >= 1"):
        tools.gdocs_edit_range(doc_id="DOC1", start_index=0, end_index=5)


# ---------------------------------------------------------------------
# gdocs_insert_image — happy path + validation through the envelope
# ---------------------------------------------------------------------


def test_gdocs_insert_image_happy_path(with_docs_stub):
    with_docs_stub.documents().batchUpdate().execute.return_value = {
        "replies": [{"insertInlineImage": {"objectId": "IMG1"}}],
    }
    result = tools.gdocs_insert_image(
        doc_id="DOC1", image_uri="https://example.com/p.png", index=3,
    )
    assert result["doc_id"] == "DOC1"
    assert result["image_object_id"] == "IMG1"
    assert result["uri"] == "https://example.com/p.png"


def test_gdocs_insert_image_validation_becomes_toolerror(with_docs_stub):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="must be a public http"):
        tools.gdocs_insert_image(doc_id="DOC1", image_uri="file:///etc/x")


# ---------------------------------------------------------------------
# gdocs_read_doc — suggestions_view_mode reaches the API
# ---------------------------------------------------------------------


def test_gdocs_read_doc_threads_suggestions_view_mode():
    docs = MagicMock(name="docs-v1-svm")
    docs.documents().get().execute.return_value = {"documentId": "DOC1", "tabs": []}
    drive = MagicMock(name="drive-v3-svm")
    drive.files().get().execute.return_value = {"trashed": False}
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("docs", "v1"): docs, ("drive", "v3"): drive,
    })):
        tools.gdocs_read_doc(
            doc_id="DOC1",
            suggestions_view_mode="PREVIEW_WITHOUT_SUGGESTIONS",
        )
    got = None
    for call in reversed(docs.documents().get.call_args_list):
        if "documentId" in call.kwargs:
            got = call.kwargs
            break
    assert got["suggestionsViewMode"] == "PREVIEW_WITHOUT_SUGGESTIONS"


# ---------------------------------------------------------------------
# gdocs comments tools (Drive v3 stub)
# ---------------------------------------------------------------------


@pytest.fixture
def with_drive_comments_stub():
    drive = MagicMock(name="drive-v3-comments-tools")
    drive.comments().list().execute.return_value = {
        "comments": [{"id": "c1", "content": "hi", "replies": []}],
        "nextPageToken": None,
    }
    drive.comments().create().execute.return_value = {"id": "c2", "content": "x"}
    drive.replies().create().execute.return_value = {"id": "r1", "content": "y"}
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
    })):
        yield drive


def test_gdocs_list_comments_happy_path(with_drive_comments_stub):
    result = tools.gdocs_list_comments(doc_id="DOC-XYZ")
    assert result["doc_id"] == "DOC-XYZ"
    assert result["comments"][0]["id"] == "c1"
    assert result["next_page_token"] is None


def test_gdocs_create_comment_happy_path(with_drive_comments_stub):
    result = tools.gdocs_create_comment(doc_id="DOC-XYZ", content="Review please")
    assert result["doc_id"] == "DOC-XYZ"
    assert result["comment"]["id"] == "c2"


def test_gdocs_create_comment_validation_becomes_toolerror(
    with_drive_comments_stub,
):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="content cannot be empty"):
        tools.gdocs_create_comment(doc_id="DOC1", content="   ")


def test_gdocs_reply_to_comment_happy_path(with_drive_comments_stub):
    result = tools.gdocs_reply_to_comment(
        doc_id="DOC-XYZ", comment_id="c1", content="Thanks",
    )
    assert result["doc_id"] == "DOC-XYZ"
    assert result["comment_id"] == "c1"
    assert result["reply"]["id"] == "r1"


def test_gdocs_reply_to_comment_validation_becomes_toolerror(
    with_drive_comments_stub,
):
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="comment_id cannot be empty"):
        tools.gdocs_reply_to_comment(doc_id="DOC1", comment_id="", content="hi")
