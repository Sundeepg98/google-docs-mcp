"""Pipeline tests for the rewired convert_docx_to_tabbed_doc.

These replace the old /exec-POST mocks: step 5 is now the in-process
REST content transplant (``services/docs/content_transplant``), so the
pipeline is drivable end-to-end with a scripted fake Docs service and
monkeypatched Drive/tab-shell seams - no web app, no HMAC, no urlopen.

The load-bearing contracts pinned here:

1. Happy path - transplant requests land ONLY in the new tabs (tabId
   discipline), the response keeps the pre-rewire envelope (doc_id /
   url / action / tabs / moved_children / warnings / info /
   split_strategy_used), and fidelity lists are empty for a clean doc.
2. TRANSACTIONAL ORDER - every content write precedes the placeholder
   delete; icons precede the placeholder delete (the empirically
   confirmed 500-race order); the source tab is only carved/deleted
   after the verify pass.
3. FAILURE path - a mid-transplant error must never leave a
   shell-riddled half-converted doc: the new shells are deleted, the
   working copy is trashed, the placeholder tab and replace_doc_id are
   left untouched, and the error says so.
"""
from __future__ import annotations

import pytest

from appscriptly import docx_import
from appscriptly.docx_import import convert_docx_to_tabbed_doc

DOC_ID = "WORKING_COPY"
PRIMARY = "t.0"


# ---------------------------------------------------------------------
# Scripted fake Docs service + fixture documents
# ---------------------------------------------------------------------


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeDocs:
    def __init__(self, get_responses, events, fail_when=None):
        self._gets = list(get_responses)
        self.events = events
        self.batches: list[list[dict]] = []
        self.fail_when = fail_when

    def documents(self):
        return self

    def get(self, documentId, includeTabsContent=True):
        def _run():
            return self._gets[0] if len(self._gets) == 1 else self._gets.pop(0)

        return _Call(_run)

    def batchUpdate(self, documentId, body):
        def _run():
            requests = body["requests"]
            if self.fail_when is not None and self.fail_when(requests):
                raise RuntimeError("synthetic transplant failure")
            self.batches.append(requests)
            self.events.append(("batch", sorted({next(iter(r)) for r in requests})))
            return {}

        return _Call(_run)


def _para(text: str, style: str = "NORMAL_TEXT", start: int = 0, end: int = 0) -> dict:
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": {"content": text}}],
        },
    }


def _source_doc() -> dict:
    content = [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
        _para("Intro\n", "HEADING_1", 1, 7),
        _para("intro body\n", start=7, end=18),
        _para("Methods\n", "HEADING_1", 18, 26),
        _para("methods body\n", start=26, end=40),
    ]
    return {
        "tabs": [
            {
                "tabProperties": {"tabId": PRIMARY},
                "documentTab": {
                    "body": {"content": content},
                    "lists": {},
                    "inlineObjects": {},
                },
            }
        ]
    }


def _tab(tab_id: str, content: list[dict]) -> dict:
    return {
        "tabProperties": {"tabId": tab_id},
        "documentTab": {"body": {"content": content}},
    }


def _empty_shell() -> list[dict]:
    return [{"startIndex": 1, "endIndex": 2, "paragraph": {"elements": []}}]


def _filled(n: int) -> list[dict]:
    return [{"paragraph": {}} for _ in range(n)]


def _shells_doc() -> dict:
    source = _source_doc()["tabs"][0]
    return {"tabs": [source, _tab("t.1", _empty_shell()), _tab("t.2", _empty_shell())]}


def _verify_doc() -> dict:
    source = _source_doc()["tabs"][0]
    return {"tabs": [source, _tab("t.1", _filled(3)), _tab("t.2", _filled(3))]}


@pytest.fixture
def pipeline(monkeypatch):
    """Wire every seam of the pipeline to recording fakes; returns the
    mutable harness (events log + fake docs + knobs)."""
    events: list[tuple] = []
    harness: dict = {"events": events, "fail_when": None}

    fake_docs = _FakeDocs(
        [_source_doc(), _shells_doc(), _verify_doc()],
        events,
        fail_when=lambda reqs: harness["fail_when"] and harness["fail_when"](reqs),
    )
    harness["docs"] = fake_docs

    monkeypatch.setattr(
        docx_import,
        "upload_and_convert_docx",
        lambda creds, path, title=None: {
            "doc_id": DOC_ID,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
        },
    )
    monkeypatch.setattr(
        docx_import, "get_service", lambda *a, **k: fake_docs
    )

    def fake_add_tabs(creds, doc_id, tabs, parent_tab_id=None):
        events.append(("add_tabs", [t["title"] for t in tabs]))
        created = [
            {"title": t["title"], "tab_id": f"t.{i + 1}", "depth": 0, "parent_tab_id": None}
            for i, t in enumerate(tabs)
        ]
        return {"doc_id": doc_id, "url": "https://docs.google.com/x", "tabs": created}

    monkeypatch.setattr(docx_import, "add_tabs_to_doc", fake_add_tabs)
    monkeypatch.setattr(
        docx_import,
        "set_tab_icons",
        lambda creds, doc_id, icons: events.append(("icons", icons))
        or {"updated_count": len(icons), "matched": {}, "unmatched_titles": []},
    )
    monkeypatch.setattr(
        docx_import,
        "delete_tab",
        lambda creds, doc_id, tab_id: events.append(("delete_tab", tab_id)),
    )
    monkeypatch.setattr(
        docx_import,
        "rename_tab",
        lambda creds, doc_id, tab_id, title=None, icon_emoji=None: events.append(
            ("rename_tab", tab_id, title)
        ),
    )
    monkeypatch.setattr(
        docx_import,
        "trash_drive_file",
        lambda creds, file_id: events.append(("trash", file_id)),
    )
    monkeypatch.setattr(
        docx_import,
        "get_doc_outline",
        lambda creds, doc_id: {
            "doc_id": doc_id,
            "trashed": False,
            "tabs": [
                {"tab_id": "t.1", "title": "Intro", "parent_tab_id": None, "depth": 0, "index": 0, "icon_emoji": None},
                {"tab_id": "t.2", "title": "Methods", "parent_tab_id": None, "depth": 0, "index": 1, "icon_emoji": None},
            ],
        },
    )
    return harness


def _convert(**kwargs):
    from pathlib import Path

    return convert_docx_to_tabbed_doc(
        object(), docx_path=Path("fake.docx"), **kwargs
    )


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_happy_path_envelope_and_content(pipeline):
    result = _convert(icons_by_title={"Intro": "\U0001f4d1"})

    assert result["doc_id"] == DOC_ID
    assert result["action"] == "created"
    assert result["split_strategy_used"] == "heading_1"
    # 2 blocks (heading + body paragraph) per tab.
    assert result["moved_children"] == 4
    # Clean source: nothing dropped, nothing degraded.
    assert result["warnings"] == []
    assert result["info"] == []
    # Tabs come from the final outline refresh, with the legacy ``id``
    # alias intact.
    assert [t["title"] for t in result["tabs"]] == ["Intro", "Methods"]
    assert all(t["id"] == t["tab_id"] for t in result["tabs"])
    # The transplant actually moved the section text.
    inserted = "".join(
        r["insertText"]["text"]
        for batch in pipeline["docs"].batches
        for r in batch
        if "insertText" in r
    )
    assert "intro body" in inserted and "methods body" in inserted


def test_transplant_writes_only_into_new_tabs_never_the_source(pipeline):
    _convert()
    touched: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ("location", "range") and isinstance(v, dict):
                    touched.add(v.get("tabId", "MISSING"))
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(pipeline["docs"].batches)
    assert touched == {"t.1", "t.2"}


def test_transactional_order_content_then_icons_then_placeholder_delete(pipeline):
    _convert(icons_by_title={"Intro": "\U0001f4d1"})
    events = pipeline["events"]
    kinds = [e[0] for e in events]
    assert "delete_tab" in kinds and "icons" in kinds
    last_batch = max(i for i, k in enumerate(kinds) if k == "batch")
    assert last_batch < kinds.index("icons") < kinds.index("delete_tab")
    assert ("delete_tab", PRIMARY) in events


def test_rename_behavior_carves_source_ranges_then_renames(pipeline):
    _convert(placeholder_behavior="rename", placeholder_title="Overview")
    events = pipeline["events"]
    carve_batches = [
        b for b in pipeline["docs"].batches if "deleteContentRange" in b[0]
    ]
    assert len(carve_batches) == 1
    ranges = [r["deleteContentRange"]["range"] for r in carve_batches[0]]
    # Both section ranges, bottom-up, in the SOURCE tab, final span
    # capped one unit short of the body end.
    assert [r["tabId"] for r in ranges] == [PRIMARY, PRIMARY]
    assert ranges[0]["startIndex"] > ranges[1]["startIndex"]
    assert ranges[0]["endIndex"] == 39  # body ends at 40; last mark stays
    assert ("rename_tab", PRIMARY, "Overview") in events
    assert ("delete_tab", PRIMARY) not in events


def test_no_splits_returns_single_tab_note_without_touching_tabs(pipeline, monkeypatch):
    doc = _source_doc()
    for elem in doc["tabs"][0]["documentTab"]["body"]["content"]:
        para = elem.get("paragraph")
        if para:
            para["paragraphStyle"]["namedStyleType"] = "NORMAL_TEXT"
    pipeline["docs"]._gets = [doc]
    result = _convert()
    assert result["tabs"] == []
    assert "No split points found" in result["note"]
    assert all(e[0] != "add_tabs" for e in pipeline["events"])


def test_replace_doc_id_trashed_only_after_success(pipeline):
    result = _convert(replace_doc_id="OLD_DOC")
    assert ("trash", "OLD_DOC") in pipeline["events"]
    assert result["action"] == "replaced"
    assert result["replaced_doc_id"] == "OLD_DOC"


# ---------------------------------------------------------------------
# Failure path - the transactional contract
# ---------------------------------------------------------------------


def test_failed_transplant_rolls_back_shells_and_trashes_copy(pipeline):
    pipeline["fail_when"] = lambda reqs: any("insertText" in r for r in reqs)

    with pytest.raises(RuntimeError) as excinfo:
        _convert(icons_by_title={"Intro": "x"}, replace_doc_id="OLD_DOC")

    msg = str(excinfo.value)
    assert "source document is untouched" in msg

    events = pipeline["events"]
    # The new shells were deleted (rollback runs even though the
    # content batches failed).
    rollback_deletes = [
        r["deleteTab"]["tabId"]
        for batch in pipeline["docs"].batches
        for r in batch
        if "deleteTab" in r
    ]
    assert set(rollback_deletes) == {"t.1", "t.2"}
    # The working copy was trashed; the doc being replaced was NOT.
    assert ("trash", DOC_ID) in events
    assert ("trash", "OLD_DOC") not in events
    # Nothing downstream of the failure ran.
    assert all(e[0] not in ("icons", "delete_tab", "rename_tab") for e in events)


def test_failed_verify_also_rolls_back(pipeline):
    # Content lands, but the verify fetch shows an empty destination
    # tab (e.g. a silently mistargeted batch): same rollback contract.
    pipeline["docs"]._gets = [
        _source_doc(),
        _shells_doc(),
        {
            "tabs": [
                _source_doc()["tabs"][0],
                _tab("t.1", _empty_shell()),
                _tab("t.2", _filled(3)),
            ]
        },
    ]
    with pytest.raises(RuntimeError, match="untouched"):
        _convert()
    assert ("trash", DOC_ID) in pipeline["events"]
