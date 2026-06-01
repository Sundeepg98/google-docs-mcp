"""Google Drive API wrapper for .docx upload + convert.

Two entry points:

- ``upload_and_convert_docx`` — local-file path: read a .docx from disk,
  upload to Drive with conversion to Google Doc. Used when the MCP is
  called with a filesystem path (Claude Code, Claude Desktop).
- ``fetch_and_convert_drive_docx`` — Drive-ID path: read a .docx that
  already lives in the user's Drive (uploaded by some other app, e.g.
  Claude.ai cloud chat's Drive connector), then upload+convert under
  our own app's ownership. Used when the MCP is called from an
  environment that can't pass local file paths.

Both produce the same downstream result: a Google Doc owned by our
OAuth user, suitable for Docs API operations.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC_MIME = "application/vnd.google-apps.document"

# Drive enforces a 50 MB upload limit and a 1.02M-character cap on the
# resulting Doc. We surface a friendly error if the source exceeds the
# upload size.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _escape_q_literal(value: str) -> str:
    """Escape a user string for embedding in a Drive ``q=`` string literal.

    Drive's query DSL wraps string operands in single quotes; a literal
    ``'`` (or ``\\``) inside the operand must be backslash-escaped or it
    closes the literal early — both a correctness bug (``Bob's Doc``) and
    an injection vector (a crafted name could append ``q`` clauses). We
    escape backslash FIRST (so we don't double-escape the backslashes we
    add for quotes), then the single quote. Shared by every ``q=``
    builder (``find_doc_by_title`` name match + the multi-filter
    ``find_file``) so the escape rule lives in exactly one place.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")

# ---------------------------------------------------------------------
# Export (files.export) — Google-native → portable format mapping.
# ---------------------------------------------------------------------
#
# Drive's ``files.export`` ONLY works on Google-native editor files
# (Docs / Sheets / Slides / Drawings) — it converts the live editor
# document to a downloadable format. A binary blob already on Drive (a
# .pdf, a .png, an uploaded .docx) is NOT exportable — it has no
# editor representation to render; ``get_media`` (raw download) is the
# path for those, which a future tool can add. We reject the binary
# case up front with a clean message rather than let Drive 403.
#
# ``_EXPORT_FORMATS`` maps a friendly format token → the export MIME
# type Drive expects. ``_EXPORTABLE_BY_SOURCE`` constrains which tokens
# are valid for each Google-native source type (Drive 400s on an
# invalid pairing — e.g. exporting a Doc as ``xlsx`` — so we validate
# client-side for a useful error). Subset of Google's documented export
# formats, curated to the genuinely useful ones.
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
GSLIDES_MIME = "application/vnd.google-apps.presentation"
GDRAWING_MIME = "application/vnd.google-apps.drawing"

# format token -> export MIME type
_EXPORT_FORMATS: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": DOCX_MIME,
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "txt": "text/plain",
    "html": "text/html",
    "epub": "application/epub+zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "png": "image/png",
    "svg": "image/svg+xml",
}

# Google-native source MIME -> the set of format tokens valid for it.
# Mirrors Google's documented per-type export support.
_EXPORTABLE_BY_SOURCE: dict[str, frozenset[str]] = {
    GDOC_MIME: frozenset({"pdf", "docx", "odt", "rtf", "txt", "html", "epub"}),
    GSHEET_MIME: frozenset({"pdf", "xlsx", "ods", "csv", "tsv", "html"}),
    GSLIDES_MIME: frozenset({"pdf", "pptx", "odp", "txt"}),
    GDRAWING_MIME: frozenset({"pdf", "png", "svg"}),
}

# A short, human label per Google-native source type for error text.
_SOURCE_LABEL: dict[str, str] = {
    GDOC_MIME: "Google Doc",
    GSHEET_MIME: "Google Sheet",
    GSLIDES_MIME: "Google Slides presentation",
    GDRAWING_MIME: "Google Drawing",
}

# Default file extension per format token (for naming the exported file
# when the caller doesn't supply one). Single-fragment formats only —
# csv/tsv on a multi-sheet Sheet export just the first sheet (Drive's
# documented behavior); the extension is still correct.
_FORMAT_EXTENSION: dict[str, str] = {
    "pdf": ".pdf", "docx": ".docx", "odt": ".odt", "rtf": ".rtf",
    "txt": ".txt", "html": ".html", "epub": ".epub",
    "xlsx": ".xlsx", "ods": ".ods", "csv": ".csv", "tsv": ".tsv",
    "pptx": ".pptx", "odp": ".odp", "png": ".png", "svg": ".svg",
}


def upload_and_convert_docx(
    creds: Credentials,
    docx_path: Path,
    title: str | None = None,
) -> dict:
    """Upload a local ``.docx`` file to Drive and have Drive convert it.

    Returns ``{"doc_id": str, "url": str, "title": str}``. The resulting
    Google Doc is owned by the OAuth user; subsequent Docs API
    operations on it work under the standard ``documents`` scope.
    """
    if not docx_path.exists():
        raise FileNotFoundError(
            f"docx_path not found: {docx_path}. The 'docx_path' parameter "
            "only works when the MCP server can see the file on its own "
            "filesystem — i.e. when running locally as a stdio MCP host "
            "(Claude Code / Claude Desktop). From claude.ai cloud chat the "
            "server cannot see your sandbox's filesystem; instead either "
            "(a) call get_signed_upload_url and POST the .docx bytes to "
            "the returned URL via your Python sandbox, or (b) upload to "
            "Drive first and pass drive_file_id."
        )
    if docx_path.suffix.lower() != ".docx":
        raise ValueError(
            f"Expected a .docx file (got '{docx_path.suffix}'). "
            "Older .doc files aren't accepted by Drive's converter; "
            "convert to .docx first via Word or Google Docs."
        )
    size = docx_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File is {size / 1024 / 1024:.1f} MB; "
            f"Drive upload limit for conversion is {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    drive = get_service("drive", "v3", credentials=creds)
    media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
    body = {
        "name": title or docx_path.stem,
        "mimeType": GDOC_MIME,
    }
    file = drive.files().create(
        body=body, media_body=media, fields="id,name"
    ).execute()
    doc_id = file["id"]
    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "title": file["name"],
    }


def fetch_and_convert_drive_docx(
    creds: Credentials,
    drive_file_id: str,
    title: str | None = None,
) -> dict:
    """Read a .docx already in Drive (any owner) and re-create as a Google Doc.

    The source file's bytes are streamed via Drive's ``files.get_media``
    using our ``drive.readonly`` scope, then re-uploaded via
    ``files.create`` with ``mimeType=GDOC_MIME`` so the conversion runs
    under our app's ownership (``drive.file`` scope). The original .docx
    is left in place — we never modify or delete it.

    This is the workflow for Claude.ai cloud chat: the user attaches
    or generates a .docx in chat, cloud chat uploads it to Drive via
    the Anthropic Drive connector (different app, different scopes),
    then hands the file ID to this tool.
    """
    drive = get_service("drive", "v3", credentials=creds)

    meta = drive.files().get(
        fileId=drive_file_id, fields="id,name,mimeType,size"
    ).execute()
    if meta.get("mimeType") != DOCX_MIME:
        raise ValueError(
            f"Drive file {drive_file_id!r} is not a .docx "
            f"(mimeType: {meta.get('mimeType')!r}). "
            "Convert to .docx via Word or Google Docs first."
        )
    size = int(meta.get("size") or 0)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Drive file is {size / 1024 / 1024:.1f} MB; "
            f"Drive's conversion limit is {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    buf = io.BytesIO()
    request = drive.files().get_media(fileId=drive_file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    buf.seek(0)

    fallback_title = meta["name"]
    if fallback_title.lower().endswith(".docx"):
        fallback_title = fallback_title[:-5]

    media = MediaIoBaseUpload(buf, mimetype=DOCX_MIME, resumable=False)
    file = drive.files().create(
        body={"name": title or fallback_title, "mimeType": GDOC_MIME},
        media_body=media,
        fields="id,name",
    ).execute()
    doc_id = file["id"]
    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "title": file["name"],
        "source_drive_file_id": drive_file_id,
    }


def copy_google_doc(
    creds: Credentials,
    google_doc_id: str,
    title: str | None = None,
) -> dict:
    """Make a working copy of an existing Google Doc (no .docx conversion).

    Used when the user's Drive already has a Google Doc (e.g. because
    they uploaded a .docx via the Drive web UI with auto-convert ON,
    which discards the raw .docx bytes). We can't re-import — there
    are no .docx bytes to re-import. Instead we copy the Google Doc
    and restructure the copy, leaving the original untouched.

    The copy is created with ``drive.files.copy()`` and inherits the
    user's ownership. Our restructure pipeline then modifies the copy
    in place via REST + Apps Script — same code path as the .docx
    conversion case, just without the initial mime-type conversion.
    """
    drive = get_service("drive", "v3", credentials=creds)
    meta = drive.files().get(
        fileId=google_doc_id, fields="id,name,mimeType"
    ).execute()
    if meta.get("mimeType") != GDOC_MIME:
        raise ValueError(
            f"Drive file {google_doc_id!r} is not a Google Doc "
            f"(mimeType: {meta.get('mimeType')!r}). For .docx files "
            "use fetch_and_convert_drive_docx instead."
        )

    fallback_title = (title or meta["name"]) + " (tabified)"
    new_file = drive.files().copy(
        fileId=google_doc_id,
        body={"name": fallback_title},
        fields="id,name",
    ).execute()
    new_id = new_file["id"]
    return {
        "doc_id": new_id,
        "url": f"https://docs.google.com/document/d/{new_id}/edit",
        "title": new_file["name"],
        "source_google_doc_id": google_doc_id,
    }


def untrash_drive_file(creds: Credentials, drive_file_id: str) -> dict:
    """Restore a Drive file from trash to its original location.

    Inverse of ``trash_drive_file``. Sets ``trashed=False`` via
    ``files.update``. Same graceful-error semantics: 404 (file not
    found) and 403 (app_not_authorized) return as soft-failure dicts
    instead of raising, so batch restores can skip-and-continue.

    Idempotent: untrashing a not-currently-trashed file succeeds and
    flags ``was_already_active: true``.

    Returns:
        Success: ``{file_id, name, mimeType, trashed: False,
        was_already_active: bool}``.
        Soft-failure: ``{file_id, trashed: <current>, reason,
        message}`` where ``reason`` is ``"not_found"`` or
        ``"app_not_authorized"``.

        Recovery window: Drive auto-purges trashed files after 30
        days. Beyond that, the file is gone permanently and this
        returns ``not_found``.
    """
    drive = get_service("drive", "v3", credentials=creds)

    try:
        # PR-Δ3.5: gdocs_untrash_file is idempotent=True; wrap.
        # 404/403 propagate to the existing handler (retry policy only
        # covers 429/5xx).
        before = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="id,name,mimeType,trashed"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "trashed": False,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the "
                    "ID; the file may have been permanently deleted "
                    "(beyond the 30-day trash window) or the OAuth user "
                    "lacks any access to it."
                ),
            }
        raise

    was_already_active = not bool(before.get("trashed"))

    try:
        # PR-Δ3.5: setting trashed=False twice is a true no-op; safe to retry.
        updated = execute_with_retry(
            lambda: drive.files().update(
                fileId=drive_file_id,
                body={"trashed": False},
                fields="id,name,mimeType,trashed",
            ).execute(),
            idempotent=True,
            op_name="drive.files.update.untrash",
        )
    except HttpError as e:
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "name": before.get("name"),
                    "mimeType": before.get("mimeType"),
                    "trashed": bool(before.get("trashed")),
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks write access to this file — it "
                        "wasn't created by this app. drive.file scope "
                        "only permits writes to app-created files. To "
                        "untrash, the file's owner must do it via the "
                        "Drive UI."
                    ),
                }
        raise

    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "mimeType": updated.get("mimeType"),
        "trashed": bool(updated.get("trashed")),
        "was_already_active": was_already_active,
    }


def find_doc_by_title(
    creds: Credentials,
    query: str,
    *,
    exact: bool = False,
    include_trashed: bool = False,
    page_size: int = 50,
    verify_writable: bool = False,
) -> dict:
    """Search Drive for Google Docs / .docx files matching a title.

    Newest-first by modified_time. Each match includes whether it's
    trashed and (if ``verify_writable=True``) whether this OAuth app's
    drive.file scope can actually write to it — which is the same
    test that determines whether ``trash_drive_file`` /
    ``untrash_drive_file`` / ``move_to_folder`` will succeed.

    Args:
        query: title text to match.
        exact: True = exact match (``name = 'X'``); False = substring
            (``name contains 'X'``).
        include_trashed: False (default) excludes trashed files.
        page_size: max results to return (Drive API caps at 100).
        verify_writable: False (default; v2.2.1+) — pure read; result
            ``owned_by_app`` is ``None`` (unknown). Pass True to opt
            into a batched no-op-update PROBE per match that triggers
            Drive's drive.file scope check (the SAME check that
            trash/untrash/move runs), populating ``owned_by_app`` as
            ``True``/``False``. Costs one extra batched HTTP request
            per call AND mutates the Drive audit log (the probe is a
            write at the API level even if the value doesn't change).

            **Default flipped to False in v2.2.1 (R33 audit Gap #3 /
            CQRS):** the tool wrapping this function is annotated
            ``readonly=True``, so the default behavior MUST be a pure
            read. Callers who genuinely need the writability check can
            opt in explicitly.

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time,
        trashed, owned_by_app}, ...], "count": int}``.
        ``owned_by_app`` is ``True``/``False`` if probed, ``None`` if
        ``verify_writable=False`` (the default).
    """
    drive = get_service("drive", "v3", credentials=creds)

    # Escape single quotes (and backslashes) inside the query — Drive's q
    # DSL requires quoting them with a backslash. Shared helper so the
    # rule lives in one place (also used by find_file).
    safe_query = _escape_q_literal(query)
    operator = "=" if exact else "contains"
    q_parts = [f"name {operator} '{safe_query}'"]
    q_parts.append(
        "(mimeType = 'application/vnd.google-apps.document' OR "
        "mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')"
    )
    if not include_trashed:
        q_parts.append("trashed = false")
    q = " and ".join(q_parts)

    # PR-Δ3.5: gdocs_find_doc_by_title is readonly=True, idempotent=True.
    resp = execute_with_retry(
        lambda: drive.files().list(
            q=q,
            orderBy="modifiedTime desc",
            pageSize=min(max(page_size, 1), 100),
            fields="files(id,name,mimeType,modifiedTime,trashed)",
        ).execute(),
        idempotent=True,
        op_name="drive.files.list",
    )

    matches: list[dict] = []
    for f in resp.get("files", []):
        matches.append({
            "file_id": f["id"],
            "name": f.get("name", ""),
            "mimeType": f.get("mimeType", ""),
            "modified_time": f.get("modifiedTime", ""),
            "trashed": bool(f.get("trashed")),
            "owned_by_app": None,  # filled below if verify_writable
        })

    # Probe writability with a batched no-op update per match. Setting
    # ``trashed`` to its current value is a true no-op (state doesn't
    # change) but still triggers Drive's drive.file scope check — the
    # SAME check that trash_drive_file's real update triggers. If the
    # probe succeeds, the file is writable under this app's scope; if
    # it 403s with appNotAuthorizedToFile, it isn't.
    #
    # NOTE: ``capabilities.canTrash`` and ``capabilities.canEdit`` are
    # USER-LEVEL signals — they reflect what the OAuth user is allowed
    # to do, NOT what this app's scope permits. They wrongly report
    # True for files the user owns but uploaded outside this app.
    # That mismatch is why a no-op probe is the only reliable check.
    if verify_writable and matches:
        write_results: dict[str, bool] = {}

        def make_callback(fid: str):
            def cb(_request_id: str, _response: Any, exception: Any) -> None:
                if exception is None:
                    write_results[fid] = True
                elif isinstance(exception, HttpError) and exception.status_code == 403:
                    # Any 403 means we can't write. The specific
                    # reason we care about is appNotAuthorizedToFile,
                    # but any 403 is "not writable for our purposes."
                    write_results[fid] = False
                else:
                    # Unknown error — be conservative and say "we
                    # don't know" rather than claim writable.
                    write_results[fid] = False
            return cb

        batch = drive.new_batch_http_request()
        for m in matches:
            probe_req = drive.files().update(
                fileId=m["file_id"],
                body={"trashed": m["trashed"]},
                fields="id",
            )
            batch.add(probe_req, callback=make_callback(m["file_id"]))
        batch.execute()

        for m in matches:
            m["owned_by_app"] = write_results.get(m["file_id"], False)

    return {"matches": matches, "count": len(matches)}


def find_file(
    creds: Credentials,
    query: str = "",
    *,
    mime_type: str | None = None,
    full_text: str | None = None,
    parent_folder_id: str | None = None,
    exact: bool = False,
    include_trashed: bool = False,
    page_size: int = 50,
    verify_writable: bool = False,
) -> dict:
    """Search APP-ACCESSIBLE Drive files of ANY type, with optional filters.

    The generalization of ``find_doc_by_title``: that function hardcodes a
    Google-Doc / ``.docx`` mimeType filter (so it silently hides Sheets,
    Slides, PDFs, folders); this drops that hardcoding and exposes the
    filter as an OPTIONAL ``mime_type`` parameter, plus a ``full_text``
    content-contains filter and a ``parent_folder_id`` folder-scope
    filter. With no filters at all it lists the most-recently-modified
    app-accessible files of every type.

    **CORPUS LIMITATION — read this.** This searches ONLY files THIS app
    has CREATED or OPENED — i.e. the per-file set the ``drive.file`` scope
    grants visibility to. It is **NOT a whole-Drive search**: arbitrary
    files in the user's Drive that this app never touched are invisible to
    ``files.list`` under ``drive.file`` and will NOT appear, no matter the
    filters. (A whole-Drive find requires the RESTRICTED
    ``drive.readonly`` / ``drive.metadata.readonly`` scope and is
    intentionally not built here.) For the app's own workflow — re-find a
    Sheet / Slides / PDF it produced earlier to export, share, move, or
    trash — the app-accessible corpus is exactly the right (and only
    in-scope) surface.

    Args:
        query: Optional name text to match. Empty / whitespace (the
            default) applies NO name filter — useful for "list my recent
            Sheets" (``mime_type`` only) or "what's in this folder"
            (``parent_folder_id`` only). Single quotes are escaped.
        mime_type: Optional EXACT Drive mimeType to filter to, e.g.
            ``"application/vnd.google-apps.spreadsheet"`` (Sheets),
            ``"application/vnd.google-apps.presentation"`` (Slides),
            ``"application/pdf"`` (PDF),
            ``"application/vnd.google-apps.folder"`` (folders only).
            Omit to match all types. Drive matches mimeType exactly (no
            wildcards). Single quotes are escaped.
        full_text: Optional ``fullText contains`` filter — matches the
            file's indexed CONTENT / title / metadata (Drive's full-text
            index), not just the name. Useful for "find the doc that
            mentions 'Q3 revenue'". Single quotes are escaped.
        parent_folder_id: Optional folder ID to scope the search to —
            only files whose parents include this folder are returned
            (``'<id>' in parents``). Combine with ``mime_type`` for
            "Sheets in this folder". Single quotes are escaped.
        exact: When a ``query`` is given, True = exact name match
            (``name = 'X'``); False (default) = substring
            (``name contains 'X'``). Ignored when ``query`` is empty.
        include_trashed: False (default) excludes trashed files.
        page_size: max results (Drive caps at 100).
        verify_writable: False (default) — pure read; ``owned_by_app`` is
            ``None``. True opts into the SAME batched no-op-update probe
            ``find_doc_by_title`` uses, populating ``owned_by_app`` per
            match (and writing the Drive audit log). The tool wrapper is
            ``readonly=True``, so the default MUST stay a pure read
            (CQRS — R33 Gap #3).

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time, trashed,
        owned_by_app}, ...], "count": int}`` — identical shape to
        ``find_doc_by_title`` so the two are drop-in interchangeable for
        consumers. ``owned_by_app`` is ``True``/``False`` if probed,
        else ``None``.

    Raises:
        ValueError: no filter at all was supplied AND a name match was
            expected — actually we permit the all-empty case (recent
            files), so this only fires defensively if ``page_size`` is
            non-positive after clamping (it can't be — clamped to >=1).
            In practice ``find_file`` does not raise on input; an
            over-broad call just returns recent files.
    """
    drive = get_service("drive", "v3", credentials=creds)

    # Build the q= clauses from whichever filters were supplied. Every
    # user-supplied string operand is escaped via _escape_q_literal to
    # preserve the single-quote-escape security property (a name/folder
    # containing ``'`` must not break out of — or inject into — the DSL).
    q_parts: list[str] = []

    if query and query.strip():
        operator = "=" if exact else "contains"
        q_parts.append(f"name {operator} '{_escape_q_literal(query.strip())}'")

    if mime_type and mime_type.strip():
        # Exact match — Drive's mimeType operand takes ``=`` (no wildcard).
        q_parts.append(f"mimeType = '{_escape_q_literal(mime_type.strip())}'")

    if full_text and full_text.strip():
        q_parts.append(
            f"fullText contains '{_escape_q_literal(full_text.strip())}'"
        )

    if parent_folder_id and parent_folder_id.strip():
        # Folder-scope: Drive's documented form is ``'<id>' in parents``.
        q_parts.append(
            f"'{_escape_q_literal(parent_folder_id.strip())}' in parents"
        )

    if not include_trashed:
        q_parts.append("trashed = false")

    # If the ONLY clause is the trashed filter (caller passed no name /
    # mime / fullText / parent), that's a valid "recent app-accessible
    # files" browse — Drive accepts a q of just ``trashed = false``. When
    # include_trashed is also True we'd have an empty q; Drive treats an
    # empty/None q as "all files", which is the intended browse-all.
    q = " and ".join(q_parts) if q_parts else None

    # Pure read (readonly=True, idempotent=True) — same retry posture as
    # find_doc_by_title's list call.
    resp = execute_with_retry(
        lambda: drive.files().list(
            q=q,
            orderBy="modifiedTime desc",
            pageSize=min(max(page_size, 1), 100),
            fields="files(id,name,mimeType,modifiedTime,trashed)",
        ).execute(),
        idempotent=True,
        op_name="drive.files.list.find_file",
    )

    matches: list[dict] = []
    for f in resp.get("files", []):
        matches.append({
            "file_id": f["id"],
            "name": f.get("name", ""),
            "mimeType": f.get("mimeType", ""),
            "modified_time": f.get("modifiedTime", ""),
            "trashed": bool(f.get("trashed")),
            "owned_by_app": None,  # filled below if verify_writable
        })

    # Identical writability-probe to find_doc_by_title (see that
    # function for the full rationale: capabilities.* are user-level and
    # unreliable; a batched no-op update is the only accurate drive.file
    # writability test). Kept inline rather than extracted because the
    # extraction (two consumers now) is a judgment call best left to the
    # rule-of-three / a dedicated refactor PR, not folded into a feature.
    if verify_writable and matches:
        write_results: dict[str, bool] = {}

        def make_callback(fid: str):
            def cb(_request_id: str, _response: Any, exception: Any) -> None:
                if exception is None:
                    write_results[fid] = True
                elif isinstance(exception, HttpError) and exception.status_code == 403:
                    write_results[fid] = False
                else:
                    write_results[fid] = False
            return cb

        batch = drive.new_batch_http_request()
        for m in matches:
            probe_req = drive.files().update(
                fileId=m["file_id"],
                body={"trashed": m["trashed"]},
                fields="id",
            )
            batch.add(probe_req, callback=make_callback(m["file_id"]))
        batch.execute()

        for m in matches:
            m["owned_by_app"] = write_results.get(m["file_id"], False)

    return {"matches": matches, "count": len(matches)}


def move_to_folder(
    creds: Credentials, drive_file_id: str, folder_id: str
) -> dict:
    """Move a Drive file from its current parents into ``folder_id``.

    Uses ``files.update`` with ``addParents``/``removeParents`` — moves
    in place, no copy. Soft-failure on 403 app_not_authorized and 404
    not_found, matching ``trash_drive_file``'s contract so batch
    workflows can skip-and-continue.

    Returns:
        Success: ``{file_id, name, mimeType, parents: [...]}``.
        Soft-failure: ``{file_id, reason, message, parents?}`` where
        ``reason`` is one of:
        - ``"not_found"`` — file_id doesn't resolve
        - ``"folder_not_found"`` — folder_id doesn't resolve
        - ``"app_not_authorized"`` — drive.file scope can't write
    """
    drive = get_service("drive", "v3", credentials=creds)

    try:
        # PR-Δ3.5: gdocs_move_to_folder is idempotent=True.
        before = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id,
                fields="id,name,mimeType,parents",
            ).execute(),
            idempotent=True,
            op_name="drive.files.get",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the ID."
                ),
            }
        raise

    # Sanity-check the folder exists and is actually a folder. Catching
    # this here gives a clean reason instead of relying on Drive's
    # error message for an invalid addParents value.
    try:
        # PR-Δ3.5: folder existence check is a pure read.
        folder_meta = execute_with_retry(
            lambda: drive.files().get(
                fileId=folder_id, fields="id,mimeType"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get.folder",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "reason": "folder_not_found",
                "message": (
                    f"Target folder {folder_id!r} not found. Verify the ID; "
                    "shared-with-me folders may need the user to add them "
                    "to My Drive first."
                ),
            }
        raise
    if folder_meta.get("mimeType") != "application/vnd.google-apps.folder":
        return {
            "file_id": drive_file_id,
            "reason": "folder_not_found",
            "message": (
                f"Target id {folder_id!r} is not a folder "
                f"(mimeType: {folder_meta.get('mimeType')!r})."
            ),
        }

    current_parents = before.get("parents", []) or []
    if folder_id in current_parents and len(current_parents) == 1:
        # Already in the target folder, no-op.
        return {
            "file_id": drive_file_id,
            "name": before.get("name"),
            "mimeType": before.get("mimeType"),
            "parents": current_parents,
            "note": "file was already in the target folder; no move performed",
        }

    try:
        # PR-Δ3.5: moving to the same folder twice is a no-op; safe to retry.
        updated = execute_with_retry(
            lambda: drive.files().update(
                fileId=drive_file_id,
                addParents=folder_id,
                removeParents=",".join(current_parents) if current_parents else None,
                fields="id,name,mimeType,parents",
            ).execute(),
            idempotent=True,
            op_name="drive.files.update.move",
        )
    except HttpError as e:
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "name": before.get("name"),
                    "mimeType": before.get("mimeType"),
                    "parents": current_parents,
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks write access — file wasn't created "
                        "by this app. drive.file scope only permits writes "
                        "to app-created files."
                    ),
                }
        raise

    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "mimeType": updated.get("mimeType"),
        "parents": updated.get("parents", []),
    }


def create_folder(
    creds: Credentials,
    name: str,
    parent_folder_id: str | None = None,
) -> dict:
    """Create a Drive folder via ``files.create`` (folder mimeType).

    Uses ``files.create`` with
    ``mimeType="application/vnd.google-apps.folder"`` — the documented
    way to create a folder through the Drive API (a folder is just a
    file whose mimeType is the folder type). Optionally nests the new
    folder inside ``parent_folder_id``; omitting it lands the folder in
    Drive root (My Drive).

    The created folder is owned by the OAuth user and — because this
    app created it — is fully writable under the ``drive.file`` scope.
    That makes it a natural destination for ``move_to_folder``: a doc
    created by this app can be filed into a folder created by this app
    without ever touching ``drive.full``.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope.
        name: The folder's display name. Empty / whitespace rejected
            client-side (Drive would create a folder literally named
            "Untitled folder" otherwise, which is rarely intended).
        parent_folder_id: Optional parent folder Drive ID. When given,
            the new folder is created INSIDE it (``parents=[id]``).
            When omitted (default), the folder lands in Drive root.

    Returns:
        ``{folder_id, name, url, parent_folder_id}`` — a flat shape the
        agent can act on. ``folder_id`` feeds straight into
        ``move_to_folder`` (as ``folder_id``) to file documents into the
        new folder. ``url`` deep-links to the folder in the Drive UI.
        ``parent_folder_id`` echoes the parent back (``None`` when the
        folder was created in root) for confirmation.

    Raises:
        ValueError: ``name`` empty / whitespace. Cheap rejection before
            the Drive round-trip.
        HttpError: any non-2xx from Drive — e.g. 404 when
            ``parent_folder_id`` doesn't resolve, or 403
            ``appNotAuthorizedToFile`` when the parent wasn't created by
            this app (``drive.file`` can't write into a folder it
            doesn't own). The tool-layer envelope
            (``_format_http_error``) renders this as a structured
            response.

    Note:
        ``files.create`` is NOT idempotent — calling twice creates two
        distinct folders with the same name (Drive permits duplicate
        names; folders are keyed by ID, not name). The tool wrapper is
        annotated ``idempotent=False`` accordingly, and the call is NOT
        wrapped in ``execute_with_retry`` — a transient retry after a
        request that actually landed would create a duplicate folder.
    """
    if not name or not name.strip():
        raise ValueError(
            "name cannot be empty — Drive requires a folder name. "
            "(An empty name would create a folder literally titled "
            "'Untitled folder'.)"
        )

    drive = get_service("drive", "v3", credentials=creds)
    body: dict[str, Any] = {
        "name": name.strip(),
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        body["parents"] = [parent_folder_id]

    # NOT idempotent — single attempt (matches create_presentation /
    # create_spreadsheet). No execute_with_retry: replaying a create
    # that already landed would spawn a duplicate folder.
    created = drive.files().create(
        body=body,
        fields="id,name",
    ).execute()
    folder_id = created["id"]
    return {
        "folder_id": folder_id,
        "name": created.get("name", name.strip()),
        "url": f"https://drive.google.com/drive/folders/{folder_id}",
        "parent_folder_id": parent_folder_id,
    }


def export_doc(
    creds: Credentials,
    drive_file_id: str,
    export_format: str,
    output_name: str | None = None,
) -> dict:
    """Export a Google-native file to a portable format, stored back in Drive.

    Symmetric inverse of the import side (``upload_and_convert_docx`` /
    ``fetch_and_convert_drive_docx``): those bring a ``.docx`` IN and
    convert it to a Google Doc; this takes a Google-native editor file
    (Doc / Sheet / Slides / Drawing) and renders it OUT to PDF / Office /
    OpenDocument / text via Drive's ``files.export`` endpoint.

    **Why store the result in Drive instead of returning bytes.** An MCP
    tool returns a JSON envelope, not a binary stream — raw export bytes
    don't fit (and a large PDF would blow the context window). So this
    mirrors the established binary-output pattern in this codebase
    (``as_encode_video`` uploads its MP4 to Drive and returns the file
    ID): the exported bytes are uploaded via ``files.create`` as a NEW,
    standalone Drive file (a real ``.pdf`` / ``.docx`` / etc., NOT a
    Google-native editor doc), and we return its ID + links. The new
    file is app-created, so it's fully writable under ``drive.file`` —
    the caller can immediately ``gdocs_share_file`` it, ``gdocs_move_to_folder``
    it, or hand the ``download_url`` to the user. The source file is
    never modified.

    **Scope — pure ``drive.file``, no new grant.** Reading the source via
    ``files.export`` needs only that this app can SEE the file (it
    created or opened it — the ``drive.file`` per-file grant); creating
    the exported file is a ``files.create`` (always allowed under
    ``drive.file``). No ``drive.readonly`` / ``drive`` needed.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope.
        drive_file_id: The Google-native file to export. Must be a Doc /
            Sheet / Slides / Drawing this app can access. A binary blob
            (an existing ``.pdf``/``.png``/uploaded ``.docx``) is NOT
            exportable — it has no editor representation; this returns a
            ``not_exportable`` soft-failure for those.
        export_format: A friendly token — one of (Doc) ``pdf docx odt rtf
            txt html epub``; (Sheet) ``pdf xlsx ods csv tsv html``;
            (Slides) ``pdf pptx odp txt``; (Drawing) ``pdf png svg``.
            Case-insensitive. Validated against the source type before
            the Drive round-trip (Drive 400s on an invalid pairing).
        output_name: Optional name for the exported Drive file. When
            omitted, defaults to the source file's name with the format's
            extension appended (e.g. ``"Q3 Plan"`` → ``"Q3 Plan.pdf"``).

    Returns:
        Success: ``{source_file_id, source_mime_type, export_format,
        export_mime_type, exported_file_id, name, url, download_url,
        size_bytes}``. ``url`` is the new file's Drive ``webViewLink``;
        ``download_url`` is its ``webContentLink`` (a direct-download
        link — how the caller/user gets the actual bytes without a
        bespoke server route). ``size_bytes`` is the exported file's
        size (``None`` if Drive didn't report it).
        Soft-failure (returned as data, not raised): ``{source_file_id,
        reason, message}`` where ``reason`` is ``"not_found"`` (id
        doesn't resolve), ``"app_not_authorized"`` (source not accessible
        to this app under ``drive.file``), or ``"not_exportable"`` (the
        source is a binary blob / unsupported type with no editor
        representation to export).

    Raises:
        ValueError: ``export_format`` is not a recognized token, OR it's
            recognized but invalid for the source's type (e.g. ``xlsx``
            on a Doc). Cheap rejection before the export round-trip.
        HttpError: any non-2xx Drive does NOT classify above propagates
            so genuine bugs surface; the tool-layer ``_format_http_error``
            renders it through the standard envelope.

    Note:
        ``files.export`` is capped by Drive at **10 MB** of exported
        content (Google's documented limit for the export endpoint —
        distinct from the 50 MB upload cap). A larger export returns a
        Drive error that propagates. The ``files.create`` upload of the
        result is a fresh file each call (NOT idempotent), so — like
        ``create_folder`` — the create is NOT wrapped in
        ``execute_with_retry`` and the tool is annotated
        ``idempotent=False``. The read-side ``files.get`` IS retried
        (pure read).
    """
    token = (export_format or "").strip().lower()
    if token not in _EXPORT_FORMATS:
        raise ValueError(
            f"export_format {export_format!r} is not recognized. "
            f"Supported formats: {sorted(_EXPORT_FORMATS)}."
        )
    target_mime = _EXPORT_FORMATS[token]

    drive = get_service("drive", "v3", credentials=creds)

    # Read the source's type + name first: (a) confirm it's a
    # Google-native exportable type, (b) validate the format pairing,
    # (c) derive a default output name. A pure read — retry it.
    try:
        meta = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="id,name,mimeType"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get.export_source",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "source_file_id": drive_file_id,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the ID; "
                    "the OAuth user may lack any access to it."
                ),
            }
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "source_file_id": drive_file_id,
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks access to this file — it wasn't "
                        "created or opened by this app. drive.file scope "
                        "only sees app-accessible files. The file's owner "
                        "must export it via the Drive UI (File → Download)."
                    ),
                }
        raise

    source_mime = meta.get("mimeType", "")
    allowed = _EXPORTABLE_BY_SOURCE.get(source_mime)
    if allowed is None:
        # Not a Google-native editor type → has no export representation.
        return {
            "source_file_id": drive_file_id,
            "reason": "not_exportable",
            "message": (
                f"File {drive_file_id!r} (mimeType {source_mime!r}) is not a "
                "Google-native editor file, so it has no export representation. "
                "files.export only works on Google Docs / Sheets / Slides / "
                "Drawings. A binary blob already on Drive (.pdf, .png, an "
                "uploaded .docx) is downloaded as-is, not exported."
            ),
        }
    if token not in allowed:
        label = _SOURCE_LABEL.get(source_mime, "this file type")
        raise ValueError(
            f"Cannot export a {label} as {token!r}. "
            f"Valid formats for a {label}: {sorted(allowed)}."
        )

    # Stream the export bytes into memory. files.export returns the
    # converted bytes directly (no intermediate Drive file). Mirrors
    # fetch_and_convert_drive_docx's MediaIoBaseDownload loop.
    buf = io.BytesIO()
    request = drive.files().export_media(fileId=drive_file_id, mimeType=target_mime)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    buf.seek(0)

    # Name the exported file. Strip a trailing same-extension off the
    # source name first so "Plan.pdf" doesn't become "Plan.pdf.pdf" when
    # the source happened to be named with the extension.
    ext = _FORMAT_EXTENSION[token]
    if output_name and output_name.strip():
        name = output_name.strip()
        if not name.lower().endswith(ext):
            name = name + ext
    else:
        base = meta.get("name", "export")
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
        name = base + ext

    # Upload the exported bytes as a NEW standalone Drive file (not a
    # Google-native doc — mimeType is the portable target). NOT
    # idempotent: a retry after a landed create would duplicate. Single
    # attempt, matching create_folder / create_presentation.
    media = MediaIoBaseUpload(buf, mimetype=target_mime, resumable=False)
    created = drive.files().create(
        body={"name": name, "mimeType": target_mime},
        media_body=media,
        fields="id,name,size,webViewLink,webContentLink",
    ).execute()
    exported_id = created["id"]

    size_raw = created.get("size")
    return {
        "source_file_id": drive_file_id,
        "source_mime_type": source_mime,
        "export_format": token,
        "export_mime_type": target_mime,
        "exported_file_id": exported_id,
        "name": created.get("name", name),
        "url": created.get(
            "webViewLink", f"https://drive.google.com/file/d/{exported_id}/view"
        ),
        "download_url": created.get("webContentLink"),
        "size_bytes": int(size_raw) if size_raw is not None else None,
    }


def is_file_trashed(creds: Credentials, drive_file_id: str) -> bool:
    """Return whether the Drive file is currently in trash.

    Used by read-side tools (``get_doc_outline``, ``read_*``) to
    surface ``trashed: true`` in responses so callers know they're
    working with a hidden file. Best-effort — if the lookup itself
    fails (e.g. file deleted permanently), returns False.
    """
    drive = get_service("drive", "v3", credentials=creds)
    try:
        # PR-Δ3.5: pure read used by readonly tool wrappers.
        meta = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="trashed"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get.trashed_check",
        )
        return bool(meta.get("trashed"))
    except Exception:  # noqa: BLE001
        return False


def classify_drive_file(creds: Credentials, drive_file_id: str) -> str:
    """Return the mime type of a Drive file. Used to route to the
    right ingestion function (raw .docx vs already-converted Google Doc)."""
    drive = get_service("drive", "v3", credentials=creds)
    # PR-Δ3.5: pure read; classifies file type so callers can route ingestion.
    meta = execute_with_retry(
        lambda: drive.files().get(
            fileId=drive_file_id, fields="mimeType"
        ).execute(),
        idempotent=True,
        op_name="drive.files.get.classify",
    )
    return meta.get("mimeType", "")


def trash_drive_file(creds: Credentials, drive_file_id: str) -> dict:
    """Move a Drive file to trash (recoverable for 30 days).

    Idempotent: setting ``trashed=True`` on an already-trashed file
    succeeds without error. Uses ``files.update`` (trash) — NEVER
    ``files.delete`` (permanent purge), so the operation is reversible.

    Returns:
        Success: ``{file_id, name, mimeType, trashed: True,
        was_already_trashed: bool}``.
        Soft-failure (NOT raised, returned as data so batch cleanups
        can skip-and-continue): ``{file_id, trashed: False, reason,
        message}`` where ``reason`` is:
        - ``"not_found"`` — file id doesn't resolve (404 on get)
        - ``"app_not_authorized"`` — OAuth app lacks write access
          (file wasn't created by this app; drive.file scope can only
          touch files this app owns; 403 appNotAuthorizedToFile)

        Other errors still raise ``HttpError`` so genuine bugs surface.
    """
    drive = get_service("drive", "v3", credentials=creds)

    # Read current state first so we can flag idempotent no-ops AND
    # detect non-existent IDs early with a clean reason.
    try:
        # PR-Δ3.5: gdocs_trash_file is destructive=True but idempotent=True
        # (trashing twice = same end state). Wrap.
        before = execute_with_retry(
            lambda: drive.files().get(
                fileId=drive_file_id, fields="id,name,mimeType,trashed"
            ).execute(),
            idempotent=True,
            op_name="drive.files.get",
        )
    except HttpError as e:
        if e.status_code == 404:
            return {
                "file_id": drive_file_id,
                "trashed": False,
                "reason": "not_found",
                "message": (
                    f"Drive file {drive_file_id!r} not found. Check the "
                    "ID; the file may have been permanently deleted or "
                    "the OAuth user lacks any access to it."
                ),
            }
        raise

    was_already_trashed = bool(before.get("trashed"))

    try:
        # PR-Δ3.5: trashing an already-trashed file is a no-op; safe to retry.
        updated = execute_with_retry(
            lambda: drive.files().update(
                fileId=drive_file_id,
                body={"trashed": True},
                fields="id,name,mimeType,trashed",
            ).execute(),
            idempotent=True,
            op_name="drive.files.update.trash",
        )
    except HttpError as e:
        # drive.file scope only grants write to files this app created.
        # Trashing a file uploaded externally (e.g. via the Drive web UI
        # by the user, or by another app) returns 403
        # appNotAuthorizedToFile. Return that as data, not as an
        # exception, so a batch cleanup can skip-and-continue.
        if e.status_code == 403:
            reasons = [
                (d.get("reason") or "").strip()
                for d in (getattr(e, "error_details", None) or [])
                if isinstance(d, dict)
            ]
            if "appNotAuthorizedToFile" in reasons or "appNotAuthorizedToFile" in str(e):
                return {
                    "file_id": drive_file_id,
                    "name": before.get("name"),
                    "mimeType": before.get("mimeType"),
                    "trashed": False,
                    "reason": "app_not_authorized",
                    "message": (
                        "OAuth app lacks write access to this file — it "
                        "wasn't created by this app. drive.file scope "
                        "only permits writes to app-created files. To "
                        "trash, the file's owner must do it via the "
                        "Drive UI."
                    ),
                }
        raise

    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "mimeType": updated.get("mimeType"),
        "trashed": bool(updated.get("trashed")),
        "was_already_trashed": was_already_trashed,
    }
