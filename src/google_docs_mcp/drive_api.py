"""Google Drive API wrapper — currently only the .docx upload+convert path.

Drive's ``files.create`` with ``mimeType=application/vnd.google-apps.document``
plus a `.docx` media body triggers server-side conversion to a native
Google Doc, preserving tables, cell shading, colored borders, inline
images, equations, and most other Word formatting. We then operate on
the resulting Doc via the Docs API.
"""
from __future__ import annotations

from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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
