"""``POST /api/convert`` request/response contract tests (T2.x wave).

Pins the HTTP half of the convert-core data-safety contract:

- **BUG 2a**: an absent ``title`` derives from the UPLOADED filename at
  import time - never from the server temp file's random stem (the
  "perfect doc named tmpjgehtmo2" bug).
- **T2.2 default parity**: the route's ``placeholder_behavior`` default
  is ``"delete"``, identical to the ``gdocs_tab_existing_doc`` tool
  (the pipeline/tool/retrofit halves are pinned in
  ``test_docx_import_pipeline.py``).
- **T2.3**: ``on_conflict`` is exposed and validated on the HTTP path.
- **S2.5**: a partial-failure result (the pipeline returned an
  ``error`` + completion manifest instead of raising) surfaces as HTTP
  500 whose BODY carries the full recovery manifest - the caller must
  learn which sections are pending before touching the placeholder.

The auth harness mirrors ``test_api_convert_multitenancy.py`` (bearer-
header path: simplest authenticated route into the endpoint).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

_TEST_KEY_BYTES = b"test-signing-key-32-characters-long"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "user_state.db"))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver,localhost")
    from appscriptly.key_provider import InMemoryKeyProvider, with_key_provider
    with with_key_provider(InMemoryKeyProvider({
        "api_bearer": _TEST_KEY_BYTES,
        "oauth_state": _TEST_KEY_BYTES,
        "signed_url": _TEST_KEY_BYTES,
    })):
        yield


@pytest.fixture(autouse=True)
def reset_nonce_store():
    from appscriptly import http_server
    from appscriptly.crypto import NonceStore
    from appscriptly.http_server import _state
    fresh = NonceStore()
    _state._NONCE_STORE = fresh
    http_server._NONCE_STORE = fresh
    yield


def _client():
    from fastmcp import FastMCP
    from appscriptly.http_server import build_app
    return TestClient(build_app(FastMCP("stub-for-test")))


def _docx_form(filename: str = "Quarterly Report.docx"):
    return {
        "file": (
            filename,
            b"PK\x03\x04 fake docx bytes",
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
        ),
    }


def _bearer():
    return {"authorization": f"Bearer {_TEST_KEY_BYTES.decode('utf-8')}"}


def _post_convert(data=None, filename="Quarterly Report.docx", capture=None):
    """POST /api/convert on the bearer path with _convert_docx mocked;
    returns (response, captured_kwargs)."""
    captured: dict = {}

    def fake_convert(creds, **kwargs):
        captured.update(kwargs)
        if capture is not None:
            return capture
        return {"doc_id": "DOC123", "url": "https://x", "tabs": []}

    with patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        return_value="operator-creds-sentinel",
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ):
        resp = _client().post(
            "/api/convert",
            files=_docx_form(filename),
            data=data or {},
            headers=_bearer(),
        )
    return resp, captured


# ---------------------------------------------------------------------
# BUG 2a - final title from the uploaded filename, never the temp stem
# ---------------------------------------------------------------------


def test_absent_title_derives_from_uploaded_filename():
    resp, kwargs = _post_convert()
    assert resp.status_code == 200, resp.text
    assert kwargs["title"] == "Quarterly Report"
    # The temp path the pipeline reads from is NOT the title source.
    assert kwargs["docx_path"].suffix == ".docx"
    assert kwargs["title"] != kwargs["docx_path"].stem


def test_explicit_title_wins_over_filename():
    resp, kwargs = _post_convert(data={"title": "My Title"})
    assert resp.status_code == 200
    assert kwargs["title"] == "My Title"


# ---------------------------------------------------------------------
# T2.2 - placeholder_behavior exposed, default 'delete' (path parity)
# ---------------------------------------------------------------------


def test_placeholder_behavior_defaults_to_delete_on_http_path():
    resp, kwargs = _post_convert()
    assert resp.status_code == 200
    assert kwargs["placeholder_behavior"] == "delete"


def test_placeholder_behavior_passes_through_and_validates():
    resp, kwargs = _post_convert(data={"placeholder_behavior": "rename"})
    assert resp.status_code == 200
    assert kwargs["placeholder_behavior"] == "rename"

    resp, _ = _post_convert(data={"placeholder_behavior": "explode"})
    assert resp.status_code == 400
    assert "placeholder_behavior" in resp.json()["error"]


# ---------------------------------------------------------------------
# T2.3 - on_conflict exposed on the HTTP path
# ---------------------------------------------------------------------


def test_on_conflict_defaults_to_new_and_passes_through():
    resp, kwargs = _post_convert()
    assert resp.status_code == 200
    assert kwargs["on_conflict"] == "new"

    resp, kwargs = _post_convert(data={"on_conflict": "skip"})
    assert resp.status_code == 200
    assert kwargs["on_conflict"] == "skip"


def test_on_conflict_invalid_value_is_rejected_with_400():
    resp, _ = _post_convert(data={"on_conflict": "upsert"})
    assert resp.status_code == 400
    assert "on_conflict" in resp.json()["error"]


# ---------------------------------------------------------------------
# S2.5 - partial failure returns 500 WITH the recovery manifest
# ---------------------------------------------------------------------


def test_partial_failure_returns_500_with_completion_manifest():
    partial = {
        "doc_id": "KEPT_DOC",
        "url": "https://docs.google.com/document/d/KEPT_DOC/edit",
        "action": "created",
        "on_conflict_action": "created",
        "error": "conversion failed after a partial content transplant: boom",
        "tabs": [],
        "heading1_found": 9,
        "tabs_created": 9,
        "placeholder": "kept",
        "warnings": [],
        "info": [],
        "split_strategy_used": "heading_1",
        "completion": {
            "steps_completed": ["import", "shells"],
            "moved_sections": ["A", "B"],
            "pending_sections": ["C", "D"],
        },
    }
    resp, _ = _post_convert(capture=partial)
    assert resp.status_code == 500
    body = resp.json()
    # The body is the FULL envelope - the caller can see the doc id and
    # exactly which sections are unsafe to lose.
    assert body["doc_id"] == "KEPT_DOC"
    assert body["completion"]["pending_sections"] == ["C", "D"]
    assert body["placeholder"] == "kept"


def test_success_returns_200_with_result_body():
    resp, _ = _post_convert()
    assert resp.status_code == 200
    assert resp.json()["doc_id"] == "DOC123"


# ---------------------------------------------------------------------
# F (convert status hygiene): the endpoint's OWN `except HttpError`
# mirrors classify_convert_error - a 4xx keeps its status, 5xx -> 502
# ---------------------------------------------------------------------


def test_endpoint_http_error_on_cred_resolution_maps_4xx_by_status():
    """An HttpError raised in the endpoint's OWN body (here: operator
    cred resolution, BEFORE any job spawns) is caught by the endpoint's
    `except HttpError` and mapped by status - a 403 stays 403, not the
    old blanket 502. This is the mirror of the converter-thread path
    (jobs.classify_convert_error). Revert-check: 502 on main."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 403
        reason = "Forbidden"

    def boom(_data_dir):
        raise HttpError(resp=_Resp(), content=b"no access")

    with patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        side_effect=boom,
    ):
        resp = _client().post(
            "/api/convert", files=_docx_form(), data={}, headers=_bearer(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["status_code"] == 403


def test_endpoint_http_error_5xx_stays_502():
    """Regression guard: a 5xx raised in the endpoint body still maps to
    502 Bad Gateway (the carve-out is 4xx-only)."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 500
        reason = "Internal Server Error"

    def boom(_data_dir):
        raise HttpError(resp=_Resp(), content=b"boom")

    with patch(
        "appscriptly.http_server.routes.convert.load_credentials",
        side_effect=boom,
    ):
        resp = _client().post(
            "/api/convert", files=_docx_form(), data={}, headers=_bearer(),
        )
    assert resp.status_code == 502, resp.text
    assert resp.json()["status_code"] == 500
