"""Wave E: lossless .docx → native-tabbed Google Doc.

Pipeline:
  1. Drive uploads + converts .docx (preserves tables, shading, borders,
     images, equations — full Word fidelity, Drive's converter handles it)
  2. We identify split points by walking the converted doc's paragraph
     stream looking for the configured heading style
  3. REST API creates empty nested tab shells (addDocumentTab with
     parentTabId) — one per split point, deeply nested per ``children``
  4. We POST the split spec to the user's deployed Apps Script Web App,
     which uses ``Element.copy()`` + ``Body.appendXxx(copy)`` to move
     content from the primary tab into the new shells — the only path
     that preserves drawings, equations, tables, and cell shading
     because no REST request type can re-emit those losslessly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest

from google.oauth2.credentials import Credentials
from appscriptly.google_clients import get_service

from . import user_store
from .config import get_webapp_url
from .credentials import current_user_id_or_none
from .services.docs.api import (
    MAX_NESTING_DEPTH,
    TabSpec,
    add_tabs_to_doc,
    delete_tab,
    get_doc_outline,
    rename_tab,
    set_tab_icons,
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

    Three input modes — pick the one that matches your environment:

    - ``docx_path``: absolute path to a local ``.docx`` on the machine
      the MCP server runs on. Works for local stdio MCP (Claude Code /
      Claude Desktop). DOES NOT work from claude.ai cloud chat — the
      remote server can't see the chat sandbox's filesystem.
    - ``drive_file_id``: Drive file ID of an already-uploaded .docx OR
      Google Doc. Use this when the document already lives on Drive.
      Note that programmatically-uploaded .docx blobs can fail Drive
      conversion with 400 conversionUnsupportedConversionPath — if
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

    Returns ``{"doc_id", "url", "tabs", "split_strategy_used", ...}``.
    """
    # Backward-compat: accept the old name drive_file_id as an alias.
    if drive_file_id is not None and drive_file_id is None:
        drive_file_id = drive_file_id

    if (docx_path is None) == (drive_file_id is None):
        raise ValueError(
            "Provide exactly one of docx_path or drive_file_id "
            "(got both, or neither)."
        )

    webapp_url = _resolve_webapp_url(user_id=user_id)
    if not webapp_url:
        # Mode-specific guidance — telling a cloud chat user to "run the
        # CLI" is useless, and telling a stdio user to "run the MCP tool"
        # is the wrong setup path. Three modes:
        #   (a) explicit user_id (REST /api/convert signed-URL caller),
        #   (b) MCP context user (cloud chat MCP tool caller), and
        #   (c) no user_id at all (stdio / operator-bearer REST caller).
        # (a) and (b) both want the MCP tool guidance.
        if user_id is not None or current_user_id_or_none() is not None:
            raise RuntimeError(
                "Workspace automation runtime not yet installed for your "
                "account. Run the gdocs_install_automation tool first; "
                "it provisions the runtime in your Workspace so Claude "
                "can build persistent workflows (including the lossless "
                "retrofit path this conversion needs)."
            )
        raise RuntimeError(
            "Apps Script Web App URL not configured. "
            "Run `google-docs-mcp setup-apps-script` for setup instructions, "
            "then save the deployment URL with "
            "`google-docs-mcp configure-webapp <URL>`."
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
    body_content = fetched["tabs"][0]["documentTab"]["body"]["content"]

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

    # 3. Cap nesting depth defensively — _detect_splits won't currently
    # produce nested splits, but a future strategy might.
    max_depth = _max_depth(splits)
    if max_depth >= MAX_NESTING_DEPTH:
        raise RuntimeError(
            f"Detected {max_depth + 1} nesting levels; "
            f"Google Docs UI allows at most {MAX_NESTING_DEPTH}."
        )

    # 4. REST creates the empty tab shells at root level — siblings of
    # the placeholder primary tab, not children of it. Gives the user a
    # sidebar of top-level section tabs instead of one collapsed root.
    primary_tab_id = fetched["tabs"][0]["tabProperties"]["tabId"]
    shell_specs = [_split_to_tabspec(s) for s in splits]
    add_tabs_to_doc(creds, doc_id, shell_specs)

    # 5. Apps Script moves content into the shells with full fidelity.
    payload = {
        "docId": doc_id,
        "splitTree": _splits_to_json(splits),
    }
    response = _call_webapp(webapp_url, payload)

    if not response.get("success", False):
        raise RuntimeError(
            f"Apps Script restructure failed at stage "
            f"'{response.get('stage', 'unknown')}': "
            f"{response.get('error', 'no error message returned')}"
        )

    # 6. Apply post-restructure icons by title match. MUST run BEFORE
    # the placeholder delete — Google returns 500 on the icon-apply
    # batchUpdate if we delete a tab and then immediately submit
    # another tab-touching batch in the same logical sequence (server
    # state hasn't settled). Race confirmed empirically on 14-section
    # converts. Order is: Apps Script → set_tab_icons → delete/rename.
    icons_result: dict | None = None
    if icons_by_title:
        try:
            icons_result = set_tab_icons(creds, doc_id, icons_by_title)
        except Exception as e:  # noqa: BLE001
            icons_result = {"error": str(e)}

    # 7. Apply the user's chosen placeholder-tab policy:
    #    delete: remove the now-empty placeholder so the sidebar shows
    #            only section tabs (default; cleanest)
    #    rename: keep it but rename to placeholder_title with an icon
    #            (useful if you want a landing/intro tab)
    #    keep:   leave as "Tab 1", do nothing (caller will edit manually)
    placeholder_warning: str | None = None
    if placeholder_behavior == "delete":
        try:
            delete_tab(creds, doc_id, primary_tab_id)
        except Exception as e:  # noqa: BLE001
            placeholder_warning = f"could not delete placeholder tab: {e}"
    elif placeholder_behavior == "rename":
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

    # Split the Apps Script warnings into real warnings (problems the
    # caller should act on) vs info notes (cosmetic things the API
    # forces on us). The "remove_failed:N:...Can't remove the last
    # paragraph in a document section." warning is structural —
    # Google Docs requires at least one paragraph per section, so the
    # script can't fully empty the placeholder tab. Always fires; never
    # actionable. Moving it to ``info`` keeps ``warnings`` meaningful.
    raw_warnings = list(response.get("warnings", []))
    info: list[str] = []
    warnings: list[str] = []
    for w in raw_warnings:
        if "Can't remove the last paragraph" in w:
            info.append(w)
        else:
            warnings.append(w)
    if placeholder_warning:
        warnings.append(placeholder_warning)

    # 8. Optional idempotency: trash the prior version so iterating on
    # a doc doesn't leave a trail of orphaned copies in Drive. Failures
    # here are non-fatal — surface as info, not warning.
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
    # state — including renamed placeholder, applied icons, and any
    # tab IDs assigned by Google. The Apps Script returns a snapshot
    # taken BEFORE steps 6 (icons) and 7 (placeholder rename/delete)
    # ran, so its titles + missing icon_emoji are stale.
    try:
        outline = get_doc_outline(creds, doc_id)
        # Keep ``id`` as alias for ``tab_id`` so callers that already
        # use the Apps-Script-snapshot shape don't break.
        for t in outline["tabs"]:
            t["id"] = t["tab_id"]
        final_tabs: list[dict] = outline["tabs"]
    except Exception:  # noqa: BLE001
        # Fallback: keep the pre-finalization snapshot if the refresh
        # fails. Better stale data than no response.
        final_tabs = response.get("tabs", [])

    result = {
        "doc_id": doc_id,
        "url": converted["url"],
        # action distinguishes a brand-new doc from one that REPLACED
        # an older doc via replace_doc_id (the old one is trashed and
        # the caller can verify the lifecycle from the payload alone).
        "action": "replaced" if replace_doc_id else "created",
        "tabs": final_tabs,
        "moved_children": response.get("movedChildren", 0),
        "warnings": warnings,
        "info": info,
        "split_strategy_used": strategy_used,
    }
    if icons_result is not None:
        result["icons"] = icons_result
    if replaced_note:
        result["replaced_doc_id"] = replace_doc_id
    return result


def _resolve_webapp_url(*, user_id: str | None = None) -> str | None:
    """HTTP mode: per-user URL from user_store. Stdio mode: local config.

    Resolution order:
      1. Explicit ``user_id`` argument — v2.1 REST signed-URL callers
         pass this in (extracted from the validated ``uid`` query param).
         Closes the v1.x deferral where REST always landed on the
         operator's URL.
      2. ``current_user_id_or_none()`` — MCP tool callers in HTTP mode
         have a FastMCP auth context; pull the Google ``sub`` from there.
      3. Fall through to ``get_webapp_url()`` — operator's local config,
         used by stdio MCP and by REST bearer-header callers (intentional;
         see ``convert_endpoint`` for the dispatch rationale).
    """
    effective_user_id = user_id or current_user_id_or_none()
    if effective_user_id is not None:
        return user_store.get_state(effective_user_id).get("apps_script_url")
    return get_webapp_url()


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

    # Filter to elements that DocumentApp.Body.getChild() exposes.
    # ``sectionBreak`` is a REST-only structural element (it describes the
    # section's page properties) and Apps Script does NOT count it as a
    # body child. If we leave it in, our indices run one ahead of what
    # Apps Script sees and the last range gets rejected as out-of-bounds.
    docapp_children = [
        elem for elem in body_content if "sectionBreak" not in elem
    ]

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

    Tab shells are created empty — content moves in via Apps Script.
    """
    spec: TabSpec = {"title": split["title"], "content": ""}
    if split["icon_emoji"]:
        spec["icon_emoji"] = split["icon_emoji"]
    if split["children"]:
        spec["children"] = [_split_to_tabspec(c) for c in split["children"]]
    return spec


def _splits_to_json(splits: list[_SplitPoint]) -> list[dict]:
    """Serialize splits for the Apps Script payload — JSON-friendly keys."""
    return [
        {
            "title": s["title"],
            "iconEmoji": s["icon_emoji"] or "",
            "ranges": [[lo, hi] for lo, hi in s["ranges"]],
            "children": _splits_to_json(s["children"]),
        }
        for s in splits
    ]


def _call_webapp(url: str, payload: dict) -> dict:
    """POST JSON to the Apps Script Web App and parse the JSON reply.

    SSRF posture (reviewed): ``url`` is NOT attacker-influenceable to an
    arbitrary/internal host. It comes from ``_resolve_webapp_url`` which is
    either (a) per-user ``apps_script_url`` from ``user_store`` — gated by
    ``user_store._valid_gas_url`` on BOTH write (save_state raises) and read
    (get_state drops tampered values), which pins ``scheme=https`` +
    ``host=script.google.com`` + the ``/macros/s/<id>/(exec|dev)`` path — or
    (b) the operator's local ``config.get_webapp_url`` (single-tenant). A
    user therefore cannot steer this POST at ``169.254.169.254``,
    ``localhost``, a private range, or a foreign host: the only reachable
    target is ``script.google.com``. So no private-IP / redirect / scheme
    guard is added here — it would be redundant against a host-pinned value
    (adding one would also not be reachable by any test without first
    defeating the storage-layer validator). If a future change ever lets a
    raw, unvalidated URL reach this function, add an SSRF guard at that new
    entry point.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Apps Script Web App returned HTTP {e.code}: {body[:500]}"
        ) from e
    except urlerror.URLError as e:
        raise RuntimeError(
            f"Could not reach Apps Script Web App at {url}: {e.reason}"
        ) from e

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "Apps Script Web App did not return JSON. "
            "Common cause: the script throws an uncaught error before "
            "the JSON response is built, so Google returns an HTML "
            "error page. First 500 chars of response:\n"
            f"{body[:500]}"
        ) from e
