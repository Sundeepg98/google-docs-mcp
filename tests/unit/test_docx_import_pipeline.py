"""Pipeline tests for the rewired convert_docx_to_tabbed_doc.

These replace the old /exec-POST mocks: step 5 is now the in-process
REST content transplant (``services/docs/content_transplant``), so the
pipeline is drivable end-to-end with a scripted fake Docs service and
monkeypatched Drive/tab-shell seams - no web app, no HMAC, no urlopen.

The load-bearing contracts pinned here (the 2026-07-10 order pin):

1. Happy path - transplant INSERTS land ONLY in the new tabs (tabId
   discipline), the response carries the full envelope (doc_id / url /
   action / on_conflict_action / tabs / moved_children / warnings /
   info / split_strategy_used / heading1_found / tabs_created /
   placeholder / completion manifest), and fidelity lists are empty
   for a clean doc.
2. PIPELINE ORDER (data safety, amended 2026-07-10) - transplant-all
   -> verify-all -> carve -> cosmetics -> placeholder handling LAST.
   The source tab is carved only after the verify pass; icons run
   after every data-safety step but BEFORE the placeholder step (a
   Google-side defect permanently breaks tab-property updates once
   the original first tab is deleted); icon failures downgrade to
   warnings and never block the placeholder step (S2.2: a cosmetic
   failure must never leave safety steps undone). A successful
   placeholder delete appends the tab-properties-locked advisory.
3. FAILURE path (S2.5) - once content has started moving, a failure
   KEEPS everything: no shell rollback, no trash, nothing carved, and
   the partial response's completion manifest says exactly which
   sections are verified in their tabs (moved_sections) vs existing
   ONLY inside the placeholder tab (pending_sections). A failure
   before any content write still rolls back + trashes (nothing of
   value exists yet).
4. on_conflict - new never looks; skip returns the existing doc
   without importing; replace trashes the prior same-title doc only
   AFTER a fully successful build.
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
        self.get_count = 0
        self.fail_get_at: int | None = None  # 0-based get() call index

    def documents(self):
        return self

    def get(self, documentId, includeTabsContent=True):
        def _run():
            index = self.get_count
            self.get_count += 1
            if self.fail_get_at is not None and index == self.fail_get_at:
                raise RuntimeError("synthetic fetch failure")
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


def _named_styles_sheet() -> dict:
    """A realistic named-style sheet. Every real Google Doc carries the
    built-in style definitions, so a source read always surfaces one; the
    converter re-emits it per destination tab (updateNamedStyle) to carry
    custom heading / text looks. Its ABSENCE is the E2 silent-loss signal
    the fidelity warning now guards, so the pipeline fixtures include a
    sheet to stay faithful to the real documents.get shape."""
    return {
        "styles": [
            {
                "namedStyleType": "HEADING_1",
                "textStyle": {
                    "bold": True,
                    "fontSize": {"magnitude": 20, "unit": "PT"},
                },
                "paragraphStyle": {},
            },
        ]
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
                    "namedStyles": _named_styles_sheet(),
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

    def fake_upload(creds, path, title=None):
        events.append(("upload", title))
        return {
            "doc_id": DOC_ID,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
            "title": title or path.stem,
        }

    monkeypatch.setattr(docx_import, "upload_and_convert_docx", fake_upload)
    monkeypatch.setattr(
        docx_import, "get_service", lambda *a, **k: fake_docs
    )
    # on_conflict seam: default = no same-title doc exists. Tests that
    # exercise skip/replace override ``harness["title_matches"]``.
    harness["title_matches"] = []
    harness["find_calls"] = []

    def fake_find(creds, query, *, exact=False, **kwargs):
        harness["find_calls"].append((query, exact))
        return {"matches": list(harness["title_matches"]),
                "count": len(harness["title_matches"])}

    monkeypatch.setattr(docx_import, "find_doc_by_title", fake_find)

    def fake_add_tabs(creds, doc_id, tabs, parent_tab_id=None):
        # Mirror the real add_tabs_to_doc contract: nested TabSpecs come
        # back as ONE pre-order flat list carrying depth/parent_tab_id
        # (that ordering is what the transplant zips against).
        events.append(("add_tabs", [t["title"] for t in tabs]))
        harness.setdefault("added_specs", []).append(tabs)
        from appscriptly.services.docs.tab_tree import _flatten_tab_tree

        path_ids: dict[tuple, str] = {}
        created = []
        for i, (depth, path, spec) in enumerate(_flatten_tab_tree(tabs)):
            tab_id = f"t.{i + 1}"
            path_ids[path] = tab_id
            created.append(
                {
                    "title": spec["title"],
                    "tab_id": tab_id,
                    "depth": depth,
                    "parent_tab_id": path_ids[path[:-1]] if depth > 0 else None,
                }
            )
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
    # Clean source: nothing dropped, nothing degraded. The ONE warning
    # is the policy advisory that a deleted original first tab locks
    # tab-property edits (Google-side defect) - it fires on every
    # successful placeholder delete, clean doc or not.
    assert len(result["warnings"]) == 1
    assert "tab icons and tab titles can no longer be edited" in result["warnings"][0]
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
    # T2.1 response contract: detected-vs-created echo + placeholder
    # outcome + conflict action.
    assert result["heading1_found"] == 2
    assert result["tabs_created"] == 2
    assert result["placeholder"] == "deleted"
    assert result["on_conflict_action"] == "created"
    # The completion manifest: every step done (in the amended order:
    # cosmetics before placeholder), every section verified moved,
    # nothing pending.
    assert result["completion"]["steps_completed"] == [
        "import", "shells", "transplant", "verify",
        "carve", "cosmetics", "placeholder",
    ]
    assert result["completion"]["moved_sections"] == ["Intro", "Methods"]
    assert result["completion"]["pending_sections"] == []
    assert "error" not in result


def test_transplant_inserts_only_into_new_tabs_never_the_source(pipeline):
    """Content INSERTS carry only new-tab tabIds. The carve (a
    deleteContentRange) targets the source tab BY DESIGN - it is the
    post-verify removal of the moved originals, not a write."""
    _convert()
    touched: set[str] = set()
    carve_targets: set[str] = set()

    def walk(value, in_carve):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ("location", "range") and isinstance(v, dict):
                    (carve_targets if in_carve else touched).add(
                        v.get("tabId", "MISSING")
                    )
                walk(v, in_carve or k == "deleteContentRange")
        elif isinstance(value, list):
            for v in value:
                walk(v, in_carve)

    walk(pipeline["docs"].batches, False)
    assert touched == {"t.1", "t.2"}
    assert carve_targets == {PRIMARY}


def _index_of_batch(events, request_kind):
    """Position of the first fake-docs batch containing request_kind."""
    for i, e in enumerate(events):
        if e[0] == "batch" and request_kind in e[1]:
            return i
    raise AssertionError(f"no batch with {request_kind} in {events!r}")


def test_pipeline_order_content_carve_icons_then_placeholder_last(pipeline):
    """The amended order pin: every content write precedes the carve;
    the carve precedes the icons (all DATA-safety steps done before any
    cosmetic); and icons precede the placeholder delete - Google
    permanently 500s tab-property updates once the original first tab
    is deleted, so icons must land while it still exists."""
    result = _convert(icons_by_title={"Intro": "\U0001f4d1"})
    events = pipeline["events"]
    kinds = [e[0] for e in events]
    assert "delete_tab" in kinds and "icons" in kinds
    carve_at = _index_of_batch(events, "deleteContentRange")
    last_insert = max(
        i for i, e in enumerate(events)
        if e[0] == "batch" and "insertText" in e[1]
    )
    assert last_insert < carve_at < kinds.index("icons") < kinds.index("delete_tab")
    assert ("delete_tab", PRIMARY) in events
    assert result["placeholder"] == "deleted"


def test_delete_behavior_carves_before_deleting_the_placeholder(pipeline):
    """``delete`` now carves the moved ranges FIRST, so a failed tab
    removal strands an EMPTY tab (cosmetic) instead of a full copy
    (the confusing pre-pin stray-Tab-1 state)."""
    _convert()
    carve_batches = [
        b for b in pipeline["docs"].batches if "deleteContentRange" in b[0]
    ]
    assert len(carve_batches) == 1
    assert all(
        r["deleteContentRange"]["range"]["tabId"] == PRIMARY
        for r in carve_batches[0]
    )
    assert ("delete_tab", PRIMARY) in pipeline["events"]


def test_rename_behavior_carves_source_ranges_then_renames(pipeline):
    result = _convert(placeholder_behavior="rename", placeholder_title="Overview")
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
    assert result["placeholder"] == "renamed"
    assert "placeholder" in result["completion"]["steps_completed"]
    # rename keeps the original first tab, so tab-property edits keep
    # working: no tab-properties-locked advisory.
    assert not any("can no longer be edited" in w for w in result["warnings"])


def test_keep_policy_reports_kept_without_veto_marker(pipeline):
    """Explicit keep is a POLICY, not a refusal: placeholder='kept' with
    NO placeholder_veto marker - that field is reserved for the
    sole-copy guard overriding a requested delete (R1), and leaking it
    onto policy keeps would make the two indistinguishable again."""
    result = _convert(placeholder_behavior="keep")
    assert result["placeholder"] == "kept"
    assert "placeholder_veto" not in result
    assert ("delete_tab", PRIMARY) not in pipeline["events"]


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
    # The manifest travels on EVERY response - here with nothing
    # pending (a single-tab import has no placeholder duplication).
    assert result["heading1_found"] == 0
    assert result["tabs_created"] == 0
    assert result["placeholder"] == "none"
    assert result["completion"]["pending_sections"] == []
    assert set(result["completion"]["steps_completed"]) == {
        "import", "shells", "transplant", "verify",
        "carve", "placeholder", "cosmetics",
    }


def test_no_splits_never_replaces_the_prior_version(pipeline):
    """A retry that found no split points must NOT trash the previous
    (real, tabbed) document - neither via replace_doc_id nor via
    on_conflict=replace."""
    doc = _source_doc()
    for elem in doc["tabs"][0]["documentTab"]["body"]["content"]:
        para = elem.get("paragraph")
        if para:
            para["paragraphStyle"]["namedStyleType"] = "NORMAL_TEXT"
    pipeline["docs"]._gets = [doc]
    result = _convert(replace_doc_id="OLD_DOC")
    assert ("trash", "OLD_DOC") not in pipeline["events"]
    assert result["action"] == "created"
    assert any("NOT replaced" in n for n in result["info"])


def test_replace_doc_id_trashed_only_after_success(pipeline):
    result = _convert(replace_doc_id="OLD_DOC")
    assert ("trash", "OLD_DOC") in pipeline["events"]
    assert result["action"] == "replaced"
    assert result["replaced_doc_id"] == "OLD_DOC"


# ---------------------------------------------------------------------
# Failure path - the S2.5 keep-everything contract
# ---------------------------------------------------------------------


def _assert_placeholder_untouched(pipeline):
    """The sole-copy guarantee: nothing was carved out of the source
    tab, no tab was deleted, and the working copy was not trashed - so
    the placeholder tab still holds every byte of the original content.
    """
    all_requests = [r for batch in pipeline["docs"].batches for r in batch]
    assert not any("deleteContentRange" in r for r in all_requests), (
        "a carve ran on the failure path - the placeholder no longer "
        "holds the sole copy"
    )
    assert not any("deleteTab" in r for r in all_requests)
    events = pipeline["events"]
    assert ("trash", DOC_ID) not in events
    assert all(
        e[0] not in ("icons", "delete_tab", "rename_tab") for e in events
    )


def test_s2_5_death_mid_transplant_keeps_sole_copy_and_marks_all_pending(pipeline):
    """THE S2.5 scenario: the process 'dies' (first content batch
    raises) after the transplant started, with NOTHING yet copied.
    Contract: the doc is KEPT (no rollback, no trash, no carve - the
    placeholder still holds the only copy of every section) and the
    returned manifest marks every section pending so no tool deletes
    the placeholder."""
    pipeline["fail_when"] = lambda reqs: any("insertText" in r for r in reqs)
    # Classification re-fetch sees the still-empty shells.
    pipeline["docs"]._gets = [_source_doc(), _shells_doc(), _shells_doc()]

    result = _convert(icons_by_title={"Intro": "x"}, replace_doc_id="OLD_DOC")

    assert "pending_sections" in result["error"]
    assert result["doc_id"] == DOC_ID
    assert result["placeholder"] == "kept"
    assert result["completion"]["steps_completed"] == ["import", "shells"]
    assert result["completion"]["moved_sections"] == []
    assert result["completion"]["pending_sections"] == ["Intro", "Methods"]
    assert result["heading1_found"] == 2
    assert result["tabs_created"] == 2
    _assert_placeholder_untouched(pipeline)
    # The prior version is NOT replaced on a failed build.
    assert ("trash", "OLD_DOC") not in pipeline["events"]


def test_partial_transplant_classifies_moved_vs_pending(pipeline):
    """A death midway (first section landed, second did not): the
    manifest's moved/pending split reflects a fresh per-tab verify, so
    a consumer knows exactly which sections are safe."""
    pipeline["fail_when"] = lambda reqs: any(
        "methods" in r.get("insertText", {}).get("text", "") for r in reqs
    )
    pipeline["docs"]._gets = [
        _source_doc(),
        _shells_doc(),
        # Classification fetch: Intro's tab filled, Methods' still empty.
        {
            "tabs": [
                _source_doc()["tabs"][0],
                _tab("t.1", _filled(3)),
                _tab("t.2", _empty_shell()),
            ]
        },
    ]

    result = _convert()

    assert result["completion"]["moved_sections"] == ["Intro"]
    assert result["completion"]["pending_sections"] == ["Methods"]
    # Execution died mid-way: "transplant" must NOT be claimed
    # (executed 1 of 2 plans).
    assert result["completion"]["steps_completed"] == ["import", "shells"]
    assert result["placeholder"] == "kept"
    _assert_placeholder_untouched(pipeline)


def test_failed_verify_keeps_doc_and_reports_unverified_as_pending(pipeline):
    """A verify failure (content landed but a destination tab reads
    short - e.g. a mistargeted batch) keeps the doc and reports the
    unverified section pending. NOTHING is carved: carve strictly
    follows verify-all. EXECUTED vs VERIFIED stays unambiguous (review
    finding on the #226 interim manifest): every plan finished
    executing, so "transplant" IS claimed, while "verify" is absent
    and the unverified section is pending - this can only read as a
    verify shortfall, never as "fully transplanted"."""
    bad_verify = {
        "tabs": [
            _source_doc()["tabs"][0],
            _tab("t.1", _empty_shell()),
            _tab("t.2", _filled(3)),
        ]
    }
    pipeline["docs"]._gets = [_source_doc(), _shells_doc(), bad_verify]

    result = _convert()

    assert result["completion"]["moved_sections"] == ["Methods"]
    assert result["completion"]["pending_sections"] == ["Intro"]
    assert result["completion"]["steps_completed"] == [
        "import", "shells", "transplant",
    ]
    assert "error" in result
    _assert_placeholder_untouched(pipeline)


def test_failure_before_any_content_write_still_cleans_up_staging(pipeline):
    """A failure BEFORE the first content batch (the shells-state fetch
    dies) cannot have moved anything - the empty shells are removed and
    the working copy is trashed. The error NAMES the trashed doc_id and
    says it is recoverable from Drive trash (review finding: for an
    HTTP upload the staging copy may be the only Drive-side copy, so
    'the source is untouched' alone was misleading - the caller must be
    able to find and untrash the staging doc)."""
    pipeline["docs"].fail_get_at = 1  # 0=source fetch, 1=shells-state fetch

    with pytest.raises(RuntimeError, match="Drive trash") as excinfo:
        _convert()

    msg = str(excinfo.value)
    assert DOC_ID in msg
    assert "recoverable" in msg

    rollback_deletes = [
        r["deleteTab"]["tabId"]
        for batch in pipeline["docs"].batches
        for r in batch
        if "deleteTab" in r
    ]
    assert set(rollback_deletes) == {"t.1", "t.2"}
    assert ("trash", DOC_ID) in pipeline["events"]


def test_classification_fetch_failure_reports_everything_pending(pipeline):
    """If even the post-failure classification fetch dies, the manifest
    must under-promise: every section pending, none claimed moved."""
    pipeline["fail_when"] = lambda reqs: any("insertText" in r for r in reqs)
    pipeline["docs"]._gets = [_source_doc(), _shells_doc()]
    pipeline["docs"].fail_get_at = 2  # the classification fetch

    result = _convert()

    assert result["completion"]["moved_sections"] == []
    assert result["completion"]["pending_sections"] == ["Intro", "Methods"]


# ---------------------------------------------------------------------
# Cosmetics are warnings-only and NEVER precede safety steps (S2.2)
# ---------------------------------------------------------------------


def test_icons_failure_is_nonfatal_and_never_strands_safety_steps(
    pipeline, monkeypatch
):
    """An icons failure (the S2.2 class) downgrades to a warning: the
    conversion still succeeds, every data-safety step already ran, and
    the PLACEHOLDER step still runs AFTER the failed cosmetic (a
    cosmetic failure must never strand it). The manifest omits only
    the cosmetics step."""
    def exploding_icons(creds, doc_id, icons):
        pipeline["events"].append(("icons", icons))
        raise RuntimeError("Google 500: icon batch rejected")

    monkeypatch.setattr(docx_import, "set_tab_icons", exploding_icons)

    result = _convert(icons_by_title={"Intro": "\U0001f4d1"})

    assert "error" not in result
    assert result["placeholder"] == "deleted"
    assert result["completion"]["pending_sections"] == []
    steps = result["completion"]["steps_completed"]
    assert "cosmetics" not in steps
    assert {"transplant", "verify", "carve", "placeholder"} <= set(steps)
    assert any("could not apply tab icons" in w for w in result["warnings"])
    assert result["icons"] == {"error": "Google 500: icon batch rejected"}
    # Amended order: icons attempted first, and the placeholder delete
    # STILL happened after the cosmetic failure.
    kinds = [e[0] for e in pipeline["events"]]
    assert kinds.index("icons") < kinds.index("delete_tab")


# ---------------------------------------------------------------------
# T2.2 - the stray-Tab-1 class: deterministic, reported outcomes
# ---------------------------------------------------------------------


def test_delete_failure_reports_kept_placeholder_with_warning(
    pipeline, monkeypatch
):
    """The residual stray-Tab-1 source at 83e5180: deleteTab failing
    after a heavy batch left an UNREPORTED full-copy Tab 1. Now the
    carve has already emptied the tab (stray is cosmetic, not a copy)
    and the outcome is REPORTED: placeholder='kept' + warning + the
    placeholder step absent from the manifest."""
    def exploding_delete(creds, doc_id, tab_id):
        raise RuntimeError("Google 500: tab busy")

    monkeypatch.setattr(docx_import, "delete_tab", exploding_delete)

    result = _convert()

    assert result["placeholder"] == "kept"
    assert any("could not delete placeholder tab" in w for w in result["warnings"])
    assert "placeholder" not in result["completion"]["steps_completed"]
    # The carve DID run first - the stray tab is empty, not a full copy.
    assert any(
        "deleteContentRange" in b[0] for b in pipeline["docs"].batches
    )
    # Everything else is intact: all sections verified moved.
    assert result["completion"]["pending_sections"] == []
    # The tab still exists, so tab-property edits still work: the
    # tab-properties-locked advisory must NOT fire.
    assert not any("can no longer be edited" in w for w in result["warnings"])


def test_placeholder_outcome_is_deterministic_across_identical_runs(pipeline):
    """Same inputs, same outcome, same report - twice. The T2.2
    'one doc in four had a stray Tab 1' class is only acceptable when
    the outcome is a pure function of inputs + reported per response."""
    first = _convert()
    # Reset the scripted world to an identical fresh state.
    pipeline["docs"]._gets = [_source_doc(), _shells_doc(), _verify_doc()]
    pipeline["docs"].get_count = 0
    second = _convert()
    assert first["placeholder"] == second["placeholder"] == "deleted"
    assert first["completion"] == second["completion"]


def test_placeholder_default_is_delete_on_pipeline_tool_and_retrofit():
    """Default parity regression (T2.2): the operator-observed
    nondeterminism traced to path defaults drifting apart (endpoint
    era-default 'keep' vs tool 'delete'). Pin every entry point to
    'delete' so they can never drift again. (The HTTP route's form
    default is pinned in the route tests.)"""
    import inspect

    from appscriptly.docx_import import convert_docx_to_tabbed_doc
    from appscriptly.retrofit import retrofit_existing_docx
    from appscriptly.services.docs import tools

    for fn in (
        convert_docx_to_tabbed_doc,
        retrofit_existing_docx,
        getattr(tools.gdocs_tab_existing_doc, "fn", tools.gdocs_tab_existing_doc),
    ):
        default = inspect.signature(fn).parameters["placeholder_behavior"].default
        assert default == "delete", f"{fn} placeholder_behavior default drifted"


# ---------------------------------------------------------------------
# Preamble safety - content before the first split point
# ---------------------------------------------------------------------


def test_delete_downgrades_to_kept_when_unmoved_content_remains(pipeline):
    """Content BEFORE the first split heading belongs to no section
    range: it is never moved to any tab, so deleting the placeholder
    would destroy its only copy. The delete policy must refuse and
    report 'kept'."""
    preamble_doc = _source_doc()
    body = preamble_doc["tabs"][0]["documentTab"]["body"]["content"]
    body.insert(1, _para("unassigned preamble\n", start=0, end=0))
    pipeline["docs"]._gets = [preamble_doc, _shells_doc(), _verify_doc()]

    result = _convert()  # placeholder_behavior defaults to "delete"

    assert result["placeholder"] == "kept"
    assert ("delete_tab", PRIMARY) not in pipeline["events"]
    assert any("never moved into any tab" in w for w in result["warnings"])
    assert "placeholder" not in result["completion"]["steps_completed"]
    # The moved sections themselves are fine.
    assert result["completion"]["pending_sections"] == []


# ---------------------------------------------------------------------
# on_conflict - new | replace | skip
# ---------------------------------------------------------------------


def test_on_conflict_new_never_looks_up_titles(pipeline):
    result = _convert()  # on_conflict defaults to "new"
    assert pipeline["find_calls"] == []
    assert result["on_conflict_action"] == "created"
    assert result["action"] == "created"


def test_on_conflict_skip_returns_existing_doc_without_importing(pipeline):
    pipeline["title_matches"] = [
        {
            "file_id": "EXISTING",
            "name": "fake",
            "mimeType": "application/vnd.google-apps.document",
        }
    ]
    result = _convert(on_conflict="skip")

    assert result["action"] == "skipped"
    assert result["on_conflict_action"] == "skipped"
    assert result["doc_id"] == "EXISTING"
    # NOTHING ran: no import, no shells, no writes, no trash.
    assert all(e[0] not in ("upload", "add_tabs", "trash") for e in pipeline["events"])
    assert pipeline["docs"].batches == []
    assert result["completion"]["pending_sections"] == []
    # The lookup was an exact-title query.
    assert pipeline["find_calls"] == [("fake", True)]


def test_on_conflict_skip_proceeds_normally_when_no_match(pipeline):
    result = _convert(on_conflict="skip")  # title_matches is empty
    assert result["action"] == "created"
    assert result["doc_id"] == DOC_ID
    assert ("upload", None) in pipeline["events"]


def test_on_conflict_replace_trashes_prior_only_after_success(pipeline):
    pipeline["title_matches"] = [
        {
            "file_id": "PRIOR",
            "name": "fake",
            "mimeType": "application/vnd.google-apps.document",
        }
    ]
    result = _convert(on_conflict="replace")

    events = pipeline["events"]
    assert ("trash", "PRIOR") in events
    # The trash happened strictly AFTER the last content batch (the
    # build is complete before the prior version goes away).
    kinds = [e[0] for e in events]
    last_batch = max(i for i, k in enumerate(kinds) if k == "batch")
    assert last_batch < events.index(("trash", "PRIOR"))
    assert result["on_conflict_action"] == "replaced"
    assert result["action"] == "replaced"
    assert result["replaced_doc_id"] == "PRIOR"


def _wire_drive_source(monkeypatch, mime):
    """Route the converter's drive_file_id branch into the harness.

    The fakes mirror the REAL import helpers' post-N8 title rules: an
    explicit title verbatim; the gdoc-copy fallback suffixed."""
    monkeypatch.setattr(
        docx_import, "classify_drive_file", lambda creds, fid: mime,
    )
    monkeypatch.setattr(
        docx_import, "copy_google_doc",
        lambda creds, fid, title=None: {
            "doc_id": DOC_ID,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
            "title": title or "copied doc (tabified)",
        },
    )
    monkeypatch.setattr(
        docx_import, "fetch_and_convert_drive_docx",
        lambda creds, fid, title=None: {
            "doc_id": DOC_ID,
            "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
            "title": title or "fetched doc",
        },
    )


def _wire_drive_meta(monkeypatch, pipeline, *, name, mime):
    """Serve drive.files().get metadata (what _expected_final_title
    reads) while keeping the docs service on the pipeline fake."""
    class _FakeDrive:
        def files(self):
            return self

        def get(self, fileId, fields):  # noqa: N803 - Google API casing
            return _Call(lambda: {"name": name, "mimeType": mime})

    fake_drive = _FakeDrive()
    docs_fake = pipeline["docs"]
    monkeypatch.setattr(
        docx_import, "get_service",
        lambda kind, version, credentials=None: (
            fake_drive if kind == "drive" else docs_fake
        ),
    )


@pytest.mark.parametrize("source_mime_attr", ["GDOC_MIME", "DOCX_MIME"])
def test_drive_entry_defaults_placeholder_delete_like_upload(
    pipeline, monkeypatch, source_mime_attr
):
    """R1 (2026-07-10 retest 2): the convert-from-Drive entry must apply
    the SAME placeholder default (delete) as the upload entry - the
    T2.2 determinism contract covers every entry point. Covers both
    drive source kinds (Google Doc copy + raw .docx fetch+convert)."""
    _wire_drive_source(monkeypatch, getattr(docx_import, source_mime_attr))
    result = convert_docx_to_tabbed_doc(object(), drive_file_id="SRC1")
    assert result["placeholder"] == "deleted", result
    assert ("delete_tab", PRIMARY) in pipeline["events"]


def test_upload_entry_defaults_placeholder_delete(pipeline):
    """Companion to the drive-entry default test: the upload entry's
    default, asserted explicitly (the happy-path test also covers it
    implicitly). The HTTP layer's default for the upload/batch/drive
    form modes is pinned separately in test_convert_job_model.py."""
    result = _convert()
    assert result["placeholder"] == "deleted"


# ---------------------------------------------------------------------
# N8 (retest 3) - explicit titles verbatim + cross-entry on_conflict
# ---------------------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("Retest3 Multi", "Retest3 Multi"),
    (None, "Retest3 Source (tabified)"),
])
def test_expected_final_title_gdoc_suffixes_only_the_fallback(
    pipeline, monkeypatch, title, expected
):
    """N8 root cause: the on_conflict lookup title for a gdoc source
    suffixed EXPLICIT titles too. Explicit = verbatim; only the
    no-title fallback carries " (tabified)" (so the working copy does
    not shadow the source doc's own name)."""
    _wire_drive_meta(
        monkeypatch, pipeline, name="Retest3 Source",
        mime=docx_import.GDOC_MIME,
    )
    got = docx_import._expected_final_title(object(), None, "SRC1", title)
    assert got == expected


def test_expected_final_title_docx_source_never_suffixes(
    pipeline, monkeypatch
):
    """The .docx drive source never carried the suffix; pin both title
    modes so the two branches cannot drift apart again."""
    _wire_drive_meta(
        monkeypatch, pipeline, name="Retest3 Source.docx",
        mime=docx_import.DOCX_MIME,
    )
    assert docx_import._expected_final_title(
        object(), None, "SRC1", "Retest3 Multi"
    ) == "Retest3 Multi"
    assert docx_import._expected_final_title(
        object(), None, "SRC1", None
    ) == "Retest3 Source"


@pytest.mark.parametrize("entry", ["upload", "drive_gdoc", "drive_docx"])
@pytest.mark.parametrize("conflict", ["skip", "replace"])
def test_on_conflict_matrix_identical_across_entry_points(
    pipeline, monkeypatch, entry, conflict
):
    """N8 (retest 3), the tester's exact repro as a matrix: with an
    EXPLICIT title, on_conflict must behave identically on every entry
    point against the same prior. Pre-fix, the drive path looked up the
    SUFFIXED title: skip returned created while an upload-path doc with
    the requested title was live, and replace trashed only the suffixed
    doc. The load-bearing assertion is the LOOKUP QUERY: the final
    effective title, verbatim, on every entry.

    'upload' here also covers the HTTP batch entry: batch fans into
    per-file converter calls identical to this one (explicit titles are
    rejected on batch; per-file stem titles take this same code path).
    """
    pipeline["title_matches"] = [
        {"file_id": "PRIOR_UPLOAD", "name": "Retest3 Multi",
         "mimeType": "application/vnd.google-apps.document"},
    ]

    if entry == "upload":
        result = _convert(title="Retest3 Multi", on_conflict=conflict)
    else:
        mime = (
            docx_import.GDOC_MIME if entry == "drive_gdoc"
            else docx_import.DOCX_MIME
        )
        _wire_drive_source(monkeypatch, mime)
        _wire_drive_meta(
            monkeypatch, pipeline, name="Retest3 Source", mime=mime,
        )
        result = convert_docx_to_tabbed_doc(
            object(), drive_file_id="SRC1",
            title="Retest3 Multi", on_conflict=conflict,
        )

    # The lookup ran against the FINAL effective title = the explicit
    # title VERBATIM - never the suffixed variant - on every entry.
    assert ("Retest3 Multi", True) in pipeline["find_calls"]
    assert not any("(tabified)" in q for q, _ in pipeline["find_calls"])

    if conflict == "skip":
        assert result["on_conflict_action"] == "skipped"
        assert result["doc_id"] == "PRIOR_UPLOAD"
        assert all(e[0] != "trash" for e in pipeline["events"])
    else:
        assert result["on_conflict_action"] == "replaced"
        assert result["replaced_doc_ids"] == ["PRIOR_UPLOAD"]
        assert ("trash", "PRIOR_UPLOAD") in pipeline["events"]


def test_drive_doc_with_preamble_keeps_placeholder_with_explicit_veto(
    pipeline, monkeypatch
):
    """The R1 field observation, reproduced mechanically: a drive-sourced
    Google Doc whose body carries visible content BEFORE the first
    Heading 1 (title line, intro paragraph - common in hand-authored
    docs) gets placeholder='kept' NOT because the delete default was
    missed but because the S2.5 sole-copy guard vetoes the delete. The
    response must make that machine-distinguishable from a keep policy:
    placeholder_veto='unmoved_content' + the warning."""
    src_tab = {
        "tabProperties": {"tabId": PRIMARY},
        "documentTab": {
            "body": {"content": [
                {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
                _para("Course title page\n", start=1, end=19),
                _para("Intro\n", "HEADING_1", 19, 25),
                _para("intro body\n", start=25, end=36),
                _para("Methods\n", "HEADING_1", 36, 44),
                _para("methods body\n", start=44, end=57),
            ]},
            "lists": {},
            "inlineObjects": {},
            "namedStyles": _named_styles_sheet(),
        },
    }
    src = {"tabs": [src_tab]}
    shells = {"tabs": [src_tab, _tab("t.1", _empty_shell()), _tab("t.2", _empty_shell())]}
    verify = {"tabs": [src_tab, _tab("t.1", _filled(3)), _tab("t.2", _filled(3))]}
    pipeline["docs"]._gets = [src, shells, verify]
    _wire_drive_source(monkeypatch, docx_import.GDOC_MIME)

    result = convert_docx_to_tabbed_doc(object(), drive_file_id="SRC1")

    assert result["placeholder"] == "kept"
    assert result["placeholder_veto"] == "unmoved_content"
    assert any("never moved" in w for w in result["warnings"])
    assert ("delete_tab", PRIMARY) not in pipeline["events"]
    # A clean-bodied doc through the same entry deletes (proven by
    # test_drive_entry_defaults_placeholder_delete_like_upload); this
    # veto is content-dependent, not entry-point-dependent.


def test_on_conflict_replace_trashes_all_same_title_priors(pipeline):
    """N5 (2026-07-10 retest): with N>1 same-title priors, replace used
    to trash only the newest and leave the older duplicates lingering.
    Every app-visible prior goes; the response lists them all
    (newest-first) and keeps the singular field as the newest."""
    pipeline["title_matches"] = [
        {"file_id": "PRIOR_NEW", "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
        {"file_id": "PRIOR_OLD", "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
    ]
    result = _convert(on_conflict="replace")

    events = pipeline["events"]
    assert ("trash", "PRIOR_NEW") in events
    assert ("trash", "PRIOR_OLD") in events
    assert result["on_conflict_action"] == "replaced"
    assert result["replaced_doc_ids"] == ["PRIOR_NEW", "PRIOR_OLD"]
    assert result["replaced_doc_id"] == "PRIOR_NEW"


def test_on_conflict_replace_partial_trash_failure_reports_what_happened(
    pipeline, monkeypatch
):
    """One prior trashed + one trash failure: action says replaced (a
    prior WAS replaced) and replaced_doc_ids lists only the doc that
    actually went to trash; the failure lands in info."""
    pipeline["title_matches"] = [
        {"file_id": "PRIOR_NEW", "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
        {"file_id": "PRIOR_STUCK", "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
    ]
    real_trash = docx_import.trash_drive_file

    def selective_trash(creds, file_id):
        if file_id == "PRIOR_STUCK":
            raise RuntimeError("Drive 500")
        return real_trash(creds, file_id)

    monkeypatch.setattr(docx_import, "trash_drive_file", selective_trash)
    result = _convert(on_conflict="replace")

    assert result["on_conflict_action"] == "replaced"
    assert result["replaced_doc_ids"] == ["PRIOR_NEW"]
    assert any("PRIOR_STUCK" in line for line in result["info"])


def test_on_conflict_replace_ignores_docx_and_self_matches(pipeline):
    """The lookup must never trash a lingering .docx SOURCE (mimeType
    filter) nor the just-created doc itself (id exclusion)."""
    pipeline["title_matches"] = [
        {
            "file_id": "RAW_DOCX",
            "name": "fake.docx",
            "mimeType": (
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
        },
        {"file_id": DOC_ID, "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
    ]
    result = _convert(on_conflict="replace")

    assert ("trash", "RAW_DOCX") not in pipeline["events"]
    assert ("trash", DOC_ID) not in pipeline["events"]
    assert result["on_conflict_action"] == "created"


def test_on_conflict_replace_with_explicit_replace_doc_id_wins(pipeline):
    """An explicit replace_doc_id takes precedence: the title lookup is
    skipped entirely."""
    pipeline["title_matches"] = [
        {"file_id": "PRIOR", "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
    ]
    result = _convert(on_conflict="replace", replace_doc_id="EXPLICIT")

    assert ("trash", "EXPLICIT") in pipeline["events"]
    assert ("trash", "PRIOR") not in pipeline["events"]
    assert pipeline["find_calls"] == []
    assert result["replaced_doc_id"] == "EXPLICIT"


def test_on_conflict_replace_trash_failure_reports_created(pipeline, monkeypatch):
    """on_conflict_action reports what HAPPENED: a failed trash means
    the prior doc still exists, so the response must say 'created'."""
    pipeline["title_matches"] = [
        {"file_id": "PRIOR", "name": "fake",
         "mimeType": "application/vnd.google-apps.document"},
    ]

    def exploding_trash(creds, file_id):
        raise RuntimeError("Drive 500")

    monkeypatch.setattr(docx_import, "trash_drive_file", exploding_trash)
    result = _convert(on_conflict="replace")

    assert result["on_conflict_action"] == "created"
    assert result["action"] == "created"
    assert "replaced_doc_id" not in result
    assert any("could not trash prior same-title doc" in n for n in result["info"])


def test_on_conflict_invalid_value_raises(pipeline):
    with pytest.raises(ValueError, match="on_conflict"):
        _convert(on_conflict="upsert")


# ---------------------------------------------------------------------
# nest_by="heading_2" - nested split tree through the full pipeline
# ---------------------------------------------------------------------
#
# Source: Part A (H1) > a intro > A.1 (H2) > a1 body > Part B (H1) >
# b body. Expected tree: t.1 = Part A (parent, keeps "a intro"),
# t.2 = A.1 (child of t.1, gets "a1 body"), t.3 = Part B.


def _nested_source_doc() -> dict:
    content = [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
        _para("Part A\n", "HEADING_1", 1, 8),
        _para("a intro\n", start=8, end=16),
        _para("A.1\n", "HEADING_2", 16, 20),
        _para("a1 body\n", start=20, end=28),
        _para("Part B\n", "HEADING_1", 28, 35),
        _para("b body\n", start=35, end=42),
    ]
    return {
        "tabs": [
            {
                "tabProperties": {"tabId": PRIMARY},
                "documentTab": {
                    "body": {"content": content},
                    "lists": {},
                    "inlineObjects": {},
                    "namedStyles": _named_styles_sheet(),
                },
            }
        ]
    }


def _nested_outline() -> dict:
    return {
        "doc_id": DOC_ID,
        "trashed": False,
        "tabs": [
            {"tab_id": "t.1", "title": "Part A", "parent_tab_id": None, "depth": 0, "index": 0, "icon_emoji": None},
            {"tab_id": "t.2", "title": "A.1", "parent_tab_id": "t.1", "depth": 1, "index": 1, "icon_emoji": None},
            {"tab_id": "t.3", "title": "Part B", "parent_tab_id": None, "depth": 0, "index": 2, "icon_emoji": None},
        ],
    }


def _use_nested_doc(pipeline, monkeypatch, gets=None):
    source = _nested_source_doc()["tabs"][0]
    shells = {
        "tabs": [
            source,
            _tab("t.1", _empty_shell()),
            _tab("t.2", _empty_shell()),
            _tab("t.3", _empty_shell()),
        ]
    }
    verify = {
        "tabs": [source] + [_tab(f"t.{i}", _filled(2)) for i in (1, 2, 3)]
    }
    pipeline["docs"]._gets = (
        gets if gets is not None else [_nested_source_doc(), shells, verify]
    )
    pipeline["nested_shells"] = shells
    monkeypatch.setattr(
        docx_import, "get_doc_outline", lambda creds, doc_id: _nested_outline()
    )


def test_nested_happy_path_shells_slices_manifest_and_response(
    pipeline, monkeypatch
):
    _use_nested_doc(pipeline, monkeypatch)
    result = _convert(nest_by="heading_2")

    # Shell specs went to add_tabs_to_doc NESTED (A.1 as a child of
    # Part A), so Google creates a real depth-2 sidebar.
    (specs,) = pipeline["added_specs"]
    assert [s["title"] for s in specs] == ["Part A", "Part B"]
    assert [c["title"] for c in specs[0].get("children", [])] == ["A.1"]
    assert "children" not in specs[1]

    # Transplant slices are planned per NODE: the parent keeps only
    # its H1 heading + the content before its first H2; the child owns
    # its H2 heading + body; nothing leaks across nodes.
    by_tab: dict[str, str] = {}
    for batch in pipeline["docs"].batches:
        for r in batch:
            if "insertText" in r:
                tab = r["insertText"]["location"]["tabId"]
                by_tab[tab] = by_tab.get(tab, "") + r["insertText"]["text"]
    assert "Part A" in by_tab["t.1"] and "a intro" in by_tab["t.1"]
    assert "A.1" not in by_tab["t.1"] and "a1 body" not in by_tab["t.1"]
    assert "A.1" in by_tab["t.2"] and "a1 body" in by_tab["t.2"]
    assert "Part B" in by_tab["t.3"] and "b body" in by_tab["t.3"]

    # 3 nodes x 2 blocks each; the T2.1 echo counts PARENTS as
    # heading1_found and every created tab (children included) as
    # tabs_created.
    assert result["moved_children"] == 6
    assert result["heading1_found"] == 2
    assert result["tabs_created"] == 3
    # The completion manifest treats child sections as first-class:
    # each node's title appears in moved_sections on its own.
    assert result["completion"]["moved_sections"] == ["Part A", "A.1", "Part B"]
    assert result["completion"]["pending_sections"] == []
    assert result["completion"]["steps_completed"] == [
        "import", "shells", "transplant", "verify",
        "carve", "cosmetics", "placeholder",
    ]
    # The response tabs mirror the outline shape: parent_tab_id + depth
    # carry the nesting.
    child = next(t for t in result["tabs"] if t["title"] == "A.1")
    assert child["parent_tab_id"] == "t.1"
    assert child["depth"] == 1
    # Clean source: the one warning is the delete-policy advisory.
    assert len(result["warnings"]) == 1
    assert "can no longer be edited" in result["warnings"][0]


def test_nested_carve_covers_child_ranges_too(pipeline, monkeypatch):
    """The post-verify carve removes EVERY transplanted range from the
    source tab - including the child sections' ranges."""
    _use_nested_doc(pipeline, monkeypatch)
    _convert(nest_by="heading_2")

    carve_batches = [
        b for b in pipeline["docs"].batches if "deleteContentRange" in b[0]
    ]
    assert len(carve_batches) == 1
    spans = [
        (r["deleteContentRange"]["range"]["startIndex"],
         r["deleteContentRange"]["range"]["endIndex"])
        for r in carve_batches[0]
    ]
    # Parent A (1..16), child A.1 (16..28), B (28..42 capped to 41),
    # emitted bottom-up.
    assert spans == [(28, 41), (16, 28), (1, 16)]


def test_nested_child_verify_failure_keeps_doc_and_marks_child_pending(
    pipeline, monkeypatch
):
    """verify-all covers CHILD tabs, and the S2.5 keep-everything
    contract applies to them: an under-filled child fails the verify
    pass, the doc is KEPT (no shell deletes, no trash, no carve), and
    the manifest lists the CHILD section pending while its verified
    siblings (parent included) read moved."""
    source = _nested_source_doc()["tabs"][0]
    shells = {
        "tabs": [source] + [_tab(f"t.{i}", _empty_shell()) for i in (1, 2, 3)]
    }
    bad_verify = {
        "tabs": [
            source,
            _tab("t.1", _filled(2)),
            _tab("t.2", _empty_shell()),  # the child got nothing
            _tab("t.3", _filled(2)),
        ]
    }
    # source fetch, shells-state fetch, verify fetch, classification
    # re-fetch (the partial-failure result re-verifies per tab).
    _use_nested_doc(
        pipeline, monkeypatch,
        gets=[_nested_source_doc(), shells, bad_verify, bad_verify],
    )

    result = _convert(nest_by="heading_2")

    assert "error" in result
    assert result["completion"]["moved_sections"] == ["Part A", "Part B"]
    assert result["completion"]["pending_sections"] == ["A.1"]
    # Every plan finished executing; verify is what fell short.
    assert result["completion"]["steps_completed"] == [
        "import", "shells", "transplant",
    ]
    assert result["heading1_found"] == 2
    assert result["tabs_created"] == 3
    assert result["placeholder"] == "kept"
    _assert_placeholder_untouched(pipeline)


def test_nested_phase_a_cleanup_deletes_child_shells_first(
    pipeline, monkeypatch
):
    """A failure BEFORE any content write (the shells-state fetch dies)
    still cleans up - and the shell deletes run in reversed pre-order,
    children before their parents, so no delete targets a tab Google
    already removed as part of a parent's subtree."""
    _use_nested_doc(pipeline, monkeypatch)
    pipeline["docs"].fail_get_at = 1  # 0=source fetch, 1=shells-state fetch

    with pytest.raises(RuntimeError, match="Drive trash"):
        _convert(nest_by="heading_2")

    rollback_deletes = [
        r["deleteTab"]["tabId"]
        for batch in pipeline["docs"].batches
        for r in batch
        if "deleteTab" in r
    ]
    assert rollback_deletes == ["t.3", "t.2", "t.1"]
    assert ("trash", DOC_ID) in pipeline["events"]


def test_nested_tab_icons_assign_in_document_order(pipeline, monkeypatch):
    """tab_icons positions follow document order - parents and children
    interleaved as their headings appear (Part A, A.1, Part B)."""
    _use_nested_doc(pipeline, monkeypatch)
    _convert(nest_by="heading_2", tab_icons=["\U0001f170", "\U00000031", "\U0001f171"])
    (specs,) = pipeline["added_specs"]
    assert specs[0].get("icon_emoji") == "\U0001f170"
    assert specs[0]["children"][0].get("icon_emoji") == "\U00000031"
    assert specs[1].get("icon_emoji") == "\U0001f171"


def test_flat_doc_with_nest_by_behaves_exactly_flat(pipeline):
    """Regression: nest_by on a doc with H1s but NO H2s must produce
    the same flat result as today (same shells, same tab count, same
    manifest)."""
    result = _convert(nest_by="heading_2")
    (specs,) = pipeline["added_specs"]
    assert [s["title"] for s in specs] == ["Intro", "Methods"]
    assert all("children" not in s for s in specs)
    assert [t["title"] for t in result["tabs"]] == ["Intro", "Methods"]
    assert result["heading1_found"] == 2
    assert result["completion"]["moved_sections"] == ["Intro", "Methods"]


# ---------------------------------------------------------------------
# nest_by validation - loud, and BEFORE any Drive/Docs traffic
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_split_by", ["heading_2", "page_break", "auto"])
def test_nest_by_rejected_unless_split_by_is_heading_1(pipeline, bad_split_by):
    with pytest.raises(ValueError, match="split_by='heading_1'"):
        _convert(split_by=bad_split_by, nest_by="heading_2")
    assert pipeline["docs"].batches == []
    assert pipeline["events"] == []


def test_nest_by_invalid_value_rejected(pipeline):
    with pytest.raises(ValueError, match="Invalid nest_by"):
        _convert(nest_by="heading_3")
    assert pipeline["docs"].batches == []
    assert pipeline["events"] == []


# ---------------------------------------------------------------------
# E2 (2026-07-12): custom docx styling - visible loss + robust read
# ---------------------------------------------------------------------


def test_missing_named_styles_sheet_warns_instead_of_silent_default(pipeline):
    """When the source read surfaces NO named-style sheet, the custom
    heading / text look cannot be re-emitted and the new tabs fall back
    to Google's defaults. That loss must be VISIBLE - a fidelity warning,
    not a silent default (the field crime). Pre-fix nothing is emitted;
    the user only discovers the plain look by eye."""
    bare = _source_doc()
    bare["tabs"][0]["documentTab"].pop("namedStyles", None)
    pipeline["docs"]._gets = [bare, _shells_doc(), _verify_doc()]

    result = _convert()

    assert any(
        "custom document styling not carried" in w for w in result["warnings"]
    )
    # The conversion itself still succeeds - the warning is advisory, not
    # fatal; content moves, tabs are created.
    assert result["completion"]["moved_sections"] == ["Intro", "Methods"]


def test_named_styles_read_falls_back_to_legacy_top_level(pipeline):
    """Robust read: under includeTabsContent the sheet normally lives at
    tabs[].documentTab.namedStyles, but if a response instead carries it
    at the legacy top-level document.namedStyles, the converter must
    still find it and carry the custom look into the new tabs. Pre-fix
    the tab-only read drops it: no updateNamedStyle is emitted."""
    src = _source_doc()
    sheet = src["tabs"][0]["documentTab"].pop("namedStyles")
    src["namedStyles"] = sheet  # legacy top-level location
    pipeline["docs"]._gets = [src, _shells_doc(), _verify_doc()]

    result = _convert()

    named_reqs = [
        r
        for batch in pipeline["docs"].batches
        for r in batch
        if "updateNamedStyle" in r
    ]
    assert named_reqs, "the sheet must be carried from the legacy top-level location"
    # It WAS carried, so no styling-loss warning fires.
    assert not any(
        "custom document styling not carried" in w for w in result["warnings"]
    )


def test_clean_doc_with_named_styles_emits_no_styling_warning(pipeline):
    """A normal doc (the enriched fixture carries a named-style sheet, as
    every real Google Doc does) must NOT trip the styling warning - it
    fires only on a genuinely absent sheet, never on ordinary content."""
    result = _convert()
    assert not any(
        "custom document styling not carried" in w for w in result["warnings"]
    )
    # The sheet is re-emitted onto each new tab.
    named_reqs = [
        r
        for batch in pipeline["docs"].batches
        for r in batch
        if "updateNamedStyle" in r
    ]
    assert named_reqs


# ---------------------------------------------------------------------
# A2 (retest 3): write governor keying + rate-limit envelope flag
# ---------------------------------------------------------------------


def test_transplant_writes_use_the_callers_user_governor(pipeline, monkeypatch):
    """docx_import keys the per-user write governor by the route's
    user_id, so every concurrent job of one user paces against the same
    quota bucket."""
    from appscriptly.services.docs import content_transplant as ct

    seen_keys: list = []
    real = ct._governor_for
    monkeypatch.setattr(
        ct, "_governor_for",
        lambda key: (seen_keys.append(key), real(key))[1],
    )
    _convert(user_id="user-G")
    assert seen_keys, "the transplant must resolve a governor"
    assert set(seen_keys) == {"user-G"}


def test_transplant_writes_fall_back_to_the_operator_governor(
    pipeline, monkeypatch
):
    """No user identity (stdio / bearer callers) = the operator bucket
    (content_transplant maps None to it)."""
    from appscriptly.services.docs import content_transplant as ct

    seen_keys: list = []
    real = ct._governor_for
    monkeypatch.setattr(
        ct, "_governor_for",
        lambda key: (seen_keys.append(key), real(key))[1],
    )
    _convert()
    assert seen_keys and set(seen_keys) == {None}


def test_partial_failure_envelope_flags_a_rate_limit_cause(
    pipeline, monkeypatch
):
    """A 429 that survives the backoff budget mid-transplant produces
    the kept-doc envelope WITH rate_limited=True - the signal the job
    runner requeues on. Non-429 partial failures (pinned by the S2.5
    death test) carry no flag."""
    from googleapiclient.errors import HttpError

    from appscriptly.services.docs import content_transplant as ct

    # The persistent 429 must exhaust the N2 backoff budget without
    # really sleeping through it.
    monkeypatch.setattr(ct.time, "sleep", lambda s: None)
    monkeypatch.setattr(ct.random, "uniform", lambda a, b: 0.0)

    class _Resp:
        status = 429
        reason = "Too Many Requests"

    calls = {"n": 0}

    def raise_429_on_second_batch(requests):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise HttpError(resp=_Resp(), content=b"rate limit")
        return False

    pipeline["fail_when"] = raise_429_on_second_batch
    result = _convert()

    assert result["error"], "partial failure expected"
    assert result["rate_limited"] is True
    # (The harness's static verify doc classifies both sections as
    # moved, so pending_sections is not meaningful here; the flag +
    # kept-doc envelope are the properties under test.)
    assert "completion" in result


def test_is_rate_limit_cause_walks_the_wrap_chain():
    """The pre-transplant path wraps the HttpError in a RuntimeError
    (raise ... from); the detector must see through it. Plain failures
    stay False."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 429
        reason = "Too Many Requests"

    err = HttpError(resp=_Resp(), content=b"x")
    assert docx_import._is_rate_limit_cause(err) is True

    try:
        try:
            raise err
        except HttpError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as wrapped:
        assert docx_import._is_rate_limit_cause(wrapped) is True

    assert docx_import._is_rate_limit_cause(RuntimeError("plain")) is False
    assert docx_import._is_rate_limit_cause(None) is False
