"""Atomic save_state tests (v1.3.1).

Guards: a crash mid-write must NOT corrupt the canonical state file.
The original file (if it existed) stays intact; the .tmp file is
cleaned up best-effort.

Previously save_state used p.write_text directly, which truncated then
wrote — a SIGKILL during the truncate window bricked the ledger. This
bug bricked the very recovery the ledger was designed to enable.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from google_docs_mcp.setup_state import SetupState


def test_save_state_writes_atomically(tmp_path):
    """Happy path: write succeeds, canonical file ends up with new contents."""
    from google_docs_mcp.setup_state import save_state, load_state

    state: SetupState = {"script_id": "abc", "content_hash": "deadbeef"}
    save_state(tmp_path, state)

    loaded = load_state(tmp_path)
    assert loaded == state


def test_save_state_overwrites_existing_atomically(tmp_path):
    """Successive saves preserve the new state, not the old."""
    from google_docs_mcp.setup_state import save_state, load_state

    save_state(tmp_path, {"script_id": "first"})
    save_state(tmp_path, {"script_id": "second"})

    loaded = load_state(tmp_path)
    assert loaded == {"script_id": "second"}


def test_save_state_crash_leaves_original_intact(tmp_path):
    """Inject an exception mid-write; original file must survive."""
    from google_docs_mcp.setup_state import save_state, load_state, state_path

    # Seed an existing canonical file.
    save_state(tmp_path, {"script_id": "original-survives"})
    original_contents = state_path(tmp_path).read_text(encoding="utf-8")

    # Patch Path.write_text to raise mid-write, simulating SIGKILL during
    # the tmp file write.
    original_write_text = Path.write_text

    def fail_on_tmp(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("simulated crash during tmpfile write")
        return original_write_text(self, *args, **kwargs)

    with patch.object(Path, "write_text", fail_on_tmp):
        with pytest.raises(OSError, match="simulated crash"):
            save_state(tmp_path, {"script_id": "would-have-replaced"})

    # Original canonical file MUST be unchanged.
    assert state_path(tmp_path).read_text(encoding="utf-8") == original_contents
    loaded = load_state(tmp_path)
    assert loaded == {"script_id": "original-survives"}


def test_save_state_cleans_up_tmpfile_on_failure(tmp_path):
    """The .tmp file should be removed on the failure path."""
    from google_docs_mcp.setup_state import save_state, state_path

    p = state_path(tmp_path)
    tmp = p.with_suffix(p.suffix + ".tmp")

    # Patch os.replace to fail AFTER tmpfile is written. This exercises
    # the cleanup branch where the tmp exists and the replace fails.
    import google_docs_mcp.setup_state as ss_mod

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    with patch.object(ss_mod.os, "replace", fail_replace):
        with pytest.raises(OSError, match="simulated replace"):
            save_state(tmp_path, {"script_id": "x"})

    # The tmp file should have been cleaned up best-effort.
    assert not tmp.exists()


def test_save_state_works_when_no_canonical_exists(tmp_path):
    """First save on an empty dir creates the canonical file cleanly."""
    from google_docs_mcp.setup_state import save_state, load_state, state_path

    assert not state_path(tmp_path).exists()
    save_state(tmp_path, {"script_id": "fresh"})
    assert state_path(tmp_path).exists()
    assert load_state(tmp_path) == {"script_id": "fresh"}
