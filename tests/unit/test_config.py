"""Unit tests for ``appscriptly.config`` — the ~/.google-docs-mcp/config.json
operator-scoped settings store (audit Gap: config.py was untested).

This module persists single-tenant operator settings (the deployed Apps
Script Web App URL + script/deployment IDs) to a small JSON file next to
``token.json``. Unlike ``user_store`` / ``storage_backend`` (50+ tests),
``config.py`` had ZERO coverage despite touching disk — a config
corruption would silently break the CLI's webapp-URL lookup. This file
closes that gap:

  1. ``config_path``    — env-override (``GOOGLE_DOCS_DATA_DIR``) vs the
                          ``~/.google-docs-mcp`` default; filename pinned.
  2. ``load``           — missing file -> {}; malformed JSON -> {} (graceful,
                          not a crash); valid JSON round-trips.
  3. ``save``           — creates the parent dir; MERGES into existing
                          (partial config preserved); returns the merged
                          dict; survives a load() round-trip.
  4. ``get_webapp_url`` — reads the one key; None when absent.

Isolation: the autouse ``isolated_db`` fixture (tests/conftest.py) points
``GOOGLE_DOCS_DATA_DIR`` at a per-test ``tmp_path``, so every test here
reads/writes an isolated config.json and never touches the real
``~/.google-docs-mcp/``. Tests take ``tmp_path`` directly to assert the
on-disk location.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from appscriptly import config


# ---------------------------------------------------------------------
# config_path — location resolution
# ---------------------------------------------------------------------


def test_config_path_honors_data_dir_env_override(tmp_path, monkeypatch):
    """With ``GOOGLE_DOCS_DATA_DIR`` set, config.json lives under it.
    (The autouse fixture already points it at tmp_path; assert that.)"""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    assert config.config_path() == tmp_path / "config.json"


def test_config_path_defaults_to_home_data_dir_when_no_override(monkeypatch):
    """Without the env override, the path is ``~/.google-docs-mcp/config.json``
    — co-located with token.json per the module docstring."""
    monkeypatch.delenv("GOOGLE_DOCS_DATA_DIR", raising=False)
    fake_home = Path("/tmp/fake-home-xyz")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert config.config_path() == fake_home / ".google-docs-mcp" / "config.json"


def test_config_path_filename_is_config_json(tmp_path, monkeypatch):
    """The filename is pinned to ``config.json`` (distinct from token.json
    in the same dir) — a rename would silently orphan existing configs."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    assert config.config_path().name == "config.json"


# ---------------------------------------------------------------------
# load — missing / malformed / valid
# ---------------------------------------------------------------------


def test_load_returns_empty_dict_when_file_missing(tmp_path, monkeypatch):
    """No config.json yet (fresh install) -> {} rather than raising. The
    autouse fixture's tmp_path starts empty, so nothing exists here."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    assert not (tmp_path / "config.json").exists()
    assert config.load() == {}


def test_load_returns_empty_dict_on_malformed_json(tmp_path, monkeypatch):
    """A corrupt / truncated config.json must degrade to {} (graceful) so
    a single bad write doesn't hard-crash every subsequent CLI call. This
    is the audit's headline concern: config corruption -> silent recovery,
    not an unhandled JSONDecodeError."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("{ this is not valid json ")
    assert config.load() == {}


def test_load_returns_parsed_dict_for_valid_json(tmp_path, monkeypatch):
    """A well-formed config.json is parsed and returned verbatim."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    payload = {
        "apps_script_webapp_url": "https://script.google.com/macros/s/AKxyz/exec",
        "apps_script_script_id": "SCRIPT-123",
    }
    (tmp_path / "config.json").write_text(json.dumps(payload))
    assert config.load() == payload


def test_load_empty_object_json_is_empty_dict(tmp_path, monkeypatch):
    """An empty-object file (``{}``) is a valid, distinct-from-missing
    state and loads as {}."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    assert config.load() == {}


# ---------------------------------------------------------------------
# save — dir creation, merge, return value, round-trip
# ---------------------------------------------------------------------


def test_save_creates_parent_dir_when_missing(tmp_path, monkeypatch):
    """save() must mkdir the data dir (parents=True) — on a fresh machine
    ``~/.google-docs-mcp`` won't exist yet, and save() is often the first
    thing to write there."""
    nested = tmp_path / "deep" / "data" / "dir"
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(nested))
    assert not nested.exists()
    config.save({"apps_script_webapp_url": "https://example.test/exec"})
    assert (nested / "config.json").exists()


def test_save_writes_then_load_round_trips(tmp_path, monkeypatch):
    """The core contract: what save() writes, load() reads back identically."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    written = config.save({
        "apps_script_webapp_url": "https://script.google.com/s/A/exec",
        "apps_script_deployment_id": "DEPLOY-9",
    })
    assert config.load() == written


def test_save_returns_merged_config(tmp_path, monkeypatch):
    """save() returns the full merged dict (not just the updates), so a
    caller can use the return value without a second load()."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    config.save({"apps_script_script_id": "SCRIPT-1"})
    merged = config.save({"apps_script_deployment_id": "DEPLOY-1"})
    assert merged == {
        "apps_script_script_id": "SCRIPT-1",
        "apps_script_deployment_id": "DEPLOY-1",
    }


def test_save_merges_into_existing_partial_config(tmp_path, monkeypatch):
    """PARTIAL-CONFIG MERGE: saving a new key must PRESERVE existing keys
    (not overwrite the whole file). setup-apps-script-auto writes the
    script_id + deployment_id separately from the webapp_url; a
    non-merging save would clobber one with the other."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    config.save({"apps_script_webapp_url": "https://a.test/exec"})
    config.save({"apps_script_script_id": "SCRIPT-XYZ"})
    final = config.load()
    assert final == {
        "apps_script_webapp_url": "https://a.test/exec",
        "apps_script_script_id": "SCRIPT-XYZ",
    }


def test_save_overwrites_same_key_with_new_value(tmp_path, monkeypatch):
    """Re-saving an existing key updates it (last-write-wins for that key)
    while leaving other keys intact — the re-deploy update-in-place case."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    config.save({
        "apps_script_webapp_url": "https://old.test/exec",
        "apps_script_script_id": "SCRIPT-KEEP",
    })
    config.save({"apps_script_webapp_url": "https://new.test/exec"})
    final = config.load()
    # Assert the whole merged dict (avoids subscripting a total=False
    # TypedDict key, which pyright flags as possibly-absent).
    assert final == {
        "apps_script_webapp_url": "https://new.test/exec",
        "apps_script_script_id": "SCRIPT-KEEP",
    }


def test_save_output_is_human_readable_indented_json(tmp_path, monkeypatch):
    """save() writes indented JSON (indent=2) — the file is operator-
    editable by hand, so pretty-printing is part of the contract."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    config.save({"apps_script_script_id": "SCRIPT-1"})
    raw = (tmp_path / "config.json").read_text()
    # Indented output contains newlines + leading spaces; minified wouldn't.
    assert "\n" in raw
    assert '  "apps_script_script_id"' in raw


# ---------------------------------------------------------------------
# get_webapp_url — the one typed accessor
# ---------------------------------------------------------------------


def test_get_webapp_url_returns_none_when_absent(tmp_path, monkeypatch):
    """No config / no key -> None (callers branch on this to decide
    whether setup is required)."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    assert config.get_webapp_url() is None


def test_get_webapp_url_returns_none_when_other_keys_present(
    tmp_path, monkeypatch,
):
    """A config that has script/deployment IDs but NOT the webapp_url
    still returns None for the URL — keys are independent."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    config.save({"apps_script_script_id": "SCRIPT-1"})
    assert config.get_webapp_url() is None


def test_get_webapp_url_returns_saved_value(tmp_path, monkeypatch):
    """After save(), the accessor returns the stored URL."""
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    url = "https://script.google.com/macros/s/AKfycb/exec"
    config.save({"apps_script_webapp_url": url})
    assert config.get_webapp_url() == url
