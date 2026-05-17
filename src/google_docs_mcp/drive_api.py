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

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC_MIME = "application/vnd.google-apps.document"

# Drive enforces a 50 MB upload limit and a 1.02M-character cap on the
# resulting Doc. We surface a friendly error if the source exceeds the
# upload size.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


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
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")
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

    drive = build("drive", "v3", credentials=creds)
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
    drive = build("drive", "v3", credentials=creds)

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
    drive = build("drive", "v3", credentials=creds)
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


def classify_drive_file(creds: Credentials, drive_file_id: str) -> str:
    """Return the mime type of a Drive file. Used to route to the
    right ingestion function (raw .docx vs already-converted Google Doc)."""
    drive = build("drive", "v3", credentials=creds)
    meta = drive.files().get(
        fileId=drive_file_id, fields="mimeType"
    ).execute()
    return meta.get("mimeType", "")


def trash_drive_file(creds: Credentials, drive_file_id: str) -> None:
    """Move a Drive file to trash (recoverable for 30 days).

    Used by ``convert_docx_to_tabbed_doc(replace_doc_id=...)`` to
    sweep the old version after the new conversion succeeds — keeps
    Drive tidy while iterating without permanent data loss.
    """
    drive = build("drive", "v3", credentials=creds)
    drive.files().update(
        fileId=drive_file_id, body={"trashed": True}
    ).execute()
