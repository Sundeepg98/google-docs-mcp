"""Wave E: .docx / Google Doc → native-tabbed Google Doc (pure REST).

Pipeline:
  1. Drive uploads + converts .docx (preserves tables, shading, borders,
     images - Drive's converter handles the Word side losslessly), or
     copies an existing Google Doc.
  2. We identify split points by walking the converted doc's paragraph
     stream looking for the configured heading style.
  3. REST creates empty tab shells (addDocumentTab) - one per split
     point, nested per ``children``.
  4. The content transplant re-emits each split range into its shell
     via ``documents.batchUpdate`` (``services/docs/content_transplant``):
     read shape → write shape, table sync-points, explicit tabId on
     every request. High-fidelity with a detected-and-warned loss tail
     (equations, drawings, floating objects - see ``DROPPED_KINDS``).
  5. Only after every new tab is built AND verified does the source
     content leave the primary tab (tab delete, or range carve for the
     rename/keep placeholder policies). A failed transplant rolls the
     new shells back and trashes the working copy - the source document
     is never left half-converted.

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
from typing import Literal, TypedDict

from google.oauth2.credentials import Credentials
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
    plan_tab_transplant,
    verify_tab_transplant,
)

PlaceholderBehavior = Literal["delete", "rename", "keep"]
DEFAULT_PLACEHOLDER_TITLE = "Overview"
DEFAULT_PLACEHOLDER_ICON = "\U0001f4d1"  # 📑
from .services.drive.api import (
    DOCX_MIME,
    GDOC_MIME,
    classify_drive_file,
    copy_google_doc,
    fetch_and_convert_drive_docx,
    trash_drive_file,
    upload_and_convert_docx,
)

SplitBy = Literal["heading_1", "heading_2", "page_break", "auto"]
_STYLE_FOR_SPLIT = {
    "heading_1": "HEADING_1",
    "heading_2": "HEADING_2",
}


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
    title: str | None = None,
    tab_icons: list[str] | None = None,
    icons_by_title: dict[str, str] | None = None,
    placeholder_behavior: PlaceholderBehavior = "delete",
    placeholder_title: str = DEFAULT_PLACEHOLDER_TITLE,
    placeholder_icon: str = DEFAULT_PLACEHOLDER_ICON,
    replace_doc_id: str | None = None,
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

    ``tab_icons`` is an optional list of emoji icons assigned in
    detected-split order. If shorter than the number of splits, the
    remaining tabs get no icon. To set icons later (or by title
    match instead of order), use ``set_tab_icons``.

    ``user_id`` is accepted for REST-route compatibility (the
    ``/api/convert`` endpoint forwards the signed-URL caller's uid).
    The conversion derives identity from ``creds`` alone now that the
    per-user web-app step (which needed a per-user URL lookup) is gone.

    Returns ``{"doc_id", "url", "tabs", "split_strategy_used", ...}``.
    """
    del user_id  # accepted for the REST route's signature; unused here

    if (docx_path is None) == (drive_file_id is None):
        raise ValueError(
            "Provide exactly one of docx_path or drive_file_id "
            "(got both, or neither)."
        )

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

    # 2. Find split points in the converted doc's primary tab body.
    docs = get_service("docs", "v1", credentials=creds)
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    source_tab = fetched["tabs"][0]["documentTab"]
    body_content = source_tab["body"]["content"]

    splits, strategy_used = _detect_splits(body_content, split_by)
    if not splits:
        return {
            "doc_id": doc_id,
            "url": converted["url"],
            "action": "replaced" if replace_doc_id else "created",
            "tabs": [],
            "split_strategy_used": strategy_used,
            "note": (
                "No split points found; doc is left as a single-tab "
                f"conversion of {source_label}."
            ),
        }

    # Assign optional icons in detected-split order.
    if tab_icons:
        for i, split in enumerate(splits):
            if i < len(tab_icons) and tab_icons[i]:
                split["icon_emoji"] = tab_icons[i]

    # 3. Cap nesting depth defensively - _detect_splits won't currently
    # produce nested splits, but a future strategy might.
    max_depth = _max_depth(splits)
    if max_depth >= MAX_NESTING_DEPTH:
        raise RuntimeError(
            f"Detected {max_depth + 1} nesting levels; "
            f"Google Docs UI allows at most {MAX_NESTING_DEPTH}."
        )

    # 4. REST creates the empty tab shells at root level - siblings of
    # the placeholder primary tab, not children of it. Gives the user a
    # sidebar of top-level section tabs instead of one collapsed root.
    primary_tab_id = fetched["tabs"][0]["tabProperties"]["tabId"]
    shell_specs = [_split_to_tabspec(s) for s in splits]
    created_tabs = add_tabs_to_doc(creds, doc_id, shell_specs)["tabs"]

    # 5. Content transplant: plan every tab first (pure - planning IS
    # the fidelity preflight, so the warning list is complete before
    # any write), then execute, then VERIFY. Split ranges are
    # DocApp-child indices, so they slice the same sectionBreak-
    # filtered list _detect_splits walked.
    docapp_children = _docapp_children(body_content)
    flat_splits = _flatten_splits(splits)
    if len(flat_splits) != len(created_tabs):
        raise RuntimeError(
            f"shell creation returned {len(created_tabs)} tabs for "
            f"{len(flat_splits)} split points"
        )

    report = FidelityReport()
    if sum(1 for e in body_content if "sectionBreak" in e) > 1:
        report.count("multi_section")

    plans: list[tuple[dict, TabTransplantPlan]] = []
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
            named_styles=source_tab.get("namedStyles"),
            document_style=source_tab.get("documentStyle"),
            report=report,
        )
        plans.append((tab, plan))

    moved_blocks = 0
    try:
        # One fetch covers every shell's starting state: each tab has
        # its own index space, so writing into one tab does not move
        # another tab's insertion point (table sync-points inside the
        # executor re-fetch on their own).
        shells_doc = docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        for tab, plan in plans:
            moved_blocks += execute_tab_transplant(
                docs, doc_id, tab["tab_id"], plan, document=shells_doc
            )
        verify_doc = docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        for tab, plan in plans:
            verify_tab_transplant(verify_doc, tab["tab_id"], plan)
    except Exception as e:
        _rollback_failed_transplant(
            creds, docs, doc_id, [t["tab_id"] for t, _ in plans]
        )
        raise RuntimeError(
            f"Tab transplant failed while converting {source_label}: {e}. "
            "The partially-built tabs were removed and the working copy "
            "was trashed; the source document is untouched."
        ) from e

    # 6. Apply post-transplant icons by title match. MUST run BEFORE
    # the placeholder delete - Google returns 500 on the icon-apply
    # batchUpdate if we delete a tab and then immediately submit
    # another tab-touching batch in the same logical sequence (server
    # state hasn't settled). Race confirmed empirically on 14-section
    # converts. Order is: transplant → set_tab_icons → delete/rename.
    icons_result: dict | None = None
    if icons_by_title:
        try:
            icons_result = set_tab_icons(creds, doc_id, icons_by_title)
        except Exception as e:  # noqa: BLE001
            icons_result = {"error": str(e)}

    # 7. Apply the user's chosen placeholder-tab policy. The source
    # content leaves the primary tab only HERE, after the verify pass
    # (the transactional contract):
    #    delete: remove the whole placeholder tab, content goes with
    #            it, so the sidebar shows only section tabs (default)
    #    rename: carve the transplanted ranges out, then rename to
    #            placeholder_title with an icon (landing/intro tab)
    #    keep:   carve the transplanted ranges out, leave it as "Tab 1"
    placeholder_warning: str | None = None
    if placeholder_behavior == "delete":
        try:
            delete_tab(creds, doc_id, primary_tab_id)
        except Exception as e:  # noqa: BLE001
            placeholder_warning = f"could not delete placeholder tab: {e}"
    else:
        try:
            carve_source_ranges(
                docs,
                doc_id,
                primary_tab_id,
                docapp_children,
                [r for s in flat_splits for r in s["ranges"]],
            )
        except Exception as e:  # noqa: BLE001
            placeholder_warning = (
                f"could not carve moved content out of the placeholder tab: {e}"
            )
        if placeholder_behavior == "rename":
            try:
                rename_tab(
                    creds,
                    doc_id,
                    primary_tab_id,
                    title=placeholder_title,
                    icon_emoji=placeholder_icon,
                )
            except Exception as e:  # noqa: BLE001
                placeholder_warning = f"could not rename placeholder tab: {e}"

    # Fidelity split: DROPPED content (equations, drawings, ...) goes
    # in warnings - the caller should surface it. Visible-but-carried
    # degradations (horizontal-rule borders, numbering restarts) are
    # info notes.
    warnings: list[str] = list(report.warnings)
    info: list[str] = list(report.notes)
    if placeholder_warning:
        warnings.append(placeholder_warning)

    # 8. Optional idempotency: trash the prior version so iterating on
    # a doc doesn't leave a trail of orphaned copies in Drive. Failures
    # here are non-fatal - surface as info, not warning.
    replaced_note: str | None = None
    if replace_doc_id:
        try:
            trash_drive_file(creds, replace_doc_id)
            replaced_note = (
                f"trashed prior version {replace_doc_id} "
                "(recoverable from Drive trash for 30 days)"
            )
        except Exception as e:  # noqa: BLE001
            info.append(f"could not trash replace_doc_id {replace_doc_id}: {e}")

    # 9. Refresh tabs from the live doc so the response reflects FINAL
    # state - including renamed placeholder, applied icons, and any
    # tab IDs assigned by Google.
    try:
        outline = get_doc_outline(creds, doc_id)
        # Keep ``id`` as alias for ``tab_id`` so callers that predate
        # the REST transplant (Apps-Script-snapshot shape) don't break.
        for t in outline["tabs"]:
            t["id"] = t["tab_id"]
        final_tabs: list[dict] = outline["tabs"]
    except Exception:  # noqa: BLE001
        # Fallback: report the tabs we created if the refresh fails.
        # Better slightly-stale data than no response.
        final_tabs = [{**t, "id": t["tab_id"]} for t in created_tabs]

    result = {
        "doc_id": doc_id,
        "url": converted["url"],
        # action distinguishes a brand-new doc from one that REPLACED
        # an older doc via replace_doc_id (the old one is trashed and
        # the caller can verify the lifecycle from the payload alone).
        "action": "replaced" if replace_doc_id else "created",
        "tabs": final_tabs,
        "moved_children": moved_blocks,
        "warnings": warnings,
        "info": info,
        "split_strategy_used": strategy_used,
    }
    if icons_result is not None:
        result["icons"] = icons_result
    if replaced_note:
        result["replaced_doc_id"] = replace_doc_id
    return result


def _rollback_failed_transplant(
    creds: Credentials, docs, doc_id: str, shell_tab_ids: list[str]
) -> None:
    """Best-effort cleanup after a mid-transplant failure.

    Order matters for the failure story: deleting the shells first
    restores the working copy to a plain single-tab conversion (so
    even if the trash step fails, nothing shell-riddled survives),
    then the copy itself is trashed (it was created by this call; the
    user's original file was never touched). Every step swallows its
    own errors - the caller re-raises the original failure.
    """
    for tab_id in shell_tab_ids:
        try:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
            ).execute()
        except Exception:  # noqa: BLE001
            pass
    try:
        trash_drive_file(creds, doc_id)
    except Exception:  # noqa: BLE001
        pass


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
    body_content: list[dict], split_by: SplitBy
) -> tuple[list[_SplitPoint], str]:
    """Walk the body and emit split points per strategy.

    ``auto`` tries ``heading_1`` → ``heading_2`` → ``page_break`` and
    returns the first non-empty result.
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

    for child_idx, elem in enumerate(docapp_children):
        para = elem.get("paragraph")
        if para is None:
            if splits:
                lo, _hi = splits[-1]["ranges"][-1]
                splits[-1]["ranges"][-1] = (lo, child_idx)
            continue

        is_split = False
        title_text = ""

        if split_by in ("heading_1", "heading_2"):
            style = para.get("paragraphStyle", {})
            if style.get("namedStyleType") == target_style:
                is_split = True
                title_text = _extract_paragraph_text(para)
        elif split_by == "page_break":
            for pe in para.get("elements", []):
                if "pageBreak" in pe:
                    is_split = True
                    title_text = f"Page {len(splits) + 2}"
                    break

        if is_split:
            splits.append(
                _SplitPoint(
                    # Google Docs API rejects tab titles >50 chars with
                    # a 400 ("The tab title cannot be longer than 50
                    # characters"). Truncate to match the API limit so
                    # we don't fail at addDocumentTab time.
                    title=(title_text or f"Section {len(splits) + 1}")[:50].strip()
                    or f"Section {len(splits) + 1}",
                    icon_emoji=None,
                    ranges=[(child_idx, child_idx)],
                    children=[],
                )
            )
        elif splits:
            lo, _hi = splits[-1]["ranges"][-1]
            splits[-1]["ranges"][-1] = (lo, child_idx)

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
