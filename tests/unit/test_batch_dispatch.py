"""Batch dispatch tests for the _run_batch helper.

Verifies trash/untrash batch mode produces correct per-item results
and accurate summary counts — and that one failure never aborts the rest.

**v2.1.4 (M3 Phase B)**: ``_run_batch`` moved from ``server.py`` to
``services/drive/tools.py`` alongside the trash/untrash tools that
use it (the helper is drive-specific). Import paths + patch target
updated accordingly.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_creds_loader():
    # v2.1.4: patch the module-level binding in services/drive/tools.py
    # (NOT the original server.py source) — _run_batch captures
    # _get_credentials at module load via the _get_server_helpers shim.
    # Patching server.py's binding wouldn't affect drive/tools.py's
    # already-bound reference.
    with patch("appscriptly.services.drive.tools._get_credentials") as m:
        m.return_value = MagicMock()
        yield m


def test_batch_summary_partitions_results(mock_creds_loader):
    """succeeded + skipped + failed must equal len(results)."""
    from appscriptly.services.drive.tools import _run_batch

    def fake_fn(_creds, fid):
        if fid == "OK":
            return {"file_id": fid, "name": "x", "trashed": True}
        if fid == "SOFT":
            return {"file_id": fid, "trashed": False, "reason": "not_found"}
        raise RuntimeError("boom")

    result = _run_batch(["OK", "SOFT", "BOOM"], fake_fn, "trashed")
    assert len(result["results"]) == 3
    s = result["summary"]
    assert s["succeeded"] == 1
    assert s["skipped"] == 1
    assert s["failed"] == 1
    assert s["succeeded"] + s["skipped"] + s["failed"] == 3


def test_batch_one_failure_does_not_abort_rest(mock_creds_loader):
    """A bad item in the middle does not stop subsequent items."""
    from appscriptly.services.drive.tools import _run_batch

    seen: list[str] = []

    def fake_fn(_creds, fid):
        seen.append(fid)
        if fid == "BAD":
            raise RuntimeError("explode")
        return {"file_id": fid, "trashed": True}

    result = _run_batch(["A", "BAD", "C"], fake_fn, "trashed")
    assert seen == ["A", "BAD", "C"], "later items skipped after a failure"
    assert result["summary"]["succeeded"] == 2
    assert result["summary"]["failed"] == 1


def test_batch_empty_list(mock_creds_loader):
    """Edge case: empty input → empty results and zero counts."""
    from appscriptly.services.drive.tools import _run_batch

    result = _run_batch([], lambda c, x: {}, "trashed")
    assert result["results"] == []
    assert result["summary"] == {"succeeded": 0, "skipped": 0, "failed": 0}


# ---------------------------------------------------------------------
# Wave-5 S4 generalization: success_key=None (move/share have no boolean
# success flag) and item_key (share batches over emails, not file ids).
# ---------------------------------------------------------------------


def test_batch_success_key_none_counts_non_reason_result_as_succeeded(
    mock_creds_loader,
):
    """The move/share path passes success_key=None: any result that
    carries no ``reason`` (and did not raise) is a success; a ``reason``
    result is a skip. Move/share return no boolean success flag, so
    absence of a reason IS the success signal."""
    from appscriptly.services.drive.tools import _run_batch

    def fake_fn(_creds, item):
        if item == "SOFT":
            return {"file_id": item, "reason": "not_found"}
        # A success dict with NO boolean success flag (like move/share).
        return {"file_id": item, "name": "n", "parents": ["D"]}

    result = _run_batch(["OK", "SOFT"], fake_fn)  # success_key defaults None
    assert result["summary"] == {"succeeded": 1, "skipped": 1, "failed": 0}


def test_batch_item_key_labels_the_error_result(mock_creds_loader):
    """``item_key`` names the item in the per-item error dict. Share
    batches over emails, so it passes item_key='email' and a failed
    recipient is labeled honestly (not as a file_id)."""
    from appscriptly.services.drive.tools import _run_batch

    def boom(_creds, _item):
        raise RuntimeError("nope")

    result = _run_batch(["a@e.com"], boom, item_key="email")
    item = result["results"][0]
    assert item["email"] == "a@e.com"
    assert "file_id" not in item
    assert item["reason"] == "unexpected_error"
    assert result["summary"] == {"succeeded": 0, "skipped": 0, "failed": 1}
