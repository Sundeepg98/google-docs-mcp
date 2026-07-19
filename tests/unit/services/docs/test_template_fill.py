"""Discriminating tests for Wave 5 S1 - Docs template fill.

Covers the four single-request ``documents.batchUpdate`` builders
(create/replace/delete named range + insert page break) and the
``include_indices`` read enabler. Each request-shape assertion is exact
(tabId threading, selector shape, reply parsing) so a revert to a
different shape - or dropping the tabs-first thread - fails here.

The stub captures the ``batchUpdate(body=...)`` request list, mirroring
``test_api.py``'s ``_edit_range_docs_stub`` / ``_last_edit_batch_requests``
pattern. Reads additionally stub ``drive.files().get`` for the
``is_file_trashed`` lookup ``read_tab_content`` / ``read_all_tabs`` perform.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.docs.api import (
    create_named_range,
    delete_named_range,
    insert_page_break,
    read_all_tabs,
    read_tab_content,
    replace_named_range_content,
)


# ---------------------------------------------------------------------
# batchUpdate stub + request capture
# ---------------------------------------------------------------------


def _docs_stub(reply: dict | None = None):
    """A Docs v1 stub whose batchUpdate captures its request body."""
    docs = MagicMock(name="docs-v1")
    docs.documents().batchUpdate().execute.return_value = reply or {"replies": [{}]}
    client = InMemoryGoogleAPIClient({("docs", "v1"): docs})
    return docs, client


def _last_requests(docs: MagicMock) -> list[dict]:
    """The ``requests`` list of the most recent batchUpdate(body=...) call."""
    for call in reversed(docs.documents().batchUpdate.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs["body"]["requests"]
    raise AssertionError("no documents().batchUpdate(body=...) call captured")


# ---------------------------------------------------------------------
# create_named_range
# ---------------------------------------------------------------------


def test_create_named_range_builds_range_request_no_tab():
    """Default tab: the range carries startIndex/endIndex and NO tabId."""
    docs, client = _docs_stub(
        {"replies": [{"createNamedRange": {"namedRangeId": "nr.1"}}]}
    )
    with with_google_api_client(client):
        result = create_named_range(MagicMock(), "DOC1", "field1", 5, 12)
    assert _last_requests(docs) == [
        {"createNamedRange": {"name": "field1", "range": {"startIndex": 5, "endIndex": 12}}}
    ]
    assert result["named_range_id"] == "nr.1"
    assert result["tab_id"] is None


def test_create_named_range_threads_non_default_tab_id():
    """A non-default tab_id must appear INSIDE the range object (tabs-first
    seam). Dropping the thread would target the wrong tab."""
    docs, client = _docs_stub(
        {"replies": [{"createNamedRange": {"namedRangeId": "nr.2"}}]}
    )
    with with_google_api_client(client):
        create_named_range(MagicMock(), "DOC1", "f", 3, 8, tab_id="t.99")
    req = _last_requests(docs)[0]["createNamedRange"]
    assert req["range"] == {"startIndex": 3, "endIndex": 8, "tabId": "t.99"}


def test_create_named_range_parses_missing_reply_id_as_none():
    """A reply without a namedRangeId yields named_range_id=None, not a crash."""
    docs, client = _docs_stub({"replies": [{}]})
    with with_google_api_client(client):
        result = create_named_range(MagicMock(), "DOC1", "f", 1, 2)
    assert result["named_range_id"] is None


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(name="", start_index=1, end_index=2), "name cannot be empty"),
        (dict(name="f", start_index=0, end_index=2), "start_index must be >= 1"),
        (dict(name="f", start_index=5, end_index=5), "end_index must be greater"),
        (dict(name="f", start_index=5, end_index=3), "end_index must be greater"),
    ],
)
def test_create_named_range_validates(kwargs, match):
    """Client-side validation fires BEFORE any Google round-trip."""
    with pytest.raises(ValueError, match=match):
        create_named_range(MagicMock(), "DOC1", **kwargs)


def test_create_named_range_rejects_blank_tab_id():
    with pytest.raises(ValueError, match="tab_id cannot be the empty string"):
        create_named_range(MagicMock(), "DOC1", "f", 1, 2, tab_id="   ")


# ---------------------------------------------------------------------
# replace_named_range_content
# ---------------------------------------------------------------------


def test_replace_by_name_builds_request_all_tabs():
    """By name, no tab_ids -> namedRangeName + text, NO tabsCriteria; scope
    is 'all_tabs'."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        result = replace_named_range_content(
            MagicMock(), "DOC1", "FILLED", named_range_name="field1"
        )
    assert _last_requests(docs) == [
        {"replaceNamedRangeContent": {"text": "FILLED", "namedRangeName": "field1"}}
    ]
    assert result["selector"] == "named_range_name"
    assert result["selector_value"] == "field1"
    assert result["text_length"] == 6
    assert result["scope"] == "all_tabs"


def test_replace_by_id_builds_request():
    """By id -> namedRangeId + text; scope is None (an id is global)."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        result = replace_named_range_content(
            MagicMock(), "DOC1", "X", named_range_id="nr.7"
        )
    assert _last_requests(docs) == [
        {"replaceNamedRangeContent": {"text": "X", "namedRangeId": "nr.7"}}
    ]
    assert result["selector"] == "named_range_id"
    assert result["scope"] is None


def test_replace_by_name_scopes_tabs_criteria():
    """tab_ids with a name -> tabsCriteria{tabIds:[...]} and scope echoes the list."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        result = replace_named_range_content(
            MagicMock(), "DOC1", "V", named_range_name="f", tab_ids=["t.1", "t.2"]
        )
    req = _last_requests(docs)[0]["replaceNamedRangeContent"]
    assert req["tabsCriteria"] == {"tabIds": ["t.1", "t.2"]}
    assert result["scope"] == ["t.1", "t.2"]


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(), "exactly one"),
        (dict(named_range_name="a", named_range_id="b"), "exactly one"),
        (dict(named_range_id="b", tab_ids=["t.1"]), "applies only to named_range_name"),
        (dict(named_range_name="a", tab_ids=[]), "tab_ids list cannot be empty"),
    ],
)
def test_replace_validates_selector(kwargs, match):
    with pytest.raises(ValueError, match=match):
        replace_named_range_content(MagicMock(), "DOC1", "T", **kwargs)


# ---------------------------------------------------------------------
# delete_named_range
# ---------------------------------------------------------------------


def test_delete_by_id_builds_request():
    docs, client = _docs_stub()
    with with_google_api_client(client):
        result = delete_named_range(MagicMock(), "DOC1", named_range_id="nr.9")
    assert _last_requests(docs) == [{"deleteNamedRange": {"namedRangeId": "nr.9"}}]
    assert result["selector"] == "named_range_id"


def test_delete_by_name_uses_name_field():
    """Docs API asymmetry: DeleteNamedRangeRequest selects by ``name`` (NOT
    ``namedRangeName``, which is replaceNamedRangeContent's field). This is
    close-smoke step 6 (delete by name, no tabs); the wrong field 400s live."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        delete_named_range(MagicMock(), "DOC1", named_range_name="field1")
    assert _last_requests(docs) == [{"deleteNamedRange": {"name": "field1"}}]


def test_delete_by_name_scopes_tabs_criteria():
    """By-name delete with tabs -> ``name`` + tabsCriteria (again ``name``,
    not ``namedRangeName``)."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        delete_named_range(
            MagicMock(), "DOC1", named_range_name="field1", tab_ids=["t.3"]
        )
    assert _last_requests(docs) == [
        {"deleteNamedRange": {"name": "field1", "tabsCriteria": {"tabIds": ["t.3"]}}}
    ]


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(), "exactly one"),
        (dict(named_range_name="a", named_range_id="b"), "exactly one"),
        (dict(named_range_id="b", tab_ids=["t.1"]), "applies only to named_range_name"),
    ],
)
def test_delete_validates_selector(kwargs, match):
    with pytest.raises(ValueError, match=match):
        delete_named_range(MagicMock(), "DOC1", **kwargs)


# ---------------------------------------------------------------------
# insert_page_break
# ---------------------------------------------------------------------


def test_page_break_default_is_end_of_segment():
    """No index -> endOfSegmentLocation (arithmetic-free); empty {} when no tab."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        result = insert_page_break(MagicMock(), "DOC1")
    assert _last_requests(docs) == [{"insertPageBreak": {"endOfSegmentLocation": {}}}]
    assert result["location_mode"] == "end_of_segment"
    assert result["index"] is None


def test_page_break_end_of_segment_threads_tab():
    docs, client = _docs_stub()
    with with_google_api_client(client):
        insert_page_break(MagicMock(), "DOC1", tab_id="t.5")
    assert _last_requests(docs) == [
        {"insertPageBreak": {"endOfSegmentLocation": {"tabId": "t.5"}}}
    ]


def test_page_break_at_index_uses_location():
    """An explicit index -> location{index,(tabId)}; location_mode 'index'."""
    docs, client = _docs_stub()
    with with_google_api_client(client):
        result = insert_page_break(MagicMock(), "DOC1", index=17, tab_id="t.5")
    assert _last_requests(docs) == [
        {"insertPageBreak": {"location": {"index": 17, "tabId": "t.5"}}}
    ]
    assert result["location_mode"] == "index"
    assert result["index"] == 17


def test_page_break_validates():
    with pytest.raises(ValueError, match="index must be >= 1"):
        insert_page_break(MagicMock(), "DOC1", index=0)
    with pytest.raises(ValueError, match="tab_id cannot be the empty string"):
        insert_page_break(MagicMock(), "DOC1", tab_id="  ")


# ---------------------------------------------------------------------
# include_indices enabler (BOTH read paths)
# ---------------------------------------------------------------------


def _para_with_index(text: str, start: int, end: int, style: str = "NORMAL_TEXT") -> dict:
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": {"content": text}}],
        },
    }


def _tab(tab_id: str, title: str, content: list[dict]) -> dict:
    return {
        "tabProperties": {"tabId": tab_id, "title": title},
        "documentTab": {"body": {"content": content}},
    }


def _read_stubs(tabs: list[dict]):
    docs = MagicMock(name="docs-v1")
    docs.documents().get().execute.return_value = {"documentId": "DOC1", "tabs": tabs}
    drive = MagicMock(name="drive-v3")
    drive.files().get().execute.return_value = {"trashed": False}
    return InMemoryGoogleAPIClient({("docs", "v1"): docs, ("drive", "v3"): drive})


def test_read_tab_content_include_indices_adds_server_span():
    """With include_indices, each paragraph carries the element's server
    start_index/end_index (the span create_named_range consumes)."""
    tab = _tab("t.2", "Two", [_para_with_index("REPLACE_ME", 1, 12)])
    client = _read_stubs([tab])
    with with_google_api_client(client):
        result = read_tab_content(
            MagicMock(), "DOC1", tab_id="t.2", include_indices=True
        )
    para = result["paragraphs"][0]
    assert para["text"] == "REPLACE_ME"
    assert para["start_index"] == 1
    assert para["end_index"] == 12


def test_read_all_tabs_include_indices_adds_server_span():
    """The SAME server span appears via the bulk read path too."""
    tab = _tab("t.2", "Two", [_para_with_index("REPLACE_ME", 1, 12)])
    client = _read_stubs([tab])
    with with_google_api_client(client):
        result = read_all_tabs(MagicMock(), "DOC1", include_indices=True)
    para = result["tabs"][0]["paragraphs"][0]
    assert para["start_index"] == 1
    assert para["end_index"] == 12


def test_include_indices_default_off_omits_span_both_paths():
    """Default (flag off) keeps the legacy {style, text} shape - no index
    keys. Guards against include_indices becoming unconditionally on."""
    tab = _tab("t.2", "Two", [_para_with_index("hi", 1, 4)])

    client = _read_stubs([tab])
    with with_google_api_client(client):
        single = read_tab_content(MagicMock(), "DOC1", tab_id="t.2")
    assert set(single["paragraphs"][0]) == {"style", "text"}

    client = _read_stubs([tab])
    with with_google_api_client(client):
        bulk = read_all_tabs(MagicMock(), "DOC1")
    assert set(bulk["tabs"][0]["paragraphs"][0]) == {"style", "text"}
