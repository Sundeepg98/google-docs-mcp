"""BUG 2a (2026-07-09) - the doc's FINAL title must exist at import time.

The pipeline names the Google Doc at ``files.create`` from
``docx_path.stem``, and the REST endpoint stages the upload in a
``NamedTemporaryFile`` - so a caller that omitted ``title`` got a doc
permanently named like "tmpjgehtmo2" (and a mid-pipeline death left an
unrecognizable orphan). The endpoint must default the title to the
UPLOADED file's stem so what reaches ``files.create`` is already final.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

_TEST_KEY_BYTES = b"test-signing-key-32-characters-long"

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Same isolation shape as test_api_convert_multitenancy: fixed keys
    via InMemoryKeyProvider + throwaway state paths + trusted testserver."""
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "user_state.db"))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver,localhost")
    from appscriptly.key_provider import InMemoryKeyProvider, with_key_provider

    with with_key_provider(
        InMemoryKeyProvider(
            {
                "api_bearer": _TEST_KEY_BYTES,
                "oauth_state": _TEST_KEY_BYTES,
                "signed_url": _TEST_KEY_BYTES,
            }
        )
    ):
        yield


def _post_convert(files: dict, data: dict | None = None):
    from fastmcp import FastMCP

    from appscriptly.http_server import build_app

    client = TestClient(build_app(FastMCP("stub-for-test")))
    captured: dict = {}

    def fake_convert(creds, **kwargs):
        captured.update(kwargs)
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        return_value="operator-creds-sentinel",
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ):
        resp = client.post(
            "/api/convert",
            files=files,
            data=data or {},
            headers={"authorization": f"Bearer {_TEST_KEY_BYTES.decode('utf-8')}"},
        )
    return resp, captured


def test_default_title_is_the_uploaded_filename_stem():
    resp, captured = _post_convert(
        {"file": ("Quarterly Report.docx", b"PK\x03\x04x", _DOCX_MIME)}
    )
    assert resp.status_code == 200, resp.text
    assert captured["title"] == "Quarterly Report", (
        "with no explicit title the doc must be created under the "
        "uploaded file's name, NEVER the tmpXXXX staging file's stem"
    )


def test_explicit_title_still_wins():
    resp, captured = _post_convert(
        {"file": ("Quarterly Report.docx", b"PK\x03\x04x", _DOCX_MIME)},
        data={"title": "My Custom Title"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["title"] == "My Custom Title"
