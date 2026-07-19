"""Wave E: .docx / Google Doc → native-tabbed Google Doc (pure REST).

Pipeline (ORDER IS A DATA-SAFETY CONTRACT - the 2026-07-10 order pin):
  1. Import with the FINAL title set at Drive-create time (upload +
     convert .docx, fetch-and-convert a Drive .docx, or copy an
     existing Google Doc). Never a temp name: a pipeline that dies
     later must still leave a doc the user can find by its real title.
  2. We identify split points by walking the converted doc's paragraph
     stream looking for the configured heading style.
  3. REST creates empty tab shells (addDocumentTab) - one per split
     point, nested per ``children``.
  4. The content transplant re-emits each split range into its shell
     via ``documents.batchUpdate`` (``services/docs/content_transplant``):
     read shape → write shape, table sync-points, explicit tabId on
     every request. High-fidelity with a detected-and-warned loss tail
     (equations, drawings, floating objects - see ``DROPPED_KINDS``).
  5. Verify EVERY new tab, then (and only then) carve the transplanted
     ranges out of the primary tab. Carving strictly after verify-all
     means a death at ANY earlier point leaves a duplicated-but-
     lossless doc: the placeholder still holds everything.
  6. Cosmetics (icons, prior-version trash) run AFTER every data-
     safety step but BEFORE the placeholder policy: a Google-side
     defect makes every ``updateDocumentTabProperties`` (icon sets AND
     tab renames) 500 permanently once a document's ORIGINAL FIRST TAB
     has been deleted, so icons must land while that tab still exists
     (2026-07-10 contract amendment; defect live-proven in the tools
     stream). Cosmetic failures downgrade to response warnings; they
     can never abort or precede a safety step (the S2.2 incident: an
     OOM during finishing steps stranded the placeholder handling).
  7. The placeholder policy (delete / rename / keep) runs LAST. A
     successful delete appends an advisory warning that tab icons and
     tab renames are permanently uneditable on the produced doc (the
     same Google defect); use rename/keep to avoid that.

Failure semantics: if the pipeline fails BEFORE any content write, any
shells are removed and the working copy is moved to Drive trash
(recoverable there for 30 days; the error names the trashed doc_id).
Once content has started moving, the working copy is KEPT and a partial
result is returned whose ``completion`` manifest says exactly which
sections are verified in their tabs (``moved_sections``) and which
still exist ONLY inside the placeholder tab (``pending_sections``) - so
no tool or user deletes a placeholder that holds sole-copy content (the
S2.5 data-loss incident). ``steps_completed`` distinguishes an
execution death ("transplant" absent) from a verify shortfall
("transplant" present, "verify" absent).

History: steps 4-5 used to POST the split spec to a per-user Apps
Script web app (``Element.copy()`` moves). That path never worked for
cloud users: an API-deployed script holding a sensitive scope has no
per-script consent grant, so Google 403s anonymous /exec requests
before ``doPost`` runs. The REST transplant needs no script project,
no deployment, and no consent beyond the product's own OAuth grant.
Decision record: ``_audit/2026-07-08-tabs-architecture-decision.md``;
root-cause spike: ``_audit/2026-07-08-exec-scope-auth-spike.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypedDict

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

from .services.docs.api import (
    MAX_NESTING_DEPTH,
    TabSpec,
    add_tabs_to_doc,
    delete_tab,
    get_doc_outline,
    rename_tab,
    set_tab_icons,
)
from .services.docs.content_transplant import (
    FidelityReport,
    TabTransplantPlan,
    carve_source_ranges,
    execute_tab_transplant,
    has_named_style_content,
    plan_tab_transplant,
    verify_tab_transplant,
)

PlaceholderBehavior = Literal["delete", "rename", "keep"]
OnConflict = Literal["new", "replace", "skip"]
DEFAULT_PLACEHOLDER_TITLE = "Overview"
DEFAULT_PLACEHOLDER_ICON = "\U0001f4d1"  # 📑

# Canonical pipeline steps in execution order (the data-safety order
# pin, amended 2026-07-10: cosmetics precede the placeholder step so
# icons land before the original first tab can be deleted - see the
# module docstring). ``completion.steps_completed`` in every convert
# response draws from this tuple; a step is listed when it ran to
# completion OR had nothing to do, so an ABSENT step always means
# "work remains or failed" - the signal a consumer needs before
# trusting the doc state.
PIPELINE_STEPS = (
    "import",
    "shells",
    "transplant",
    "verify",
    "carve",
    "cosmetics",
    "placeholder",
)
from .services.drive.api import (
    DOCX_MIME,
    GDOC_MIME,
    classify_drive_file,
    copy_google_doc,
    effective_convert_title,
    fetch_and_convert_drive_docx,
    find_doc_by_title,
    trash_drive_file,
    upload_and_convert_docx,
)

SplitBy = Literal["heading_1", "heading_2", "page_break", "auto"]
# Second-level split strategy: only "heading_2" (child tabs under each
# heading_1 parent), and only valid combined with split_by="heading_1".
NestBy = Literal["heading_2"]
_STYLE_FOR_SPLIT = {
    "heading_1": "HEADING_1",
    "heading_2": "HEADING_2",
}

# Google Docs rejects a tab title longer than this ("The tab title cannot
# be longer than 50 characters"), so detected titles are truncated to it
# and any de-dup suffix must keep the result within it.
_MAX_TAB_TITLE = 50

# Advisory appended whenever the placeholder tab is actually deleted:
# a live-proven Google-side defect makes updateDocumentTabProperties
# (icon sets AND tab renames) return 500 permanently on a document
# whose ORIGINAL FIRST TAB has been deleted. The conversion itself is
# unaffected; only later tab-property edits are. The trailing
# (first_tab_deleted_500) marker is the defect's canonical grep-able
# identifier, shared with gdocs_delete_tab and the docs api error text.
_DELETE_LOCKS_TAB_PROPERTIES_WARNING = (
    "placeholder tab deleted: due to a Google-side defect, tab icons and "
    "tab titles can no longer be edited on this document (Google returns "
    "an internal error for tab-property updates after a document's "
    "original first tab is deleted; adding tabs later does not clear the "
    "state). New tabs can still carry icon_emoji at creation. If you need "
    "to adjust icons or rename tabs later, convert with "
    "placeholder_behavior='rename' or 'keep' instead. (first_tab_deleted_500)"
)


class _SplitPoint(TypedDict):
    title: str
    icon_emoji: str | None
    # List of [startChild, endChild] index ranges into the source body
    # (inclusive on both ends). Multiple ranges support the future
    # "parent keeps gaps between children" semantics; flat detection
    # always produces a single contiguous range per split.
    ranges: list[tuple[int, int]]
    children: list["_SplitPoint"]


def convert_docx_to_tabbed_doc(
    creds: Credentials,
    docx_path: Path | None = None,
    drive_file_id: str | None = None,
    split_by: SplitBy = "heading_1",
    nest_by: NestBy | None = None,
    title: str | None = None,
    tab_icons: list[str] | None = None,
    icons_by_title: dict[str, str] | None = None,
    placeholder_behavior: PlaceholderBehavior = "delete",
    placeholder_title: str = DEFAULT_PLACEHOLDER_TITLE,
    placeholder_icon: str = DEFAULT_PLACEHOLDER_ICON,
    replace_doc_id: str | None = None,
    on_conflict: OnConflict = "new",
    docx_drive_file_id: str | None = None,  # deprecated alias for drive_file_id
    user_id: str | None = None,
) -> dict:
    """Convert a .docx OR a Google Doc into a Google Doc with native nested tabs.

    Three input modes - pick the one that matches your environment:

    - ``docx_path``: absolute path to a local ``.docx`` on the machine
      the MCP server runs on. Works for local stdio MCP (Claude Code /
      Claude Desktop). DOES NOT work from claude.ai cloud chat - the
      remote server can't see the chat sandbox's filesystem.
    - ``drive_file_id``: Drive file ID of an already-uploaded .docx OR
      Google Doc. Use this when the document already lives on Drive.
      Note that programmatically-uploaded .docx blobs can fail Drive
      conversion with 400 conversionUnsupportedConversionPath - if
      that happens from cloud chat, switch to the signed-URL flow.
    - **Signed-URL flow** (NOT a parameter here): from cloud chat,
      call ``get_signed_upload_url`` then POST the .docx bytes to
      that URL with multipart/form-data. The REST endpoint accepts
      the same fields as this tool. This is the only reliable path
      for sandbox-built .docx files.

    ``nest_by`` turns the flat split into a depth-2 tab tree. The only
    supported value is ``"heading_2"``, and it is only valid together
    with ``split_by="heading_1"`` (anything else raises ``ValueError``
    - no silent fallback). Each Heading 1 becomes a parent tab; every
    Heading 2 after it becomes a child tab of that parent; content
    between a Heading 1 and its first Heading 2 stays in the parent
    tab. A doc whose Heading 1 sections contain no Heading 2s produces
    exactly the flat result. A Heading 2 that appears BEFORE the first
    Heading 1 has no parent to attach to and stays behind in the
    placeholder tab (the same contract flat mode applies to any
    content preceding the first split point). The ``completion``
    manifest treats child sections exactly like parents: each child
    title appears in ``moved_sections`` / ``pending_sections`` on its
    own.

    ``tab_icons`` is an optional list of emoji icons assigned in
    detected-split order - document order, so with ``nest_by`` parents
    and children interleave exactly as their headings appear. If
    shorter than the number of splits, the remaining tabs get no icon.
    To set icons later (or by title match instead of order), use
    ``set_tab_icons``.

    ``on_conflict`` controls what happens when an app-visible Google
    Doc with the SAME final title already exists (title lookup runs
    under the ``drive.file`` scope, so only docs this app created or
    was explicitly granted are considered):

    - ``"new"`` (default): always create a new doc; never look.
    - ``"replace"``: build the new doc fully; after a fully successful
      build, trash EVERY prior same-title doc (recoverable from Drive
      trash for 30 days) - the response lists them newest-first in
      ``replaced_doc_ids``, with ``replaced_doc_id`` kept as the newest
      for pre-N5 callers. If the build fails or finds no split points,
      the prior docs are left untouched. An explicit
      ``replace_doc_id`` takes precedence over the title lookup (and
      trashes only that doc).
    - ``"skip"``: if a same-title doc already exists, perform NO
      conversion and return that doc's info with ``action="skipped"``.

    on_conflict compares against PRE-EXISTING documents only, never
    against sibling jobs inside the same batch request: two same-title
    parts in one batch both get created (by design - the lookup runs
    before either sibling exists).

    ``user_id`` is accepted for REST-route compatibility (the
    ``/api/convert`` endpoint forwards the signed-URL caller's uid).
    The conversion derives identity from ``creds`` alone now that the
    per-user web-app step (which needed a per-user URL lookup) is gone.

    Returns ``{"doc_id", "url", "action", "on_conflict_action", "tabs",
    "split_strategy_used", "heading1_found", "tabs_created",
    "placeholder", "warnings", "info", "completion", ...}``.

    ``completion`` is the manifest every response carries:
    ``{"steps_completed": [...], "moved_sections": [...],
    "pending_sections": [...]}``. ``pending_sections`` is non-empty only
    on partial-failure returns; a section listed there exists ONLY in
    the placeholder tab - deleting that tab destroys it.
    """
    # user_id keys the per-user cross-job WRITE GOVERNOR (A2): every
    # Docs write of this conversion is paced against the same user's
    # quota bucket shared with any concurrently running jobs. None
    # (stdio / bearer callers) maps to the operator bucket.
    governor_key = user_id

    if (docx_path is None) == (drive_file_id is None):
        raise ValueError(
            "Provide exactly one of docx_path or drive_file_id "
            "(got both, or neither)."
        )
    if on_conflict not in ("new", "replace", "skip"):
        raise ValueError(
            f"on_conflict must be one of 'new', 'replace', 'skip' "
            f"(got {on_conflict!r})."
        )

    # nest_by is strictly validated up front (before any Drive work,
    # including the on_conflict=skip lookup): an unsupported value or
    # combination must fail loudly, never fall back to a flat split
    # the caller didn't ask for.
    if nest_by is not None:
        if nest_by != "heading_2":
            raise ValueError(
                f"Invalid nest_by: {nest_by!r}. The only supported value "
                "is 'heading_2' (Heading 2 sections become child tabs)."
            )
        if split_by != "heading_1":
            raise ValueError(
                "nest_by='heading_2' requires split_by='heading_1' "
                f"(got split_by={split_by!r}): nesting places Heading 2 "
                "sections under their Heading 1 parent tabs."
            )

    # 0. on_conflict=skip resolves BEFORE any import work: if a same-
    # title doc already exists, return it without creating anything.
    # The lookup title mirrors the import helpers' naming rules (see
    # _expected_final_title - kept in lockstep with them).
    if on_conflict == "skip":
        existing = _newest_same_title_doc(
            creds,
            _expected_final_title(creds, docx_path, drive_file_id, title),
            exclude_ids=frozenset(),
        )
        if existing is not None:
            return _skipped_result(creds, existing)

    # 1. Get the source content into a Google Doc we own.
    # Three input modes:
    #   - local .docx path -> upload+convert via Drive
    #   - drive file id pointing to a raw .docx -> fetch+convert
    #   - drive file id pointing to an already-converted Google Doc
    #     -> copy (no conversion needed; the Doc is already native)
    if docx_path is not None:
        converted = upload_and_convert_docx(creds, docx_path, title=title)
        source_label = docx_path.name
    else:
        assert drive_file_id is not None  # narrowing for typecheckers
        source_mime = classify_drive_file(creds, drive_file_id)
        if source_mime == DOCX_MIME:
            converted = fetch_and_convert_drive_docx(
                creds, drive_file_id, title=title
            )
        elif source_mime == GDOC_MIME:
            converted = copy_google_doc(
                creds, drive_file_id, title=title
            )
        else:
            raise ValueError(
                f"Drive file {drive_file_id!r} has mimeType "
                f"{source_mime!r}. Expected .docx or Google Doc."
            )
        source_label = f"drive file {drive_file_id}"
    doc_id = converted["doc_id"]

    # Steps 2-5 (fetch, split detection, shells, transplant, verify)
    # run under phase-aware failure handling (BUG 1d + S2.5):
    #   - failure BEFORE any transplant write: the staging copy holds
    #     nothing unique (the source is untouched), so delete any
    #     shells and trash the copy - failed conversions must not
    #     orphan working copies in Drive. The error names the trashed
    #     doc_id (recoverable from Drive trash for 30 days).
    #   - failure AT or AFTER the first transplant write: content may
    #     already live in the new tabs and a batch whose response was
    #     lost may have landed server-side - the copy is KEPT and a
    #     structured partial result carries the completion manifest.
    #     Cleanup code must never trash a doc that could hold the only
    #     copy of anything.
    transplant_write_attempted = False
    executed_count = 0
    created_tabs: list[dict] = []
    splits: list[_SplitPoint] = []
    plans: list[tuple[dict, TabTransplantPlan]] = []
    report = FidelityReport()
    strategy_used: str = split_by
    docs: Any = None
    moved_blocks = 0
    try:
        # 2. Find split points in the converted doc's primary tab body.
        docs = get_service("docs", "v1", credentials=creds)
        fetched = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.tabExistingDoc.detectSplits",
        )
        source_tab = fetched["tabs"][0]["documentTab"]
        body_content = source_tab["body"]["content"]

        splits, strategy_used = _detect_splits(
            body_content, split_by, nest_by=nest_by
        )
        if not splits:
            # No conversion work to do: the doc is a clean single-tab
            # import carrying its final title. Prior-version replacement
            # (replace_doc_id / on_conflict=replace) is deliberately NOT
            # applied here - trashing a real tabbed doc in favor of an
            # unsplit import is never what a retry intended.
            info: list[str] = []
            if replace_doc_id or on_conflict == "replace":
                info.append(
                    "prior version NOT replaced: no split points were found, "
                    "so the previous document was left untouched."
                )
            return {
                "doc_id": doc_id,
                "url": converted["url"],
                "action": "created",
                "on_conflict_action": "created",
                "tabs": [],
                "moved_children": 0,
                "heading1_found": 0,
                "tabs_created": 0,
                "placeholder": "none",
                "warnings": [],
                "info": info,
                "split_strategy_used": strategy_used,
                "completion": {
                    # Every step either ran or had nothing to do;
                    # nothing is pending. (There is no placeholder
                    # duplication in a single-tab import - the one tab
                    # IS the content.)
                    "steps_completed": list(PIPELINE_STEPS),
                    "moved_sections": [],
                    "pending_sections": [],
                },
                "note": (
                    "No split points found; doc is left as a single-tab "
                    f"conversion of {source_label}."
                ),
            }

        # Pre-order flatten once: this is BOTH the icon-assignment
        # order (document order - parents and children interleaved as
        # their headings appear) and the order add_tabs_to_doc returns
        # created shells in, so position i means the same node
        # everywhere downstream.
        flat_splits = _flatten_splits(splits)

        # Assign optional icons in detected-split order.
        if tab_icons:
            for i, split in enumerate(flat_splits):
                if i < len(tab_icons) and tab_icons[i]:
                    split["icon_emoji"] = tab_icons[i]

        # 3. Cap nesting depth defensively - heading_1 + nest_by emits
        # at most parent+child (depth 1); guard against a future
        # strategy emitting deeper trees than Google allows.
        max_depth = _max_depth(splits)
        if max_depth >= MAX_NESTING_DEPTH:
            raise RuntimeError(
                f"Detected {max_depth + 1} nesting levels; "
                f"Google Docs UI allows at most {MAX_NESTING_DEPTH}."
            )

        # 4. REST creates the empty tab shells at root level - siblings
        # of the placeholder primary tab, not children of it. Gives the
        # user a sidebar of top-level section tabs instead of one
        # collapsed root.
        primary_tab_id = fetched["tabs"][0]["tabProperties"]["tabId"]
        # N11: the working copy of a native multi-tab Google Doc carries
        # the source's existing tabs. A shell whose title matches one of
        # them (or the primary tab, or an earlier shell) 400s
        # addDocumentTab ("Tab title must be unique"), so de-dupe every
        # detected title against what already exists. Nothing is deleted:
        # the source's other tabs are preserved; only the first tab's
        # Heading 1 sections drive new tabs (see the drive_file_id note in
        # gdocs_tab_existing_doc).
        _dedupe_split_titles(flat_splits, _existing_tab_titles(fetched.get("tabs") or []))
        shell_specs = [_split_to_tabspec(s) for s in splits]
        created_tabs = add_tabs_to_doc(creds, doc_id, shell_specs)["tabs"]

        # 5. Content transplant: plan every tab first (pure - planning
        # IS the fidelity preflight, so the warning list is complete
        # before any write), then execute, then VERIFY. Split ranges
        # are DocApp-child indices, so they slice the same
        # sectionBreak-filtered list _detect_splits walked.
        docapp_children = _docapp_children(body_content)
        if len(flat_splits) != len(created_tabs):
            raise RuntimeError(
                f"shell creation returned {len(created_tabs)} tabs for "
                f"{len(flat_splits)} split points"
            )

        if sum(1 for e in body_content if "sectionBreak" in e) > 1:
            report.count("multi_section")

        # Named styles carry the source's custom heading / text looks
        # (navy bold headings, a monospace body font, ...). Under
        # includeTabsContent the sheet lives at
        # tabs[].documentTab.namedStyles; fall back to the legacy
        # top-level document.namedStyles so an unexpected response shape
        # cannot silently strand it. If the sheet did not reach the
        # planner at all, the custom look CANNOT be re-emitted - surface
        # that as a fidelity warning rather than letting the new tabs
        # default silently (the E2 field report: custom-styled headings
        # rendered as plain defaults with no signal to the user).
        named_styles = source_tab.get("namedStyles") or fetched.get("namedStyles")
        if not has_named_style_content(named_styles):
            report.count("named_styles_not_carried")

        for split, tab in zip(flat_splits, created_tabs):
            elements = [
                element
                for lo, hi in split["ranges"]
                for element in docapp_children[lo : hi + 1]
            ]
            plan = plan_tab_transplant(
                elements,
                lists=source_tab.get("lists") or {},
                inline_objects=source_tab.get("inlineObjects") or {},
                dest_tab_id=tab["tab_id"],
                named_styles=named_styles,
                document_style=source_tab.get("documentStyle"),
                report=report,
            )
            plans.append((tab, plan))

        # One fetch covers every shell's starting state: each tab has
        # its own index space, so writing into one tab does not move
        # another tab's insertion point (table sync-points inside the
        # executor re-fetch on their own).
        shells_doc = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.tabExistingDoc.shellsState",
        )
        for tab, plan in plans:
            transplant_write_attempted = True
            moved_blocks += execute_tab_transplant(
                docs, doc_id, tab["tab_id"], plan, document=shells_doc,
                governor_key=governor_key,
            )
            executed_count += 1
        verify_doc = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.tabExistingDoc.verifyTransplant",
        )
        for tab, plan in plans:
            verify_tab_transplant(verify_doc, tab["tab_id"], plan)
    except Exception as e:
        if transplant_write_attempted:
            # Content HAS started moving. The placeholder tab still
            # holds the full source (nothing is carved before
            # verify-all), but some sections may exist only there -
            # trashing or shell-deleting now could destroy the only
            # reachable copy (S2.5). Keep everything and return a
            # partial result whose manifest says exactly what is safe.
            return _partial_failure_result(
                creds,
                docs,
                doc_id=doc_id,
                url=converted["url"],
                plans=plans,
                created_tabs=created_tabs,
                strategy_used=strategy_used,
                report=report,
                error=e,
                source_label=source_label,
                executed_count=executed_count,
                # Parents only (nest_by children are separate plan
                # nodes but not Heading 1s); a transplant write implies
                # detection succeeded, so this is never the initial [].
                heading1_found=len(splits),
            )
        # No content has moved yet: the shells are empty and the
        # working copy holds nothing that doesn't exist elsewhere, so
        # cleanup cannot destroy content. Reversed pre-order deletes
        # child shells before their parent, so no delete ever targets
        # a tab Google already removed as part of a parent's subtree.
        _cleanup_failed_staging_copy(
            creds, doc_id, [t["tab_id"] for t in reversed(created_tabs)]
        )
        wrapped = RuntimeError(
            f"Conversion of {source_label} failed before any content "
            f"moved: {e}. The staging copy {doc_id} was moved to Drive "
            "trash (recoverable there for 30 days); the original source "
            "was not modified."
        )
        # A2: keep the 429 signal readable through the wrap (the raise
        # ... from below sets __cause__, which _is_rate_limit_cause
        # walks; the attribute is belt-and-suspenders for handlers that
        # only see the exception object).
        if _is_rate_limit_cause(e):
            wrapped.rate_limited = True  # type: ignore[attr-defined]
        raise wrapped from e

    # 6. Carve the transplanted ranges out of the primary tab - for
    # EVERY placeholder policy, strictly after verify-all. For
    # ``delete`` this makes the subsequent tab removal a cosmetic
    # cleanup of an (almost) empty tab: if the removal then fails, the
    # stray "Tab 1" contains nothing, instead of a confusing full copy.
    warnings: list[str] = list(report.warnings)
    info: list[str] = list(report.notes)
    all_ranges = [r for s in flat_splits for r in s["ranges"]]
    carve_ok = True
    try:
        carve_source_ranges(
            docs, doc_id, primary_tab_id, docapp_children, all_ranges,
            governor_key=governor_key,
        )
    except Exception as e:  # noqa: BLE001
        carve_ok = False
        warnings.append(
            f"could not carve moved content out of the placeholder tab: {e}"
        )

    # 7. Cosmetics run BEFORE the placeholder step (2026-07-10 contract
    # amendment): deleting a doc's ORIGINAL FIRST TAB permanently 500s
    # every later updateDocumentTabProperties on that document (Google-
    # side defect, live-proven in the tools stream) - so icons must
    # land while that tab still exists. Every DATA-safety step
    # (transplant, verify, carve) is already done; failures here
    # downgrade to warnings, never fatal, and never block the
    # placeholder step below.
    icons_result: dict | None = None
    cosmetics_ok = True
    if icons_by_title:
        try:
            icons_result = set_tab_icons(creds, doc_id, icons_by_title)
        except Exception as e:  # noqa: BLE001
            cosmetics_ok = False
            icons_result = {"error": str(e)}
            warnings.append(
                "cosmetic step failed (the conversion itself is complete): "
                f"could not apply tab icons: {e}"
            )

    # 8. Apply the user's chosen placeholder-tab policy - LAST:
    #    delete: remove the placeholder tab (default) - REFUSED when
    #            content that was never moved into any tab (e.g. text
    #            before the first split heading) still lives there,
    #            because deleting it would destroy the only copy.
    #    rename: rename to placeholder_title with an icon.
    #    keep:   leave it as "Tab 1".
    placeholder_outcome = "kept"
    placeholder_veto: str | None = None
    placeholder_done = False
    if placeholder_behavior == "delete":
        unmoved = _unmoved_visible_count(docapp_children, all_ranges)
        if unmoved:
            # R1 (retest 2) surfaced how easily this veto reads as "the
            # delete default was not applied" - drive-sourced Google
            # Docs commonly carry visible content before the first
            # Heading 1 (a title line, an intro paragraph), which the
            # split never moves, so the sole-copy guard keeps the tab.
            # The ``placeholder_veto`` response field makes the refusal
            # machine-distinguishable from a keep POLICY.
            placeholder_veto = "unmoved_content"
            warnings.append(
                f"placeholder tab kept instead of deleted: {unmoved} content "
                "block(s) before the first split point were never moved into "
                "any tab, and deleting the tab would destroy the only copy. "
                "Review the placeholder tab; delete it manually only if that "
                "content is disposable."
            )
        else:
            try:
                delete_tab(creds, doc_id, primary_tab_id)
                placeholder_outcome = "deleted"
                placeholder_done = True
                warnings.append(_DELETE_LOCKS_TAB_PROPERTIES_WARNING)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"could not delete placeholder tab: {e}")
    elif placeholder_behavior == "rename":
        try:
            rename_tab(
                creds,
                doc_id,
                primary_tab_id,
                title=placeholder_title,
                icon_emoji=placeholder_icon,
            )
            placeholder_outcome = "renamed"
            placeholder_done = True
        except Exception as e:  # noqa: BLE001
            warnings.append(f"could not rename placeholder tab: {e}")
    else:  # keep
        placeholder_done = True

    # 9. Prior-version cleanup - after the build is fully complete. An
    # explicit replace_doc_id wins over the on_conflict=replace title
    # lookup. Failures are non-fatal (info, not warning);
    # ``on_conflict_action`` reports what actually happened, so a
    # wholly-failed trash reads "created", not "replaced".
    #
    # N5 (2026-07-10 retest): on_conflict=replace trashes EVERY
    # app-visible same-title prior, not just the newest - iterating on
    # a doc used to accumulate older duplicates that "replace" silently
    # skipped. ``replaced_doc_ids`` lists all of them (newest first);
    # ``replaced_doc_id`` stays the newest for callers of the singular
    # pre-N5 field.
    on_conflict_action = "created"
    replaced_doc_ids: list[str] = []
    if replace_doc_id:
        try:
            trash_drive_file(creds, replace_doc_id)
            replaced_doc_ids.append(replace_doc_id)
            on_conflict_action = "replaced"
            info.append(
                f"trashed prior version {replace_doc_id} "
                "(recoverable from Drive trash for 30 days)"
            )
        except Exception as e:  # noqa: BLE001
            info.append(f"could not trash replace_doc_id {replace_doc_id}: {e}")
    elif on_conflict == "replace":
        priors = _same_title_docs(
            creds,
            converted.get("title")
            or _expected_final_title(creds, docx_path, drive_file_id, title),
            exclude_ids=frozenset(x for x in (doc_id, drive_file_id) if x),
        )
        for prior in priors:
            # _same_title_docs only returns matches with a non-empty
            # file_id.
            prior_id = prior["file_id"]
            try:
                trash_drive_file(creds, prior_id)
                replaced_doc_ids.append(prior_id)
                on_conflict_action = "replaced"
                info.append(
                    f"trashed prior same-title doc {prior_id} "
                    "(recoverable from Drive trash for 30 days)"
                )
            except Exception as e:  # noqa: BLE001
                info.append(
                    f"could not trash prior same-title doc {prior_id}: {e}"
                )

    # 10. Refresh tabs from the live doc so the response reflects FINAL
    # state - including renamed placeholder, applied icons, and any
    # tab IDs assigned by Google.
    final_tabs = _refresh_tabs(creds, doc_id, created_tabs)

    steps_completed = ["import", "shells", "transplant", "verify"]
    if carve_ok:
        steps_completed.append("carve")
    if cosmetics_ok:
        steps_completed.append("cosmetics")
    if placeholder_done:
        steps_completed.append("placeholder")

    result = {
        "doc_id": doc_id,
        "url": converted["url"],
        # ``action`` mirrors ``on_conflict_action`` (kept for callers
        # that predate the on_conflict parameter): "replaced" only when
        # a prior version was actually trashed.
        "action": on_conflict_action,
        "on_conflict_action": on_conflict_action,
        "tabs": final_tabs,
        "moved_children": moved_blocks,
        # Parents only: with nest_by, child (Heading 2) sections are
        # counted in tabs_created but are not Heading 1s.
        "heading1_found": len(splits),
        "tabs_created": len(created_tabs),
        "placeholder": placeholder_outcome,
        "warnings": warnings,
        "info": info,
        "split_strategy_used": strategy_used,
        "completion": {
            "steps_completed": steps_completed,
            "moved_sections": [t["title"] for t, _ in plans],
            "pending_sections": [],
        },
    }
    if placeholder_veto:
        result["placeholder_veto"] = placeholder_veto
    if icons_result is not None:
        result["icons"] = icons_result
    if replaced_doc_ids:
        # Newest-first; the singular field predates N5 and keeps
        # pointing at the newest prior for existing callers.
        result["replaced_doc_id"] = replaced_doc_ids[0]
        result["replaced_doc_ids"] = replaced_doc_ids
    return result


def _cleanup_failed_staging_copy(
    creds: Credentials, doc_id: str, shell_tab_ids: list[str]
) -> None:
    """Best-effort cleanup after a PRE-transplant pipeline failure.

    Only safe to call while no transplant write has been attempted -
    at that point the working copy holds nothing that does not also
    exist in the source, so removing it cannot lose content (the
    keep-if-partial branch in ``convert_docx_to_tabbed_doc`` guards
    the other case).

    Order matters for the failure story: deleting the shells first
    restores the working copy to a plain single-tab conversion (so
    even if the trash step fails, nothing shell-riddled survives),
    then the copy itself is trashed (it was created by this call; the
    user's original file was never touched). Every step swallows its
    own errors - the caller re-raises the original failure.
    """
    if shell_tab_ids:
        try:
            docs = get_service("docs", "v1", credentials=creds)
            for tab_id in shell_tab_ids:
                try:
                    docs.documents().batchUpdate(
                        documentId=doc_id,
                        body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
                    ).execute()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    try:
        trash_drive_file(creds, doc_id)
    except Exception:  # noqa: BLE001
        pass


def _partial_failure_result(
    creds: Credentials,
    docs,
    *,
    doc_id: str,
    url: str,
    plans: list[tuple[dict, TabTransplantPlan]],
    created_tabs: list[dict],
    strategy_used: str,
    report: FidelityReport,
    error: Exception,
    source_label: str,
    executed_count: int,
    heading1_found: int,
) -> dict:
    """The keep-everything response for a failure after content started
    moving (the S2.5 contract).

    Classifies each planned section by re-verifying it against a fresh
    fetch: verified sections are ``moved_sections`` (their copies are
    proven), everything else is ``pending_sections`` (the ONLY copy
    still lives inside the placeholder tab, which is left untouched -
    nothing is carved before verify-all). If even the classification
    fetch fails, every section is reported pending: the manifest may
    under-promise but never over-promises what is safe to delete.

    EXECUTED vs VERIFIED are kept distinct (review finding on the #226
    interim manifest, which called executed tabs "fully transplanted"
    even when the failure WAS a verify shortfall): ``steps_completed``
    includes ``"transplant"`` only when every plan finished executing
    (``executed_count == len(plans)``) - so "transplant present, verify
    absent" reads unambiguously as a verify shortfall, while "transplant
    absent" means execution itself died mid-way. ``moved_sections``
    never promotes an executed-but-unverified tab.
    """
    moved_sections: list[str] = []
    pending_sections: list[str] = []
    moved_blocks = 0
    try:
        document = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.verifyExecutedTabs",
        )
    except Exception:  # noqa: BLE001
        document = None
    for tab, plan in plans:
        verified = False
        if document is not None:
            try:
                verify_tab_transplant(document, tab["tab_id"], plan)
                verified = True
            except Exception:  # noqa: BLE001
                verified = False
        if verified:
            moved_sections.append(tab["title"])
            moved_blocks += plan.block_count
        else:
            pending_sections.append(tab["title"])

    result = {
        "doc_id": doc_id,
        "url": url,
        "action": "created",
        "on_conflict_action": "created",
        "error": (
            f"conversion of {source_label} failed after a partial content "
            f"transplant: {error}. The working copy was KEPT: the already-"
            "built section tabs and the original content in the first "
            "(placeholder) tab are all still in the document. Sections in "
            "completion.pending_sections exist ONLY inside the placeholder "
            "tab - do NOT delete that tab. To recover, re-run the "
            "conversion, or move the pending sections yourself with "
            "gdocs_append_to_tab."
        ),
        "tabs": _refresh_tabs(creds, doc_id, created_tabs),
        "moved_children": moved_blocks,
        "heading1_found": heading1_found,
        "tabs_created": len(created_tabs),
        "placeholder": "kept",
        "warnings": list(report.warnings),
        "info": list(report.notes),
        "split_strategy_used": strategy_used,
        "completion": {
            "steps_completed": (
                ["import", "shells", "transplant"]
                if plans and executed_count == len(plans)
                else ["import", "shells"]
            ),
            "moved_sections": moved_sections,
            "pending_sections": pending_sections,
        },
    }
    if _is_rate_limit_cause(error):
        # A2 safety net: a 429-budget-exhaustion partial failure is
        # RETRYABLE by re-running from the source (which still exists in
        # full); the job runner requeues instead of dying terminally.
        result["rate_limited"] = True
    return result


def _is_rate_limit_cause(error: BaseException | None) -> bool:
    """True when the failure chain bottoms out in a Docs HTTP 429.

    The runner's requeue decision (A2): a rate-limit death is transient
    by definition (the quota refills every minute), so the job may
    safely re-run from its still-intact source instead of reporting a
    terminal error. Walks ``__cause__`` because the pre-transplant path
    wraps the HttpError in a RuntimeError."""
    seen: BaseException | None = error
    while seen is not None:
        if (
            isinstance(seen, HttpError)
            and getattr(seen, "status_code", None) == 429
        ):
            return True
        seen = seen.__cause__
    return False


def _refresh_tabs(
    creds: Credentials, doc_id: str, created_tabs: list[dict]
) -> list[dict]:
    """Live tab list for the response (with the legacy ``id`` alias);
    falls back to the created-tab records if the refresh fails - better
    slightly-stale data than no response."""
    try:
        outline = get_doc_outline(creds, doc_id)
        for t in outline["tabs"]:
            t["id"] = t["tab_id"]
        return outline["tabs"]
    except Exception:  # noqa: BLE001
        return [{**t, "id": t["tab_id"]} for t in created_tabs]


def _expected_final_title(
    creds: Credentials,
    docx_path: Path | None,
    drive_file_id: str | None,
    title: str | None,
) -> str:
    """The title the import step WILL assign, resolved without running
    it - the on_conflict lookups need it before/independent of import.

    Lockstep with the naming sites is now BY CONSTRUCTION: this
    predictor and every import helper (``upload_and_convert_docx`` /
    ``fetch_and_convert_drive_docx`` / ``copy_google_doc``) route
    through the single ``effective_convert_title`` (services/drive/api)
    - the N8 incident was exactly these drifting apart.
    """
    if docx_path is not None:
        return effective_convert_title(
            title, source_kind="docx", source_name=docx_path.stem
        )
    assert drive_file_id is not None  # caller validated exactly-one-of
    drive = get_service("drive", "v3", credentials=creds)
    meta = execute_with_retry(
        lambda: drive.files().get(
            fileId=drive_file_id, fields="name,mimeType"
        ).execute(),
        idempotent=True,
        op_name="drive.files.get.convertTitle.metadata",
    )
    name = meta.get("name") or ""
    kind = "gdoc" if meta.get("mimeType") == GDOC_MIME else "docx"
    return effective_convert_title(title, source_kind=kind, source_name=name)


def _same_title_docs(
    creds: Credentials, final_title: str, exclude_ids: frozenset[str]
) -> list[dict]:
    """ALL app-visible Google Docs whose title exactly matches, newest
    first (the find_doc_by_title ordering). Runs under the
    ``drive.file`` scope, so ONLY files this app created or was
    explicitly granted are considered - a same-title doc the app has
    never touched is invisible here (documented limitation of the
    on_conflict lookups)."""
    if not final_title:
        return []
    found = find_doc_by_title(creds, final_title, exact=True)
    return [
        match
        for match in (found.get("matches") or [])
        # A lingering .docx source is not a prior OUTPUT.
        if match.get("mimeType") == GDOC_MIME
        and match.get("file_id")
        and match["file_id"] not in exclude_ids
    ]


def _newest_same_title_doc(
    creds: Credentials, final_title: str, exclude_ids: frozenset[str]
) -> dict | None:
    """Newest matching prior, or None (see ``_same_title_docs``)."""
    matches = _same_title_docs(creds, final_title, exclude_ids)
    return matches[0] if matches else None


def _skipped_result(creds: Credentials, existing: dict) -> dict:
    """The no-op response for ``on_conflict="skip"`` when a same-title
    doc already exists: return the existing doc's info; convert nothing.
    """
    doc_id = existing["file_id"]
    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "action": "skipped",
        "on_conflict_action": "skipped",
        "existing_title": existing.get("name"),
        "tabs": _refresh_tabs(creds, doc_id, []),
        "moved_children": 0,
        "heading1_found": 0,
        "tabs_created": 0,
        "placeholder": "none",
        "warnings": [],
        "info": [
            "on_conflict=skip: a document with this title already exists "
            "(within this app's drive.file visibility); no conversion was "
            "performed. Returning the existing document."
        ],
        "split_strategy_used": "none",
        "completion": {
            "steps_completed": [],
            "moved_sections": [],
            "pending_sections": [],
        },
    }


def _unmoved_visible_count(
    docapp_children: list[dict], ranges: list[tuple[int, int]]
) -> int:
    """How many body elements with VISIBLE content sit outside every
    transplanted range (typically content before the first split
    heading). Such elements were never moved into any tab, so the
    placeholder tab is their only home - a nonzero count vetoes the
    ``delete`` placeholder policy."""
    covered: set[int] = set()
    for lo, hi in ranges:
        covered.update(range(lo, hi + 1))
    return sum(
        1
        for i, elem in enumerate(docapp_children)
        if i not in covered and _has_visible_content(elem)
    )


def _has_visible_content(elem: dict) -> bool:
    """True when a body element would show the user something: any
    non-paragraph element (table, TOC, ...), any paragraph with
    non-whitespace text, or any non-text inline (image, chip, break)."""
    para = elem.get("paragraph")
    if para is None:
        return True
    for pe in para.get("elements") or []:
        if "textRun" in pe:
            if (pe["textRun"].get("content") or "").strip():
                return True
        else:
            return True
    return bool(para.get("bullet"))


def _docapp_children(body_content: list[dict]) -> list[dict]:
    """Body children in the DocApp index space (the coordinate system
    split ranges use): every element except ``sectionBreak``, a
    REST-only structural record that Apps Script's ``Body.getChild``
    never counted. ``_detect_splits`` applies the SAME filter - the two
    must stay in lockstep or range indices drift."""
    return [elem for elem in body_content if "sectionBreak" not in elem]


def _flatten_splits(splits: list[_SplitPoint]) -> list[_SplitPoint]:
    """Pre-order flatten of the split forest - the same traversal order
    ``_flatten_tab_tree`` applies to the shell TabSpecs, so position i
    here corresponds to created tab i."""
    out: list[_SplitPoint] = []

    def walk(nodes: list[_SplitPoint]) -> None:
        for node in nodes:
            out.append(node)
            walk(node["children"])

    walk(splits)
    return out


def _detect_splits(
    body_content: list[dict],
    split_by: SplitBy,
    nest_by: NestBy | None = None,
) -> tuple[list[_SplitPoint], str]:
    """Walk the body and emit split points per strategy.

    ``auto`` tries ``heading_1`` → ``heading_2`` → ``page_break`` and
    returns the first non-empty result.

    ``nest_by="heading_2"`` (public callers guarantee it only arrives
    together with ``split_by="heading_1"``) emits a depth-2 forest:
    each HEADING_1 opens a parent split, each HEADING_2 after it opens
    a child split under the CURRENT parent, and non-heading content
    extends whichever node is open (the newest child once one exists,
    else the parent). A parent's range therefore ends on the last
    element before its first child's heading - the "content between
    the H1 and its first H2 stays in the parent tab" contract. A
    HEADING_2 before the first HEADING_1 has no parent: like all
    pre-first-split content, it stays behind in the placeholder tab.
    """
    if split_by == "auto":
        for strategy in ("heading_1", "heading_2", "page_break"):
            splits, _ = _detect_splits(body_content, strategy)  # type: ignore[arg-type]
            if splits:
                return splits, strategy
        return [], "auto"

    # Filter to elements that DocumentApp.Body.getChild() exposes -
    # see _docapp_children. Keeping the range index space aligned with
    # that filtered list is what lets the transplant slice
    # docapp_children directly.
    docapp_children = _docapp_children(body_content)

    splits: list[_SplitPoint] = []
    target_style = _STYLE_FOR_SPLIT.get(split_by)
    child_style = (
        _STYLE_FOR_SPLIT[nest_by]
        if nest_by is not None and split_by == "heading_1"
        else None
    )
    # The node whose last range grows as non-split content streams by.
    # Flat mode: always the newest split. Nested mode: the newest child
    # once one is open, else the newest parent.
    current: _SplitPoint | None = None
    node_count = 0

    def _new_split(title_text: str, child_idx: int) -> _SplitPoint:
        nonlocal node_count
        node_count += 1
        fallback = f"Section {node_count}"
        return _SplitPoint(
            # Truncate to the API limit so we don't fail at addDocumentTab
            # time (see _MAX_TAB_TITLE).
            title=(title_text or fallback)[:_MAX_TAB_TITLE].strip() or fallback,
            icon_emoji=None,
            ranges=[(child_idx, child_idx)],
            children=[],
        )

    for child_idx, elem in enumerate(docapp_children):
        para = elem.get("paragraph")
        if para is None:
            if current is not None:
                lo, _hi = current["ranges"][-1]
                current["ranges"][-1] = (lo, child_idx)
            continue

        is_split = False
        is_child_split = False
        title_text = ""

        if split_by in ("heading_1", "heading_2"):
            named_style = para.get("paragraphStyle", {}).get("namedStyleType")
            if named_style == target_style:
                is_split = True
                title_text = _extract_paragraph_text(para)
            elif child_style is not None and named_style == child_style and splits:
                is_child_split = True
                title_text = _extract_paragraph_text(para)
        elif split_by == "page_break":
            for pe in para.get("elements", []):
                if "pageBreak" in pe:
                    is_split = True
                    title_text = f"Page {len(splits) + 2}"
                    break

        if is_split:
            node = _new_split(title_text, child_idx)
            splits.append(node)
            current = node
        elif is_child_split:
            node = _new_split(title_text, child_idx)
            splits[-1]["children"].append(node)
            current = node
        elif current is not None:
            lo, _hi = current["ranges"][-1]
            current["ranges"][-1] = (lo, child_idx)

    return splits, split_by


def _extract_paragraph_text(para: dict) -> str:
    """Concatenate the visible text in a paragraph's element runs."""
    return "".join(
        pe.get("textRun", {}).get("content", "")
        for pe in para.get("elements", [])
    ).strip()


def _max_depth(splits: list[_SplitPoint]) -> int:
    if not splits:
        return -1
    return 1 + max(
        (_max_depth(s["children"]) for s in splits), default=-1
    )


def _existing_tab_titles(tabs: list[dict]) -> set[str]:
    """Every tab title already present in a document, at all nesting
    levels. The working copy of a native multi-tab Google Doc carries the
    source's tabs, whose titles a new shell must not duplicate."""
    titles: set[str] = set()
    for tab in tabs or []:
        title = (tab.get("tabProperties") or {}).get("title")
        if title:
            titles.add(title)
        titles |= _existing_tab_titles(tab.get("childTabs") or [])
    return titles


def _unique_tab_title(base: str, taken: set[str]) -> str:
    """``base`` if free, else the first ``base (2)`` / ``base (3)`` / ...
    not in ``taken``, kept within the tab-title length limit. Tab titles
    must be unique across a document or addDocumentTab 400s."""
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = base[: _MAX_TAB_TITLE - len(suffix)].rstrip() + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _dedupe_split_titles(flat_splits: list[_SplitPoint], existing: set[str]) -> None:
    """In place: rename each split's title so it is unique against the
    document's pre-existing tabs AND every earlier split. Pre-order
    (``flat_splits``) means a parent claims its title before its
    children. Mutates the shared _SplitPoint dicts, so the shell specs
    built from the split tree pick up the unique titles."""
    taken = set(existing)
    for split in flat_splits:
        unique = _unique_tab_title(split["title"], taken)
        split["title"] = unique
        taken.add(unique)


def _split_to_tabspec(split: _SplitPoint) -> TabSpec:
    """Convert a split point into a TabSpec for tab-shell creation.

    Tab shells are created empty - content moves in via the transplant.
    """
    spec: TabSpec = {"title": split["title"], "content": ""}
    if split["icon_emoji"]:
        spec["icon_emoji"] = split["icon_emoji"]
    if split["children"]:
        spec["children"] = [_split_to_tabspec(c) for c in split["children"]]
    return spec
