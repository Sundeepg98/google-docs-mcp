"""Co-located tests for services/drive/api.py.

Per test architect (Round 3 review of M3 Phase A — PR #94): each
service folder must have a co-located ``test_api.py`` that exercises
what it can directly, rather than letting coverage come entirely from
indirect paths. PR #95 established this pattern for ``services/docs``
(29 tests against pure helpers); this file does the same for
``services/drive`` to close the Phase-B test layout gap.

**Scope note — drive has fewer pure helpers than docs.**

Unlike ``services/docs/api.py`` (which has multiple module-level pure
helpers like ``_flatten_tab_tree`` and ``_find_tab_by_id``),
``services/drive/api.py``'s functions all call ``get_service(...)``
near the top and then operate on the returned Drive Resource. The
test architect's stop condition applies: don't extract pure helpers
just to enable testing if no obvious extraction exists.

What we CAN test directly here:

1. **Module-level constants** (DOCX_MIME, GDOC_MIME, MAX_UPLOAD_BYTES) —
   trivial but pins the public surface so a stray edit gets caught.
2. **Pre-API validation in ``upload_and_convert_docx``** — the
   FileNotFoundError / extension-check / size-limit branches all
   raise BEFORE the ``get_service`` call, so they're pure-function
   tests in disguise.
3. **The Drive ``q=`` query DSL built inside ``find_doc_by_title``** —
   exercised via the M2 ``with_google_api_client(InMemoryGoogleAPIClient)``
   pattern. We stub ``drive.files().list()`` and assert on the ``q``
   kwarg the call site passes. This catches single-quote escape
   regressions (a security-relevant code path) and operator/trashed-
   filter logic without mocking the actual Drive API.

Tests for the larger consumer paths (trash/untrash/move_to_folder
soft-failure contracts) already live in
``tests/unit/test_soft_failure_contracts.py``, which ship-d1 updated
in M3 Phase B to import from ``google_docs_mcp.services.drive.api``.
Not duplicated here.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from google_docs_mcp.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from google_docs_mcp.services.drive.api import (
    DOCX_MIME,
    GDOC_MIME,
    MAX_UPLOAD_BYTES,
    find_doc_by_title,
    upload_and_convert_docx,
)


# ---------------------------------------------------------------------
# Module-level constants — pin the public surface
# ---------------------------------------------------------------------


def test_docx_mime_is_the_openxml_wordprocessingml_string():
    """The DOCX_MIME constant must be the exact MIME type Drive expects
    for a Microsoft Word document. A stray edit (e.g. truncation, typo)
    would cause every .docx upload to be misclassified."""
    assert DOCX_MIME == (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    )


def test_gdoc_mime_is_the_google_apps_document_string():
    assert GDOC_MIME == "application/vnd.google-apps.document"


def test_max_upload_bytes_is_50_mib():
    """50 MiB is the Drive converter's upload cap. The value is asserted
    explicitly so a change forces a CHANGELOG note + comms (e.g. updating
    user-facing docs that quote the limit)."""
    assert MAX_UPLOAD_BYTES == 50 * 1024 * 1024


# ---------------------------------------------------------------------
# upload_and_convert_docx — pre-API validation branches
#
# These raise BEFORE calling get_service(...), so they're testable as
# pure functions without any Google API mocking.
# ---------------------------------------------------------------------


def test_upload_and_convert_docx_raises_FileNotFoundError_for_missing_path(tmp_path):
    """A missing path must raise FileNotFoundError with a message that
    explains the cloud-vs-local restriction. The message wording is
    user-facing — if it changes, the CLI/MCP error surface changes."""
    missing = tmp_path / "does-not-exist.docx"
    with pytest.raises(FileNotFoundError) as exc:
        upload_and_convert_docx(MagicMock(), missing)
    msg = str(exc.value)
    assert str(missing) in msg
    # The error explains the cloud-chat workaround so users hit by
    # this can self-serve. If this assertion ever breaks, the doc
    # surface broke too — see also the README.
    assert "cloud chat" in msg.lower() or "get_signed_upload_url" in msg


def test_upload_and_convert_docx_rejects_non_docx_extension(tmp_path):
    """Older .doc / .odt files aren't accepted by Drive's converter.
    Catching this early gives a useful error rather than letting Drive
    return a generic 400."""
    bad = tmp_path / "old-format.doc"
    bad.write_bytes(b"PK\x03\x04stub")  # exists, but wrong extension
    with pytest.raises(ValueError, match=r"\.docx"):
        upload_and_convert_docx(MagicMock(), bad)


def test_upload_and_convert_docx_rejects_oversize_file(tmp_path, monkeypatch):
    """Files above MAX_UPLOAD_BYTES must be refused locally. Patching
    Path.stat to lie about size keeps the test fast (no need to write
    a 50 MiB fixture)."""
    big = tmp_path / "huge.docx"
    big.write_bytes(b"PK\x03\x04stub")

    real_stat = Path.stat
    fake_size = MAX_UPLOAD_BYTES + 1

    def lying_stat(self, **kw):
        result = real_stat(self, **kw)
        # st_size is read-only on the os.stat_result; shadow via MagicMock.
        m = MagicMock(wraps=result)
        m.st_size = fake_size
        return m

    monkeypatch.setattr(Path, "stat", lying_stat)
    with pytest.raises(ValueError, match=r"MB"):
        upload_and_convert_docx(MagicMock(), big)


def test_upload_and_convert_docx_accepts_extension_case_insensitively(tmp_path):
    """``.DOCX`` (uppercase) should be accepted — the check normalizes
    via ``.lower()``. Without this, a user dragging a file from
    Windows Explorer can hit a confusing rejection."""
    upper = tmp_path / "shouty.DOCX"
    upper.write_bytes(b"PK\x03\x04stub")
    # We don't care about the success path here — only that we get
    # PAST the extension check. The next gate is the size check
    # (which passes for our tiny stub), and after that we'd hit
    # get_service. Stub the API client so we don't actually call Drive.
    drive = MagicMock()
    drive.files().create().execute.return_value = {"id": "ID", "name": "shouty"}
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = upload_and_convert_docx(MagicMock(), upper)
    assert result["doc_id"] == "ID"


# ---------------------------------------------------------------------
# find_doc_by_title — Drive q= query DSL construction
#
# Exercises the security-relevant single-quote escape + operator/trashed
# logic by capturing the ``q`` kwarg passed to drive.files().list().
# This is the M2 with_google_api_client pattern (PR #92) applied to
# expose pure logic embedded inside a consumer-path function.
# ---------------------------------------------------------------------


@pytest.fixture
def stubbed_drive_with_empty_list():
    """A Drive Resource stub whose files().list().execute() returns an
    empty file set. Just enough for find_doc_by_title to complete and
    let us inspect what q-string it built."""
    drive = MagicMock()
    drive.files().list().execute.return_value = {"files": []}
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def _last_q_passed_to_list(drive: MagicMock) -> str:
    """Extract the ``q`` kwarg from the most recent files().list() call."""
    # MagicMock records call args; the last list() invocation that
    # received q= is the one find_doc_by_title made.
    for call in reversed(drive.files().list.call_args_list):
        if "q" in call.kwargs:
            return call.kwargs["q"]
    raise AssertionError("no files().list() call captured a q= kwarg")


def test_find_doc_by_title_substring_match_uses_contains_operator(
    stubbed_drive_with_empty_list,
):
    """Default mode (exact=False) uses Drive's ``contains`` operator,
    which is a substring match. ``exact=True`` switches to ``=``."""
    find_doc_by_title(
        MagicMock(), "my doc", exact=False, verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "name contains 'my doc'" in q
    assert "name = 'my doc'" not in q


def test_find_doc_by_title_exact_match_uses_equals_operator(
    stubbed_drive_with_empty_list,
):
    find_doc_by_title(
        MagicMock(), "my doc", exact=True, verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "name = 'my doc'" in q
    assert "name contains 'my doc'" not in q


def test_find_doc_by_title_escapes_single_quotes_in_query_string(
    stubbed_drive_with_empty_list,
):
    """SECURITY-RELEVANT: a title containing a ``'`` must not break out
    of Drive's q= DSL string literal. The implementation replaces
    ``'`` with ``\\'`` (Drive's documented escape). Without this, a
    user title like ``Bob's Doc`` would close the literal mid-string."""
    find_doc_by_title(
        MagicMock(), "Bob's Doc", exact=True, verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    # The query must contain the backslash-escaped form, not the raw apostrophe.
    assert "Bob\\'s Doc" in q
    # And the literal must be properly closed: count of unescaped
    # single-quotes is even. (Two for the name literal + two each for
    # the mime literals = 6, all paired.)
    unescaped = q.replace("\\'", "")
    assert unescaped.count("'") % 2 == 0, (
        f"Unescaped single-quote count is odd; the query DSL is broken: q={q!r}"
    )


def test_find_doc_by_title_excludes_trashed_by_default(
    stubbed_drive_with_empty_list,
):
    """Default ``include_trashed=False`` must add a ``trashed = false``
    filter; otherwise trashed files would show up in search results."""
    find_doc_by_title(MagicMock(), "x", verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "trashed = false" in q


def test_find_doc_by_title_omits_trashed_filter_when_include_trashed_true(
    stubbed_drive_with_empty_list,
):
    """``include_trashed=True`` must NOT add the trashed filter, so
    operators can recover deleted files via search."""
    find_doc_by_title(
        MagicMock(), "x", include_trashed=True, verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "trashed" not in q


def test_find_doc_by_title_filters_to_docs_and_docx_mime_types(
    stubbed_drive_with_empty_list,
):
    """The query must constrain results to Google Doc OR .docx — never
    other Drive file types. Both MIME types must appear (OR-joined)."""
    find_doc_by_title(MagicMock(), "x", verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert GDOC_MIME in q
    assert DOCX_MIME in q
    # The two are joined by Drive's OR keyword (literal " OR ").
    assert " OR " in q


def test_find_doc_by_title_clamps_page_size_to_drive_max_100(
    stubbed_drive_with_empty_list,
):
    """Drive caps pageSize at 100. The implementation clamps via
    ``min(max(page_size, 1), 100)`` — verify both edges of the clamp."""
    find_doc_by_title(
        MagicMock(), "x", page_size=500, verify_writable=False,
    )
    last_call = stubbed_drive_with_empty_list.files().list.call_args_list[-1]
    assert last_call.kwargs["pageSize"] == 100

    find_doc_by_title(
        MagicMock(), "x", page_size=0, verify_writable=False,
    )
    last_call = stubbed_drive_with_empty_list.files().list.call_args_list[-1]
    assert last_call.kwargs["pageSize"] == 1


def test_find_doc_by_title_returns_empty_matches_on_no_results(
    stubbed_drive_with_empty_list,
):
    """The contract is ``{matches: list, count: int}`` even when zero
    files match. Consumers branch on ``count`` rather than truthiness."""
    result = find_doc_by_title(
        MagicMock(), "nothing matches this", verify_writable=False,
    )
    assert result == {"matches": [], "count": 0}


# ---------------------------------------------------------------------
# CQRS invariant (R33 audit Gap #3 — v2.2.1)
#
# The tool wrapping this function is annotated ``readonly=True``. The
# default behavior MUST be a pure Drive READ. Pre-v2.2.1 the default
# was ``verify_writable=True``, which performed a "batched no-op
# update" probe per match — a Drive WRITE operation that creates an
# audit-log entry on every call. The CQRS violation: a tool annotated
# read-only silently wrote on every default invocation.
#
# These tests pin the v2.2.1 fix: with default args, no probe runs.
# Pass ``verify_writable=True`` explicitly to opt into the probe.
# ---------------------------------------------------------------------


@pytest.fixture
def stubbed_drive_with_one_match():
    """A Drive Resource stub whose files().list().execute() returns a
    single match. We need at least one match to trigger the probe
    branch (the post-list block guards ``if verify_writable and matches``,
    so empty matches doesn't exercise the probe call at all)."""
    drive = MagicMock()
    drive.files().list().execute.return_value = {
        "files": [{
            "id": "FILE_ID_1",
            "name": "match.docx",
            "mimeType": "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document",
            "modifiedTime": "2026-05-26T00:00:00.000Z",
            "trashed": False,
        }],
    }
    # The probe path uses drive.new_batch_http_request() + .add() +
    # .execute(). Stub a batch that records its add() calls so we can
    # assert on whether the probe ran. We never want the probe to
    # actually invoke the callback (the test doesn't care about probe
    # results — only whether the probe was constructed at all).
    batch = MagicMock()
    drive.new_batch_http_request.return_value = batch
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def test_find_doc_by_title_default_does_not_perform_drive_writes(
    stubbed_drive_with_one_match,
):
    """CQRS guard (R33 Gap #3): default invocation (no verify_writable
    kwarg) must NOT run the writability probe. The tool is annotated
    ``readonly=True``; if the default fires the probe, every default
    call writes to the Drive audit log even though no state changes.

    Pre-v2.2.1 default was ``verify_writable=True``. v2.2.1 flips
    that to False so default == read-only behavior matches the
    ``readonly=True`` annotation.
    """
    result = find_doc_by_title(MagicMock(), "match")

    # The list() call is the pure-read step; it MUST have happened.
    assert stubbed_drive_with_one_match.files().list.called, (
        "list() not called — query didn't reach Drive at all"
    )

    # The probe path goes through drive.new_batch_http_request() +
    # batch.add() + batch.execute(). NONE of those may fire under the
    # default invocation. Pre-v2.2.1 ALL THREE would fire.
    assert not stubbed_drive_with_one_match.new_batch_http_request.called, (
        "new_batch_http_request() called under default args — the "
        "writability probe is running on a tool annotated readonly=True. "
        "This is the R33 audit Gap #3 CQRS violation. v2.2.1 changed "
        "verify_writable's default to False to fix this; if you're "
        "seeing this failure, the default got flipped back."
    )
    # The matched file must still come back from the list() — the
    # data path is unchanged; only the probe is suppressed.
    assert result["count"] == 1
    assert result["matches"][0]["file_id"] == "FILE_ID_1"
    # And owned_by_app is None (unknown) because no probe ran. The
    # caller can either re-call with verify_writable=True (opt-in
    # write) OR attempt the write directly and branch on the
    # structured ``app_not_authorized`` soft-failure response.
    assert result["matches"][0]["owned_by_app"] is None, (
        f"owned_by_app should be None when verify_writable defaults "
        f"to False; got {result['matches'][0]['owned_by_app']!r}. "
        f"If you're seeing this, the probe ran (the only code path "
        f"that sets owned_by_app to True/False)."
    )


def test_find_doc_by_title_explicit_verify_writable_runs_probe(
    stubbed_drive_with_one_match,
):
    """Verify the opt-in path still works post-CQRS-fix. With
    ``verify_writable=True``, the probe MUST run — that's the whole
    point of the kwarg. Callers who explicitly opt in still get the
    pre-v2.2.1 owned_by_app behavior.
    """
    # The probe's batched HTTP request needs a callback registered
    # via .add(callback=...). Stub the batch to invoke each callback
    # with no exception so the probe records ``True`` for the file.
    batch = stubbed_drive_with_one_match.new_batch_http_request.return_value
    add_calls: list = []
    def fake_add(request, callback):
        add_calls.append((request, callback))
        # Simulate the request succeeding. The production callback's
        # positional signature is ``(request_id, response, exception)``
        # — matching googleapiclient.http.BatchHttpRequest's calling
        # convention. Pass positionally to mirror it.
        callback("req-1", MagicMock(), None)
    batch.add.side_effect = fake_add

    result = find_doc_by_title(
        MagicMock(), "match", verify_writable=True,
    )

    # The probe path MUST have fired.
    assert stubbed_drive_with_one_match.new_batch_http_request.called, (
        "new_batch_http_request() not called even though "
        "verify_writable=True was passed — the opt-in probe path is "
        "broken."
    )
    assert batch.add.called, "batch.add() not called — probe not built"
    assert batch.execute.called, "batch.execute() not called — probe not run"
    # And the probe's success path populates owned_by_app=True.
    assert result["matches"][0]["owned_by_app"] is True


def test_find_doc_by_title_no_matches_skips_probe_regardless_of_verify_writable():
    """Defensive: when there are zero matches, there's nothing to probe.
    Both verify_writable=True AND verify_writable=False must skip the
    probe path entirely (the guard is ``if verify_writable and matches``).
    Pre-v2.2.1 this already held; pinning it so it doesn't regress."""
    drive = MagicMock()
    drive.files().list().execute.return_value = {"files": []}
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        for verify in (True, False):
            drive.new_batch_http_request.reset_mock()
            find_doc_by_title(MagicMock(), "no-match", verify_writable=verify)
            assert not drive.new_batch_http_request.called, (
                f"new_batch_http_request() called with verify_writable={verify} "
                f"and zero matches — probe should be guarded by "
                f"``and matches`` clause."
            )
