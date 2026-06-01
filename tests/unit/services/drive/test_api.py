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
in M3 Phase B to import from ``appscriptly.services.drive.api``.
Not duplicated here.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.drive.api import (
    DOCX_MIME,
    GDOC_MIME,
    GSHEET_MIME,
    GSLIDES_MIME,
    MAX_UPLOAD_BYTES,
    _escape_q_literal,
    create_folder,
    export_doc,
    find_doc_by_title,
    find_file,
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


# ---------------------------------------------------------------------
# create_folder — files.create with folder mimeType
#
# A folder is a Drive file whose mimeType is the folder type. The
# function is NOT idempotent (no execute_with_retry), so it's called
# exactly once — stub files().create() and assert on the body it built
# plus the flat response envelope. Mirrors the q= DSL capture pattern.
# ---------------------------------------------------------------------


_FOLDER_MIME = "application/vnd.google-apps.folder"


@pytest.fixture
def stub_drive_for_create_folder():
    """A Drive Resource stub whose files().create().execute() returns a
    plausible folder response. Enough to let create_folder complete and
    let us inspect the body / fields it passed."""
    drive = MagicMock(name="drive-v3-stub-create-folder")
    drive.files().create().execute.return_value = {
        "id": "FOLDER-NEW",
        "name": "Q3 Onboarding",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def _last_create_kwargs(drive: MagicMock) -> dict:
    """The kwargs of the most recent files().create(...) call that
    actually carried a ``body`` (filters out the bare ``()`` chain-build
    lookups MagicMock records). Mirrors ``_last_q_passed_to_list``."""
    for call in reversed(drive.files().create.call_args_list):
        if "body" in call.kwargs:
            return call.kwargs
    raise AssertionError("no files().create() call captured a body= kwarg")


def test_create_folder_rejects_blank_name():
    """Empty / whitespace name is a caller bug (Drive would name it
    'Untitled folder'). Rejected client-side BEFORE the Drive round-trip
    — no get_service call needed, so MagicMock creds suffice."""
    with pytest.raises(ValueError, match="name cannot be empty"):
        create_folder(MagicMock(), "   ")
    with pytest.raises(ValueError, match="name cannot be empty"):
        create_folder(MagicMock(), "")


def test_create_folder_builds_folder_mimetype_body(stub_drive_for_create_folder):
    """The request body must carry the documented folder mimeType — a
    Drive folder IS a file with this exact mimeType. A stray edit would
    create a regular (empty) file instead of a folder."""
    create_folder(MagicMock(), "Q3 Onboarding")
    kw = _last_create_kwargs(stub_drive_for_create_folder)
    assert kw["body"]["mimeType"] == _FOLDER_MIME
    assert kw["body"]["name"] == "Q3 Onboarding"


def test_create_folder_strips_whitespace_from_name(stub_drive_for_create_folder):
    """Leading / trailing whitespace on the name is stripped before the
    Drive call (consistent with grant_permission's email handling)."""
    create_folder(MagicMock(), "  Padded Name  ")
    kw = _last_create_kwargs(stub_drive_for_create_folder)
    assert kw["body"]["name"] == "Padded Name"


def test_create_folder_omits_parents_when_no_parent_given(
    stub_drive_for_create_folder,
):
    """No parent_folder_id → the body MUST NOT carry a ``parents`` key
    (Drive lands the folder in root). Passing ``parents: [None]`` or an
    empty list would be a 400."""
    create_folder(MagicMock(), "Root Folder")
    kw = _last_create_kwargs(stub_drive_for_create_folder)
    assert "parents" not in kw["body"]


def test_create_folder_nests_under_parent_when_given(
    stub_drive_for_create_folder,
):
    """A parent_folder_id → ``parents: [parent_id]`` so the folder is
    created INSIDE the parent."""
    create_folder(MagicMock(), "Child", parent_folder_id="PARENT-123")
    kw = _last_create_kwargs(stub_drive_for_create_folder)
    assert kw["body"]["parents"] == ["PARENT-123"]


def test_create_folder_returns_flat_envelope_with_url(
    stub_drive_for_create_folder,
):
    """The returned dict is the flat ``{folder_id, name, url,
    parent_folder_id}`` envelope. ``folder_id`` maps Drive's ``id``
    (so the agent can pipe it into move_to_folder); ``url`` deep-links
    to the Drive folder UI; ``parent_folder_id`` echoes the parent back
    (None when created in root)."""
    stub_drive_for_create_folder.files().create().execute.return_value = {
        "id": "FOLDER-ABC",
        "name": "Reports",
    }
    result = create_folder(MagicMock(), "Reports")
    assert result == {
        "folder_id": "FOLDER-ABC",
        "name": "Reports",
        "url": "https://drive.google.com/drive/folders/FOLDER-ABC",
        "parent_folder_id": None,
    }


def test_create_folder_echoes_parent_folder_id_in_envelope(
    stub_drive_for_create_folder,
):
    """When created under a parent, the envelope echoes that parent back
    so the caller can confirm the nesting landed."""
    stub_drive_for_create_folder.files().create().execute.return_value = {
        "id": "F-CHILD",
        "name": "Sub",
    }
    result = create_folder(MagicMock(), "Sub", parent_folder_id="P-1")
    assert result["parent_folder_id"] == "P-1"
    assert result["folder_id"] == "F-CHILD"


# ---------------------------------------------------------------------
# export_doc — files.export (Google-native → portable format)
#
# Pattern: stub files().get (source metadata), files().export_media
# (the export request), and files().create (the re-upload of exported
# bytes). MediaIoBaseDownload / MediaIoBaseUpload are patched to no-op
# fakes so no real streaming runs — we only assert on the call shapes
# (export_media mimeType, create body) and the response envelope, plus
# the pre-API validation + soft-failure branches.
# ---------------------------------------------------------------------


def _mock_http_error(status_code: int, reason_code: str = ""):
    """Build a fake HttpError with the structure googleapiclient produces.

    Mirror of the helper in test_sharing.py / test_soft_failure_contracts.py.
    error_details is populated directly — that's what export_doc inspects
    to classify appNotAuthorizedToFile.
    """
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status_code
    resp.reason = "Forbidden" if status_code == 403 else "Not Found"
    content = (
        f'{{"error":{{"code":{status_code},"errors":'
        f'[{{"reason":"{reason_code}","message":"mocked"}}]}}}}'
    ).encode("utf-8")
    err = HttpError(resp, content)
    err.error_details = [{"reason": reason_code, "message": "mocked"}]
    return err


@pytest.fixture
def _patch_media(monkeypatch):
    """Patch MediaIoBaseDownload + MediaIoBaseUpload in the api module so
    export_doc's stream-download loop + re-upload don't touch real HTTP.

    The fake downloader's next_chunk() returns (None, True) immediately
    (done, zero chunks) — export_doc only needs the loop to terminate;
    the actual bytes are irrelevant since the upload media is also faked.
    """
    import appscriptly.services.drive.api as api_mod

    class _FakeDownloader:
        def __init__(self, _buf, _request):
            pass

        def next_chunk(self):
            return (None, True)

    monkeypatch.setattr(api_mod, "MediaIoBaseDownload", _FakeDownloader)
    monkeypatch.setattr(
        api_mod, "MediaIoBaseUpload", lambda *a, **k: MagicMock(name="media-upload")
    )


@pytest.fixture
def stub_drive_for_export(_patch_media):
    """A Drive stub wired for the export happy path: source is a Google
    Doc; export_media returns a request handle; create returns the new
    exported file with links."""
    drive = MagicMock(name="drive-v3-stub-export")
    drive.files().get().execute.return_value = {
        "id": "SRC-1", "name": "Quarterly Plan", "mimeType": GDOC_MIME,
    }
    drive.files().export_media.return_value = MagicMock(name="export-request")
    drive.files().create().execute.return_value = {
        "id": "EXP-1",
        "name": "Quarterly Plan.pdf",
        "size": "20480",
        "webViewLink": "https://drive.google.com/file/d/EXP-1/view",
        "webContentLink": "https://drive.google.com/uc?id=EXP-1&export=download",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


def test_export_doc_rejects_unknown_format():
    """An unrecognized format token is rejected client-side BEFORE any
    Drive call — no get_service needed, so MagicMock creds suffice."""
    with pytest.raises(ValueError, match="is not recognized"):
        export_doc(MagicMock(), "SRC-1", "garblesnort")


def test_export_doc_rejects_format_invalid_for_source(stub_drive_for_export):
    """A recognized token that's wrong for the source TYPE (xlsx on a
    Doc) is rejected after the metadata read, with a message naming the
    valid formats for that type."""
    # Source is a Google Doc (from the fixture); xlsx is a Sheet format.
    with pytest.raises(ValueError, match="Cannot export a Google Doc as 'xlsx'"):
        export_doc(MagicMock(), "SRC-1", "xlsx")


def test_export_doc_passes_target_mime_to_export_media(stub_drive_for_export):
    """The export_media call must carry the export MIME type mapped from
    the friendly token (pdf → application/pdf) and the source file id."""
    export_doc(MagicMock(), "SRC-1", "pdf")
    kw = stub_drive_for_export.files().export_media.call_args.kwargs
    assert kw["fileId"] == "SRC-1"
    assert kw["mimeType"] == "application/pdf"


def test_export_doc_uploads_result_with_target_mime(stub_drive_for_export):
    """The exported bytes are re-uploaded via files.create as a NEW file
    whose mimeType is the portable target (NOT a Google-native doc), and
    whose name carries the format extension."""
    export_doc(MagicMock(), "SRC-1", "pdf")
    # Find the create() call carrying a body (filter chain-build lookups).
    create_kw = None
    for call in reversed(stub_drive_for_export.files().create.call_args_list):
        if "body" in call.kwargs:
            create_kw = call.kwargs
            break
    assert create_kw is not None, "no files().create() captured a body="
    assert create_kw["body"]["mimeType"] == "application/pdf"
    assert create_kw["body"]["name"] == "Quarterly Plan.pdf"


def test_export_doc_case_insensitive_format_token(stub_drive_for_export):
    """Format tokens are normalized to lowercase — 'PDF' works like 'pdf'."""
    result = export_doc(MagicMock(), "SRC-1", "PDF")
    assert result["export_format"] == "pdf"
    assert result["export_mime_type"] == "application/pdf"


def test_export_doc_returns_flat_envelope_with_links(stub_drive_for_export):
    """The success envelope surfaces the source + export identity plus
    the new file's id / view url / direct-download url / size."""
    result = export_doc(MagicMock(), "SRC-1", "pdf")
    assert result == {
        "source_file_id": "SRC-1",
        "source_mime_type": GDOC_MIME,
        "export_format": "pdf",
        "export_mime_type": "application/pdf",
        "exported_file_id": "EXP-1",
        "name": "Quarterly Plan.pdf",
        "url": "https://drive.google.com/file/d/EXP-1/view",
        "download_url": "https://drive.google.com/uc?id=EXP-1&export=download",
        "size_bytes": 20480,
    }


def test_export_doc_default_name_appends_extension(stub_drive_for_export):
    """With no output_name, the exported file is named from the source's
    name + the format extension (Quarterly Plan → Quarterly Plan.pdf)."""
    export_doc(MagicMock(), "SRC-1", "pdf")
    create_kw = next(
        c.kwargs for c in reversed(stub_drive_for_export.files().create.call_args_list)
        if "body" in c.kwargs
    )
    assert create_kw["body"]["name"] == "Quarterly Plan.pdf"


def test_export_doc_explicit_output_name_gets_extension_if_missing(
    stub_drive_for_export,
):
    """A caller output_name without the extension gets it appended."""
    export_doc(MagicMock(), "SRC-1", "pdf", output_name="Final Report")
    create_kw = next(
        c.kwargs for c in reversed(stub_drive_for_export.files().create.call_args_list)
        if "body" in c.kwargs
    )
    assert create_kw["body"]["name"] == "Final Report.pdf"


def test_export_doc_explicit_output_name_keeps_existing_extension(
    stub_drive_for_export,
):
    """A caller output_name that ALREADY ends in the extension isn't
    doubled (Final.pdf stays Final.pdf, not Final.pdf.pdf)."""
    export_doc(MagicMock(), "SRC-1", "pdf", output_name="Final.pdf")
    create_kw = next(
        c.kwargs for c in reversed(stub_drive_for_export.files().create.call_args_list)
        if "body" in c.kwargs
    )
    assert create_kw["body"]["name"] == "Final.pdf"


def test_export_doc_sheet_to_xlsx_happy_path(stub_drive_for_export):
    """A Google Sheet exports to xlsx (a valid Sheet format) — verifies
    the per-source-type allowlist admits the right pairings, not just
    Docs."""
    stub_drive_for_export.files().get().execute.return_value = {
        "id": "SHEET-1", "name": "Budget", "mimeType": GSHEET_MIME,
    }
    stub_drive_for_export.files().create().execute.return_value = {
        "id": "EXP-XLSX", "name": "Budget.xlsx",
        "webViewLink": "https://drive.google.com/file/d/EXP-XLSX/view",
        "webContentLink": "https://drive.google.com/uc?id=EXP-XLSX",
    }
    result = export_doc(MagicMock(), "SHEET-1", "xlsx")
    assert result["export_format"] == "xlsx"
    kw = stub_drive_for_export.files().export_media.call_args.kwargs
    assert kw["mimeType"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_export_doc_size_bytes_none_when_drive_omits_size(stub_drive_for_export):
    """size_bytes is None (not a crash) when Drive's create response
    omits the size field."""
    stub_drive_for_export.files().create().execute.return_value = {
        "id": "EXP-NOSIZE", "name": "Quarterly Plan.pdf",
        "webViewLink": "https://drive.google.com/file/d/EXP-NOSIZE/view",
    }
    result = export_doc(MagicMock(), "SRC-1", "pdf")
    assert result["size_bytes"] is None
    # download_url also None when webContentLink is absent.
    assert result["download_url"] is None


# --- export_doc soft-failure branches (returned as data, not raised) ---


def test_export_doc_not_found_returns_soft_failure(_patch_media):
    """404 on the source metadata read → reason: not_found (data, not raised)."""
    drive = MagicMock(name="drive-export-404")
    drive.files().get().execute.side_effect = _mock_http_error(404)
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = export_doc(MagicMock(), "GONE", "pdf")
    assert result["reason"] == "not_found"
    assert result["source_file_id"] == "GONE"


def test_export_doc_app_not_authorized_returns_soft_failure(_patch_media):
    """403 appNotAuthorizedToFile on the source (file not app-accessible
    under drive.file) → reason: app_not_authorized (data, not raised)."""
    drive = MagicMock(name="drive-export-403")
    drive.files().get().execute.side_effect = _mock_http_error(
        403, "appNotAuthorizedToFile",
    )
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = export_doc(MagicMock(), "FOREIGN", "pdf")
    assert result["reason"] == "app_not_authorized"
    assert result["source_file_id"] == "FOREIGN"


def test_export_doc_not_exportable_for_binary_source(_patch_media):
    """A binary blob (e.g. an existing PDF) is NOT a Google-native editor
    file → reason: not_exportable, returned as data (no export attempted)."""
    drive = MagicMock(name="drive-export-binary")
    drive.files().get().execute.return_value = {
        "id": "BLOB-1", "name": "scan.pdf", "mimeType": "application/pdf",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = export_doc(MagicMock(), "BLOB-1", "pdf")
    assert result["reason"] == "not_exportable"
    # export_media must NOT have been called — we bailed before the export.
    drive.files().export_media.assert_not_called()


def test_export_doc_reraises_unclassified_http_error(_patch_media):
    """A 500 on the source read is not classified above → propagates as
    HttpError so genuine bugs surface."""
    from googleapiclient.errors import HttpError

    drive = MagicMock(name="drive-export-500")
    drive.files().get().execute.side_effect = _mock_http_error(500)
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(HttpError):
            export_doc(MagicMock(), "SRC-1", "pdf")


# ---------------------------------------------------------------------
# _escape_q_literal — the shared q= string-literal escaper
# ---------------------------------------------------------------------


def test_escape_q_literal_escapes_single_quote():
    """A single quote is backslash-escaped (Drive's documented form)."""
    assert _escape_q_literal("Bob's Doc") == "Bob\\'s Doc"


def test_escape_q_literal_escapes_backslash_first():
    """Backslash is escaped BEFORE the quote so we don't double-escape
    the backslashes we add for quotes. A literal ``\\`` becomes ``\\\\``."""
    # Input: one backslash. Output: two backslashes.
    assert _escape_q_literal("a\\b") == "a\\\\b"
    # Combined: backslash + quote → escaped backslash + escaped quote.
    assert _escape_q_literal("a\\'b") == "a\\\\\\'b"


def test_escape_q_literal_leaves_plain_text_untouched():
    assert _escape_q_literal("plain text 123") == "plain text 123"


# ---------------------------------------------------------------------
# find_file — generalized q= DSL construction (any mimeType, filters)
#
# Reuses the find_doc_by_title test scaffolding (stubbed_drive_with_empty_list
# + _last_q_passed_to_list) since find_file lands on the same
# drive.files().list() call. We assert on the q-string each filter
# combination builds.
# ---------------------------------------------------------------------


def test_find_file_name_substring_uses_contains(stubbed_drive_with_empty_list):
    """A query (default exact=False) builds a ``name contains`` clause."""
    find_file(MagicMock(), "budget", verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "name contains 'budget'" in q


def test_find_file_name_exact_uses_equals(stubbed_drive_with_empty_list):
    find_file(MagicMock(), "Budget 2026", exact=True, verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "name = 'Budget 2026'" in q


def test_find_file_does_NOT_hardcode_docs_mimetype(stubbed_drive_with_empty_list):
    """THE GENERALIZATION: unlike find_doc_by_title, find_file must NOT
    inject the Google-Doc / .docx mimeType filter. Without a mime_type
    arg, the q has no mimeType clause at all — so Sheets/Slides/PDF are
    in scope."""
    find_file(MagicMock(), "report", verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "mimeType" not in q


def test_find_file_mime_type_filter_added_when_given(
    stubbed_drive_with_empty_list,
):
    """An explicit mime_type adds an exact ``mimeType =`` clause — e.g.
    constraining to Sheets."""
    find_file(
        MagicMock(), mime_type=GSHEET_MIME, verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert f"mimeType = '{GSHEET_MIME}'" in q


def test_find_file_full_text_filter_added_when_given(
    stubbed_drive_with_empty_list,
):
    """full_text builds a ``fullText contains`` clause (content search)."""
    find_file(MagicMock(), full_text="Q3 revenue", verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "fullText contains 'Q3 revenue'" in q


def test_find_file_parent_folder_filter_uses_in_parents(
    stubbed_drive_with_empty_list,
):
    """parent_folder_id builds the documented ``'<id>' in parents`` form."""
    find_file(
        MagicMock(), parent_folder_id="FOLDER-99", verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "'FOLDER-99' in parents" in q


def test_find_file_combines_all_filters_with_and(stubbed_drive_with_empty_list):
    """All filters together are AND-joined into one q."""
    find_file(
        MagicMock(),
        "plan",
        mime_type=GSLIDES_MIME,
        full_text="roadmap",
        parent_folder_id="F1",
        verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "name contains 'plan'" in q
    assert f"mimeType = '{GSLIDES_MIME}'" in q
    assert "fullText contains 'roadmap'" in q
    assert "'F1' in parents" in q
    assert " and " in q


def test_find_file_escapes_single_quotes_in_all_string_filters(
    stubbed_drive_with_empty_list,
):
    """SECURITY: a single quote in ANY string filter must be escaped so
    it can't break out of — or inject into — the q DSL. Checks query,
    mime_type, full_text, parent_folder_id all run through the escaper."""
    find_file(
        MagicMock(),
        "O'Brien",
        mime_type="x'y",
        full_text="it's here",
        parent_folder_id="fold'er",
        verify_writable=False,
    )
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "O\\'Brien" in q
    assert "x\\'y" in q
    assert "it\\'s here" in q
    assert "fold\\'er" in q
    # And the literals stay balanced (even count of unescaped quotes).
    unescaped = q.replace("\\'", "")
    assert unescaped.count("'") % 2 == 0, f"q literals unbalanced: {q!r}"


def test_find_file_excludes_trashed_by_default(stubbed_drive_with_empty_list):
    find_file(MagicMock(), "x", verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert "trashed = false" in q


def test_find_file_empty_call_browses_recent_with_only_trashed_filter(
    stubbed_drive_with_empty_list,
):
    """No filters at all (no query/mime/fullText/parent) is a valid
    "recent app-accessible files" browse — q is just the trashed filter,
    NOT a crash or a forced name match."""
    find_file(MagicMock(), verify_writable=False)
    q = _last_q_passed_to_list(stubbed_drive_with_empty_list)
    assert q == "trashed = false"


def test_find_file_empty_call_with_include_trashed_sends_q_none(
    stubbed_drive_with_empty_list,
):
    """No filters AND include_trashed=True → no clauses at all → q is
    None (Drive treats that as "all files"), the intended browse-all."""
    find_file(MagicMock(), include_trashed=True, verify_writable=False)
    last_call = stubbed_drive_with_empty_list.files().list.call_args_list[-1]
    assert last_call.kwargs["q"] is None


def test_find_file_clamps_page_size_to_100(stubbed_drive_with_empty_list):
    find_file(MagicMock(), "x", page_size=500, verify_writable=False)
    last_call = stubbed_drive_with_empty_list.files().list.call_args_list[-1]
    assert last_call.kwargs["pageSize"] == 100


def test_find_file_returns_matches_with_mimetype_surfaced(
    stubbed_drive_with_empty_list,
):
    """The return shape mirrors find_doc_by_title: matches[] with
    file_id/name/mimeType/modified_time/trashed/owned_by_app + count.
    Here a Sheet comes back — proving non-Doc types flow through."""
    stubbed_drive_with_empty_list.files().list().execute.return_value = {
        "files": [{
            "id": "SHEET-1", "name": "Budget",
            "mimeType": GSHEET_MIME,
            "modifiedTime": "2026-05-30T00:00:00.000Z",
            "trashed": False,
        }],
    }
    result = find_file(MagicMock(), mime_type=GSHEET_MIME, verify_writable=False)
    assert result["count"] == 1
    assert result["matches"][0]["file_id"] == "SHEET-1"
    assert result["matches"][0]["mimeType"] == GSHEET_MIME
    assert result["matches"][0]["owned_by_app"] is None


def test_find_file_default_does_not_probe_writability():
    """CQRS: default (verify_writable=False) must NOT run the no-op write
    probe — the tool is readonly=True. Mirror of the find_doc_by_title
    CQRS guard."""
    drive = MagicMock()
    drive.files().list().execute.return_value = {
        "files": [{
            "id": "F1", "name": "n", "mimeType": GSHEET_MIME,
            "modifiedTime": "2026-05-30T00:00:00.000Z", "trashed": False,
        }],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = find_file(MagicMock(), mime_type=GSHEET_MIME)
    assert not drive.new_batch_http_request.called, (
        "find_file ran the writability probe under default args — it is "
        "readonly=True, so the default must be a pure read (CQRS)."
    )
    assert result["matches"][0]["owned_by_app"] is None


def test_find_file_explicit_verify_writable_runs_probe():
    """Opt-in verify_writable=True runs the batched no-op probe and
    populates owned_by_app (same mechanism as find_doc_by_title)."""
    drive = MagicMock()
    drive.files().list().execute.return_value = {
        "files": [{
            "id": "F1", "name": "n", "mimeType": GSHEET_MIME,
            "modifiedTime": "2026-05-30T00:00:00.000Z", "trashed": False,
        }],
    }
    batch = MagicMock()
    drive.new_batch_http_request.return_value = batch

    def fake_add(request, callback):
        callback("req-1", MagicMock(), None)  # simulate success
    batch.add.side_effect = fake_add

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = find_file(
            MagicMock(), mime_type=GSHEET_MIME, verify_writable=True,
        )
    assert drive.new_batch_http_request.called
    assert batch.execute.called
    assert result["matches"][0]["owned_by_app"] is True


def test_find_file_returns_empty_on_no_results(stubbed_drive_with_empty_list):
    """Contract holds with zero matches — {matches: [], count: 0}."""
    result = find_file(MagicMock(), "nothing-here", verify_writable=False)
    assert result == {"matches": [], "count": 0}
