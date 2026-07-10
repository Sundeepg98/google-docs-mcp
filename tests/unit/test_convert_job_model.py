"""Endpoint-level tests for the /api/convert job model (T1.1/T3.2/T3.3).

Drives the PRODUCTION app (build_app + real middleware stack) through
the new job-model surface:

- async=1 opt-in: 202 {job_id, status_url}, pre-signed multi-use status
  URL, full result via polling;
- status-URL auth: tampered signature and expired URLs get 403; a
  validly-signed unknown job gets 404;
- nonce-at-job-creation: a validation failure does not burn the signed
  URL; a burned-nonce retry with the SAME payload attaches to the
  existing job (no duplicate conversion, no duplicate docs); a
  burned-nonce request with a DIFFERENT payload is rejected;
- stalled + re-arm: a row whose process "died" (stale heartbeat) reads
  stalled on the status endpoint, and the identical re-POST re-runs it
  under the SAME job_id;
- batch fan-out (multiple files / drive_file_ids) and the
  drive_file_id single-convert mode;
- markers pass-through to the retrofit entry.

Async tests use ``with TestClient(app) as client`` so one portal event
loop persists across requests (detached job tasks live on that loop
between the 202 and the status polls).
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from starlette.testclient import TestClient

from appscriptly import job_store
from appscriptly.http_server import jobs


_TEST_KEY_BYTES = b"test-signing-key-32-characters-long"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Key provider + host allowlist + deterministic base-URL resolution.

    Mirrors test_api_convert_multitenancy.py's fixture; additionally
    unsets the base-URL overrides so minted status URLs resolve from
    the request (http://testserver/...) and clears job-model process
    state (task registry + job-store init guard).
    """
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "user_state.db"))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver,localhost")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_BASE_URL", raising=False)

    job_store._initialized_paths.clear()
    jobs._TASKS.clear()

    from appscriptly.key_provider import (
        InMemoryKeyProvider,
        with_key_provider,
    )
    with with_key_provider(InMemoryKeyProvider({
        "api_bearer": _TEST_KEY_BYTES,
        "oauth_state": _TEST_KEY_BYTES,
        "signed_url": _TEST_KEY_BYTES,
    })):
        yield

    job_store._initialized_paths.clear()
    jobs._TASKS.clear()


@pytest.fixture(autouse=True)
def reset_nonce_store():
    from appscriptly import http_server
    from appscriptly.crypto import NonceStore
    from appscriptly.http_server import _state
    fresh = NonceStore()
    _state._NONCE_STORE = fresh
    http_server._NONCE_STORE = fresh
    yield


def _build_app_under_test():
    from fastmcp import FastMCP
    from appscriptly.http_server import build_app
    return build_app(FastMCP("stub-for-jobmodel-tests"))


def _mint_qs(user_id: str = "user-A") -> str:
    from appscriptly.crypto import sign_upload_url
    minted = sign_upload_url(
        base_url="http://testserver/api/convert",
        signing_key=_TEST_KEY_BYTES,
        user_id=user_id,
    )
    return urlparse(minted["url"]).query


def _docx_form(content: bytes = b"PK\x03\x04minimaldocx", name: str = "test.docx"):
    return {"file": (name, content, _DOCX_MIME)}


def _bearer_headers() -> dict:
    return {"authorization": f"Bearer {_TEST_KEY_BYTES.decode('utf-8')}"}


def _creds_patches():
    """Patch per-user + operator credential resolution to sentinels."""
    return (
        patch(
            "appscriptly.http_server.routes.convert.get_credentials_for_user",
            return_value="per-user-creds-sentinel",
        ),
        patch(
            "appscriptly.http_server.routes.convert._resolve_client_config",
            return_value={"web": {"client_id": "X", "client_secret": "Y"}},
        ),
        patch(
            "appscriptly.http_server.routes.convert.load_credentials",
            return_value="operator-creds-sentinel",
        ),
    )


def _status_path(descriptor: dict) -> str:
    parsed = urlparse(descriptor["status_url"])
    assert parsed.path.startswith("/api/convert/status/")
    return f"{parsed.path}?{parsed.query}"


def _poll_until_terminal(client, status_path: str, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict | None = None
    while time.time() < deadline:
        resp = client.get(status_path)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job never reached a terminal status; last={last}")


# ---------------------------------------------------------------------
# async=1 opt-in + status polling
# ---------------------------------------------------------------------


def test_async_flow_end_to_end_with_mocked_converter():
    """POST async=1 -> immediate 202 {job_id, status_url}; polling the
    pre-signed status URL yields running/queued then done + the FULL
    converter result."""
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(kwargs)
        time.sleep(0.1)
        return {"doc_id": "ASYNC1", "url": "https://x", "tabs": [{"t": 1}]}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        resp = client.post(
            f"/api/convert?{_mint_qs()}",
            files=_docx_form(),
            data={"async": "1"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["job_id"]
        assert body["status"] in ("queued", "running")
        assert body["attached_to_existing_job"] is False
        assert "/api/convert/status/" in body["status_url"]
        assert body["status_url_expires_at"] > int(time.time()) + 23 * 3600

        final = _poll_until_terminal(client, _status_path(body))
        assert final["status"] == "done"
        assert final["result"] == {
            "doc_id": "ASYNC1", "url": "https://x", "tabs": [{"t": 1}],
        }
        assert final["job_id"] == body["job_id"]

    assert len(calls) == 1
    # Row persisted as done in the store too.
    row = job_store.get_job(body["job_id"])
    assert row is not None and row["status"] == "done"


def test_async_error_job_reports_error_via_status():
    app = _build_app_under_test()

    def failing_convert(creds, **kwargs):
        raise ValueError("bad document structure")

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=failing_convert,
    ), TestClient(app) as client:
        resp = client.post(
            f"/api/convert?{_mint_qs()}",
            files=_docx_form(content=b"PK\x03\x04errdoc"),
            data={"async": "1"},
        )
        assert resp.status_code == 202, resp.text
        final = _poll_until_terminal(client, _status_path(resp.json()))
        assert final["status"] == "error"
        # N9: the message lives at error.message, never error.error.
        assert final["error"] == {"message": "bad document structure"}
        assert final["error_http_status"] == 400


# ---------------------------------------------------------------------
# status-URL signature validation
# ---------------------------------------------------------------------


def test_status_url_tampered_signature_is_403():
    app = _build_app_under_test()

    def fake_convert(creds, **kwargs):
        return {"doc_id": "SIG1", "url": "https://x", "tabs": []}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        resp = client.post(
            f"/api/convert?{_mint_qs()}",
            files=_docx_form(),
            data={"async": "1"},
        )
        assert resp.status_code == 202
        path = _status_path(resp.json())

        # Flip the last signature character.
        base, _, query = path.partition("?")
        qs = parse_qs(query)
        sig = qs["sig"][0]
        qs["sig"] = [sig[:-1] + ("0" if sig[-1] != "0" else "1")]
        tampered = f"{base}?{urlencode({k: v[0] for k, v in qs.items()})}"

        r = client.get(tampered)
        assert r.status_code == 403, r.text
        assert "signature mismatch" in r.json()["error"]

        # Tampering the PATH (another job_id) also breaks the HMAC.
        other = f"{base}x?{query}"
        r = client.get(other)
        assert r.status_code == 403

        # And the untampered URL still works (multi-use, no nonce).
        assert client.get(path).status_code == 200
        assert client.get(path).status_code == 200


def test_status_url_expired_is_403_and_unknown_job_is_404():
    from appscriptly.crypto import sign_job_status_url

    app = _build_app_under_test()
    with TestClient(app) as client:
        # Valid signature over an unknown job id: authenticated, but no
        # such row -> 404.
        minted = sign_job_status_url(
            base_url="http://testserver/api/convert/status/no-such-job",
            signing_key=_TEST_KEY_BYTES,
            job_id="no-such-job",
        )
        parsed = urlparse(minted["status_url"])
        r = client.get(f"{parsed.path}?{parsed.query}")
        assert r.status_code == 404

        # Expired: signature computed over a past exp verifies the HMAC
        # but fails the expiry gate -> 403.
        import hashlib
        import hmac as hmac_mod
        past = int(time.time()) - 10
        sig = hmac_mod.new(
            _TEST_KEY_BYTES,
            f"jobstatus.job-x.{past}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        r = client.get(f"/api/convert/status/job-x?exp={past}&sig={sig}")
        assert r.status_code == 403
        assert "expired" in r.json()["error"]

        # No credentials at all -> the generic 401 (not the 403 branch).
        r = client.get("/api/convert/status/job-x")
        assert r.status_code == 401


# ---------------------------------------------------------------------
# nonce-at-job-creation + fingerprint attach
# ---------------------------------------------------------------------


def test_validation_failure_does_not_burn_the_signed_url():
    """Pre-job-model, ANY verified request consumed the nonce - even a
    400. Now a rejected request leaves the URL usable and the follow-up
    succeeds with the SAME URL."""
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(1)
        return {"doc_id": "RETRY-OK", "url": "https://x", "tabs": []}

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        bad = client.post(
            f"/api/convert?{qs}",
            files=_docx_form(),
            data={"split_by": "chapter"},  # invalid -> 400 before any job
        )
        assert bad.status_code == 400

        good = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert good.status_code == 200, good.text
        assert good.json()["doc_id"] == "RETRY-OK"
    assert len(calls) == 1


def test_burned_nonce_identical_retry_attaches_to_existing_job():
    """The T1.1 disconnect-retry contract: the first POST consumed the
    nonce at job creation; re-POSTing the IDENTICAL request on the same
    (now burned) URL attaches to that job's outcome instead of 401ing
    or converting twice."""
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(1)
        return {"doc_id": "ONCE", "url": "https://x", "tabs": []}

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        first = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert first.status_code == 200, first.text
        assert first.json() == {"doc_id": "ONCE", "url": "https://x", "tabs": []}
        assert "attached_to_existing_job" not in first.json()

        retry = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert retry.status_code == 200, retry.text
        body = retry.json()
        assert body["doc_id"] == "ONCE"
        assert body["attached_to_existing_job"] is True

    assert len(calls) == 1, "identical retry must not convert twice"


def test_burned_nonce_different_payload_is_rejected():
    app = _build_app_under_test()

    def fake_convert(creds, **kwargs):
        return {"doc_id": "FIRST", "url": "https://x", "tabs": []}

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        first = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert first.status_code == 200

        different = client.post(
            f"/api/convert?{qs}",
            files=_docx_form(content=b"PK\x03\x04completely-different"),
        )
        assert different.status_code == 401
        assert "already used" in different.json()["error"]


def test_failed_job_does_not_capture_retries():
    """N1 (2026-07-10 retest): a FAILED job must not poison the 15-min
    fingerprint window. Pre-N1 the identical retry ATTACHED to the
    failure and replayed the cached corpse; the recovery advice
    ("re-run the conversion") could not work. Now a fresh signed URL
    re-runs the conversion for real, and the burned original URL gets
    the honest 401 pointing at a fresh mint."""
    app = _build_app_under_test()
    calls = []

    def failing_convert(creds, **kwargs):
        calls.append(1)
        raise RuntimeError("pipeline exploded")

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=failing_convert,
    ), TestClient(app) as client:
        first = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert first.status_code == 500
        assert first.json()["error"] == "pipeline exploded"

        # Same burned URL: no attachable job (failures are excluded),
        # so this is a create on a consumed nonce -> honest 401 with
        # the mint-a-fresh-URL instruction. No conversion ran.
        burned = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert burned.status_code == 401
        assert "fresh signed upload URL" in burned.json()["error"]
        assert len(calls) == 1

        # Fresh URL + identical payload: a genuinely NEW job runs the
        # conversion again instead of replaying the recorded failure.
        retry = client.post(f"/api/convert?{_mint_qs()}", files=_docx_form())
        assert retry.status_code == 500
        assert "attached_to_existing_job" not in retry.json()
    assert len(calls) == 2, "the retry must actually re-run the conversion"


def test_succeeded_job_still_attaches_after_n1():
    """N1 boundary: only FAILED jobs lost attach eligibility. Success
    dedup (the T1.1 disconnect-retry contract) is unchanged; the
    stalled re-arm path is pinned by
    test_stalled_status_and_rearm_on_identical_retry."""
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(1)
        return {"doc_id": "STILL-DEDUPED", "url": "https://x", "tabs": []}

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        first = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert first.status_code == 200

        retry = client.post(f"/api/convert?{qs}", files=_docx_form())
        assert retry.status_code == 200
        assert retry.json()["attached_to_existing_job"] is True
    assert len(calls) == 1


def test_plan_input_treats_legacy_done_with_error_row_as_failed():
    """Rows persisted by a pre-N3 build could be status=done with the
    partial-failure envelope in result_json. _plan_input must treat
    those as FAILED (create a new job), not as attachable successes."""
    from appscriptly.http_server.routes.convert import _plan_input

    job_id = job_store.create_job("user-A", "fp-legacy")
    job_store.finish_done(job_id, {"doc_id": "KEPT", "error": "partial"})

    plan = _plan_input("x.docx", "fp-legacy", lambda: {}, "user-A")
    assert plan.kind == "create"

    # And a clean done row still attaches.
    clean_id = job_store.create_job("user-A", "fp-legacy-clean")
    job_store.finish_done(clean_id, {"doc_id": "OK", "tabs": []})
    plan = _plan_input("x.docx", "fp-legacy-clean", lambda: {}, "user-A")
    assert plan.kind == "attach_done" and plan.job_id == clean_id


def test_async_retry_attaches_with_same_job_id():
    app = _build_app_under_test()

    def fake_convert(creds, **kwargs):
        return {"doc_id": "A202", "url": "https://x", "tabs": []}

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        first = client.post(
            f"/api/convert?{qs}", files=_docx_form(), data={"async": "1"},
        )
        assert first.status_code == 202
        jid = first.json()["job_id"]
        _poll_until_terminal(client, _status_path(first.json()))

        retry = client.post(
            f"/api/convert?{qs}", files=_docx_form(), data={"async": "1"},
        )
        assert retry.status_code == 202
        assert retry.json()["job_id"] == jid
        assert retry.json()["attached_to_existing_job"] is True


# ---------------------------------------------------------------------
# stalled derivation + re-arm on retry
# ---------------------------------------------------------------------


def _force_stalled(job_id: str) -> None:
    """Simulate a deploy kill: row left running, heartbeat gone stale,
    and no task in this process's registry."""
    stale = int(time.time()) - job_store.STALLED_AFTER_SECONDS - 5
    with job_store._connect() as conn:
        conn.execute(
            "UPDATE convert_jobs SET status = 'running', result_json = NULL, "
            "heartbeat_at = ? WHERE job_id = ?",
            (stale, job_id),
        )
    jobs._TASKS.clear()


def test_stalled_status_and_rearm_on_identical_retry():
    """Deploy-kill semantics end to end: the status endpoint derives
    stalled from the stale heartbeat, and the identical re-POST (same
    burned URL) re-runs the job under the SAME job_id."""
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(1)
        return {"doc_id": "REBORN", "url": "https://x", "tabs": []}

    qs = _mint_qs()
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        first = client.post(
            f"/api/convert?{qs}", files=_docx_form(), data={"async": "1"},
        )
        assert first.status_code == 202
        descriptor = first.json()
        jid = descriptor["job_id"]
        _poll_until_terminal(client, _status_path(descriptor))
        assert calls == [1]

        # The machine "restarts": running row, stale heartbeat, no task.
        _force_stalled(jid)

        status = client.get(_status_path(descriptor)).json()
        assert status["status"] == "stalled"
        assert "restarted" in status["note"]

        # Identical retry on the burned URL: re-arms the SAME row.
        retry = client.post(
            f"/api/convert?{qs}", files=_docx_form(), data={"async": "1"},
        )
        assert retry.status_code == 202, retry.text
        assert retry.json()["job_id"] == jid
        assert retry.json()["attached_to_existing_job"] is True

        final = _poll_until_terminal(client, _status_path(descriptor))
        assert final["status"] == "done"
        assert final["result"]["doc_id"] == "REBORN"
    assert len(calls) == 2, "re-arm must actually re-run the conversion"


# ---------------------------------------------------------------------
# batch fan-out (T3.3)
# ---------------------------------------------------------------------


def test_batch_multiple_files_fans_out_one_job_each():
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(kwargs)
        return {"doc_id": f"B{len(calls)}", "url": "https://x", "tabs": []}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        files = [
            ("file", ("a.docx", b"PK\x03\x04aaa", _DOCX_MIME)),
            ("file", ("b.docx", b"PK\x03\x04bbb", _DOCX_MIME)),
            ("file", ("c.docx", b"PK\x03\x04ccc", _DOCX_MIME)),
        ]
        resp = client.post(f"/api/convert?{_mint_qs()}", files=files)
        assert resp.status_code == 202, resp.text
        descriptors = resp.json()["jobs"]
        assert len(descriptors) == 3
        assert [d["input"] for d in descriptors] == ["a.docx", "b.docx", "c.docx"]
        job_ids = {d["job_id"] for d in descriptors}
        assert len(job_ids) == 3, "each file gets its own job"
        assert all(d["attached_to_existing_job"] is False for d in descriptors)

        for d in descriptors:
            final = _poll_until_terminal(client, _status_path(d))
            assert final["status"] == "done"

    assert len(calls) == 3


def test_batch_rejects_title_and_replace_doc_id():
    app = _build_app_under_test()
    files = [
        ("file", ("a.docx", b"PK\x03\x04aaa", _DOCX_MIME)),
        ("file", ("b.docx", b"PK\x03\x04bbb", _DOCX_MIME)),
    ]
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, TestClient(app) as client:
        r = client.post(
            f"/api/convert?{_mint_qs()}", files=files, data={"title": "One"},
        )
        assert r.status_code == 400
        assert "title" in r.json()["error"]

        r = client.post(
            f"/api/convert?{_mint_qs('user-B')}",
            files=files,
            data={"replace_doc_id": "OLD"},
        )
        assert r.status_code == 400
        assert "replace_doc_id" in r.json()["error"]


# ---------------------------------------------------------------------
# convert-from-Drive (T3.3)
# ---------------------------------------------------------------------


def test_drive_file_id_single_sync_reuses_converter_entry():
    app = _build_app_under_test()
    captured = {}

    def fake_convert(creds, **kwargs):
        captured.update(kwargs)
        return {"doc_id": "FROMDRIVE", "url": "https://x", "tabs": []}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        resp = client.post(
            "/api/convert",
            data={"drive_file_id": "DRIVE123"},
            headers=_bearer_headers(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["doc_id"] == "FROMDRIVE"
    assert captured["drive_file_id"] == "DRIVE123"
    assert "docx_path" not in captured
    # R1 (retest 2): the HTTP drive mode shares the endpoint's
    # placeholder default (delete) - same as the upload and batch modes.
    assert captured["placeholder_behavior"] == "delete"


def test_drive_file_ids_batch():
    app = _build_app_under_test()
    seen = []

    def fake_convert(creds, **kwargs):
        seen.append(kwargs["drive_file_id"])
        return {"doc_id": f"D-{kwargs['drive_file_id']}", "url": "u", "tabs": []}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        resp = client.post(
            "/api/convert",
            data={"drive_file_ids": json.dumps(["F1", "F2"])},
            headers=_bearer_headers(),
        )
        assert resp.status_code == 202, resp.text
        descriptors = resp.json()["jobs"]
        assert [d["input"] for d in descriptors] == ["F1", "F2"]
        for d in descriptors:
            final = _poll_until_terminal(client, _status_path(d))
            assert final["status"] == "done"
    assert sorted(seen) == ["F1", "F2"]


@pytest.mark.parametrize("mode", ["upload", "batch", "drive_file_id"])
def test_every_entry_point_defaults_placeholder_delete(mode):
    """R1 regression pin, reviewer-requested form: ONE parametrized test
    over ALL entry points so the placeholder default can never drift
    per-path again. No placeholder_behavior is passed; every mode must
    forward the identical default (delete) to the converter. (The tool
    entry - gdocs_tab_existing_doc - is pinned the same way in
    services/docs/test_tools.py; the converter's own entries in
    test_docx_import_pipeline.py.)"""
    app = _build_app_under_test()
    captured_behaviors: list[str] = []

    def fake_convert(creds, **kwargs):
        captured_behaviors.append(kwargs["placeholder_behavior"])
        return {
            "doc_id": f"D{len(captured_behaviors)}", "url": "https://x",
            "tabs": [],
        }

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        if mode == "upload":
            r = client.post(
                "/api/convert", files=_docx_form(), headers=_bearer_headers(),
            )
            assert r.status_code == 200, r.text
        elif mode == "batch":
            files = [
                ("file", ("a.docx", b"PK\x03\x04pd-a", _DOCX_MIME)),
                ("file", ("b.docx", b"PK\x03\x04pd-b", _DOCX_MIME)),
            ]
            r = client.post(
                "/api/convert", files=files, headers=_bearer_headers(),
            )
            assert r.status_code == 202, r.text
            for descriptor in r.json()["jobs"]:
                _poll_until_terminal(client, _status_path(descriptor))
        else:
            r = client.post(
                "/api/convert",
                data={"drive_file_id": "F-DEFAULTS"},
                headers=_bearer_headers(),
            )
            assert r.status_code == 200, r.text

    assert captured_behaviors, "converter never ran"
    assert all(b == "delete" for b in captured_behaviors), captured_behaviors


def test_input_mode_validation():
    app = _build_app_under_test()
    with TestClient(app) as client:
        # No input at all.
        r = client.post("/api/convert", data={}, headers=_bearer_headers())
        assert r.status_code == 400
        assert "exactly one input" in r.json()["error"]

        # Two modes at once.
        r = client.post(
            "/api/convert",
            files=_docx_form(),
            data={"drive_file_id": "F1"},
            headers=_bearer_headers(),
        )
        assert r.status_code == 400
        assert "exactly one input" in r.json()["error"]

        # Malformed drive_file_ids JSON.
        r = client.post(
            "/api/convert",
            data={"drive_file_ids": "not-json"},
            headers=_bearer_headers(),
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------
# markers (T3.2 retrofit parity)
# ---------------------------------------------------------------------


def test_markers_routes_to_retrofit_entry():
    app = _build_app_under_test()
    captured = {}

    def fake_retrofit(creds, **kwargs):
        captured.update(kwargs)
        return {"doc_id": "RETRO", "url": "https://x", "tabs": [],
                "retrofit": {"markers_matched": 1, "markers_missed": []}}

    markers = [{"marker_text": "Module 1", "tab_title": "Module 1"}]
    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._retrofit_docx",
        side_effect=fake_retrofit,
    ), patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=AssertionError("plain converter must not run for markers"),
    ), TestClient(app) as client:
        resp = client.post(
            f"/api/convert?{_mint_qs()}",
            files=_docx_form(),
            data={"markers": json.dumps(markers)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["doc_id"] == "RETRO"
    assert captured["markers"] == markers


def test_markers_conflicting_split_by_rejected():
    app = _build_app_under_test()
    with TestClient(app) as client:
        r = client.post(
            "/api/convert",
            files=_docx_form(),
            data={
                "markers": json.dumps([{"marker_text": "x", "tab_title": "X"}]),
                "split_by": "page_break",
            },
            headers=_bearer_headers(),
        )
        assert r.status_code == 400
        assert "heading_1" in r.json()["error"]


# ---------------------------------------------------------------------
# async flag validation
# ---------------------------------------------------------------------


def test_invalid_async_value_rejected():
    app = _build_app_under_test()
    with TestClient(app) as client:
        r = client.post(
            "/api/convert",
            files=_docx_form(),
            data={"async": "maybe"},
            headers=_bearer_headers(),
        )
        assert r.status_code == 400
        assert "async" in r.json()["error"]


# ---------------------------------------------------------------------
# nest_by + on_conflict wiring (post-#225/#229 rebase)
# ---------------------------------------------------------------------


def test_nest_by_on_conflict_and_default_title_flow_to_converter():
    """The documented params must actually REACH the converter (the
    T3.2 silent-drop bug class), and an absent title defaults to the
    uploaded file's own stem (BUG 2a) rather than a temp-file name."""
    app = _build_app_under_test()
    captured = {}

    def fake_convert(creds, **kwargs):
        captured.update(kwargs)
        return {"doc_id": "WIRED", "url": "https://x", "tabs": []}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        resp = client.post(
            "/api/convert",
            files=_docx_form(name="quarterly report.docx"),
            data={"nest_by": "heading_2", "on_conflict": "replace"},
            headers=_bearer_headers(),
        )
        assert resp.status_code == 200, resp.text
    assert captured["nest_by"] == "heading_2"
    assert captured["on_conflict"] == "replace"
    assert captured["title"] == "quarterly report"


def test_on_conflict_reaches_retrofit_entry():
    app = _build_app_under_test()
    captured = {}

    def fake_retrofit(creds, **kwargs):
        captured.update(kwargs)
        return {"doc_id": "RETRO2", "url": "https://x", "tabs": [],
                "retrofit": {"markers_matched": 1, "markers_missed": []}}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._retrofit_docx",
        side_effect=fake_retrofit,
    ), TestClient(app) as client:
        resp = client.post(
            "/api/convert",
            files=_docx_form(),
            data={
                "markers": json.dumps(
                    [{"marker_text": "x", "tab_title": "X"}]
                ),
                "on_conflict": "skip",
            },
            headers=_bearer_headers(),
        )
        assert resp.status_code == 200, resp.text
    assert captured["on_conflict"] == "skip"


def test_retry_varying_nest_by_or_on_conflict_does_not_attach():
    """nest_by / on_conflict are OUTPUT-AFFECTING: requests that differ
    only in them must get their own jobs, never attach to each other."""
    app = _build_app_under_test()
    calls = []

    def fake_convert(creds, **kwargs):
        calls.append(
            {"nest_by": kwargs["nest_by"], "on_conflict": kwargs["on_conflict"]}
        )
        return {"doc_id": f"V{len(calls)}", "url": "https://x", "tabs": []}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        flat = client.post(f"/api/convert?{_mint_qs()}", files=_docx_form())
        assert flat.status_code == 200
        assert "attached_to_existing_job" not in flat.json()

        nested = client.post(
            f"/api/convert?{_mint_qs()}",
            files=_docx_form(),
            data={"nest_by": "heading_2"},
        )
        assert nested.status_code == 200, nested.text
        assert "attached_to_existing_job" not in nested.json()

        replacing = client.post(
            f"/api/convert?{_mint_qs()}",
            files=_docx_form(),
            data={"on_conflict": "replace"},
        )
        assert replacing.status_code == 200, replacing.text
        assert "attached_to_existing_job" not in replacing.json()

    assert len(calls) == 3, "each variant must run its own conversion"
    assert calls[0] == {"nest_by": None, "on_conflict": "new"}
    assert calls[1] == {"nest_by": "heading_2", "on_conflict": "new"}
    assert calls[2] == {"nest_by": None, "on_conflict": "replace"}


def test_nest_by_and_on_conflict_validation():
    app = _build_app_under_test()
    with TestClient(app) as client:
        r = client.post(
            "/api/convert", files=_docx_form(),
            data={"nest_by": "heading_3"}, headers=_bearer_headers(),
        )
        assert r.status_code == 400 and "nest_by" in r.json()["error"]

        r = client.post(
            "/api/convert", files=_docx_form(),
            data={"nest_by": "heading_2", "split_by": "page_break"},
            headers=_bearer_headers(),
        )
        assert r.status_code == 400
        assert "split_by='heading_1'" in r.json()["error"]

        r = client.post(
            "/api/convert", files=_docx_form(),
            data={"on_conflict": "overwrite"}, headers=_bearer_headers(),
        )
        assert r.status_code == 400 and "on_conflict" in r.json()["error"]

        r = client.post(
            "/api/convert", files=_docx_form(),
            data={
                "nest_by": "heading_2",
                "markers": json.dumps([{"marker_text": "x", "tab_title": "X"}]),
            },
            headers=_bearer_headers(),
        )
        assert r.status_code == 400
        assert "nest_by is not supported together with markers" in r.json()["error"]


def test_partial_failure_envelope_maps_to_500_with_body():
    """S2.5 x N3: a pipeline that died after content started moving
    RETURNS its kept-doc envelope with an ``error`` field. The sync
    path signals 500 while keeping the recovery data (byte-identical to
    the pre-job-model endpoint); the STATUS endpoint reports it as a
    terminal ``status="error"`` (N3: done means success - the retest
    caught machine consumers reading a partial failure as a win) with
    the FULL envelope attached for recovery."""
    app = _build_app_under_test()
    envelope = {
        "doc_id": "KEPT", "url": "https://x", "tabs": [],
        "error": "transplant died after move started",
        "completion": {
            "steps_completed": ["import", "shells", "transplant"],
            "moved_sections": ["A"], "pending_sections": ["B"],
        },
    }

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        return_value=envelope,
    ), TestClient(app) as client:
        sync = client.post(f"/api/convert?{_mint_qs()}", files=_docx_form())
        assert sync.status_code == 500, sync.text
        assert sync.json()["completion"]["pending_sections"] == ["B"]
        assert sync.json()["doc_id"] == "KEPT"

        run = client.post(
            f"/api/convert?{_mint_qs('user-B')}",
            files=_docx_form(content=b"PK\x03\x04partial2"),
            data={"async": "1"},
        )
        assert run.status_code == 202
        final = _poll_until_terminal(client, _status_path(run.json()))
        # N3: the quota-death/partial-failure job polls as ERROR - a
        # poller may trust status=="done" as success - and the whole
        # recovery envelope (kept doc id, completion manifest) rides
        # under ``error`` with the sync path's HTTP status beside it.
        # N9: the message is at error.message; error.error must not
        # exist (consumers were double-reading result.error.error).
        assert final["status"] == "error"
        assert final["error"]["message"].startswith("transplant died")
        assert "error" not in final["error"]
        assert final["error"]["doc_id"] == "KEPT"
        assert final["error"]["completion"]["pending_sections"] == ["B"]
        assert final["error_http_status"] == 500
        assert "not deduplicated" in final["note"]
        # The SYNC contract is untouched by N9: the envelope's
        # historical top-level "error" key stays (asserted above via
        # the 500 body's completion/doc_id + here explicitly).
        assert sync.json()["error"].startswith("transplant died")


def test_happy_path_still_polls_done():
    """N3 boundary: success is still ``done`` + result."""
    app = _build_app_under_test()

    def fake_convert(creds, **kwargs):
        return {"doc_id": "CLEAN", "url": "https://x", "tabs": [],
                "warnings": [], "completion": {"steps_completed": ["import"]}}

    p1, p2, p3 = _creds_patches()
    with p1, p2, p3, patch(
        "appscriptly.http_server.routes.convert._convert_docx",
        side_effect=fake_convert,
    ), TestClient(app) as client:
        run = client.post(
            f"/api/convert?{_mint_qs()}", files=_docx_form(), data={"async": "1"},
        )
        assert run.status_code == 202
        final = _poll_until_terminal(client, _status_path(run.json()))
        assert final["status"] == "done"
        assert final["result"]["doc_id"] == "CLEAN"
        assert "error" not in final
