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
