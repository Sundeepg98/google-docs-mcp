"""PR-Δ5 — Optional GCP project linking for Apps Script manifests.

When the operator sets ``GCP_PROJECT_NUMBER``, every Apps Script
project this app provisions includes a ``cloudPlatform.projectId``
manifest block, which routes execution logs into Cloud Logging
under the named GCP project. This is the SOC 2 audit-log path.

When ``GCP_PROJECT_NUMBER`` is unset, the manifest is bit-for-bit
identical to the pre-PR-Δ5 shape — zero behavior change for
personal users.

Tests cover the binary state (env unset vs set) at the helper
function boundary. Integration tests for the broader pipeline
already exist (test_setup_apps_script_for_user.py); this file is
the focused contract test for the new env-var seam.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------
# _build_manifest — the pure-function contract
# ---------------------------------------------------------------------


def test_build_manifest_unset_returns_base_manifest_unchanged():
    """When no GCP project number is supplied (None), the returned
    manifest must NOT carry a ``cloudPlatform`` key. This is the
    backward-compat guarantee: personal users without the env var
    see a manifest identical to v2.3.x."""
    from google_docs_mcp.setup_apps_script import _build_manifest

    manifest = _build_manifest(None)
    assert "cloudPlatform" not in manifest, (
        "Manifest must not include cloudPlatform when no GCP project "
        f"is configured. Got: {manifest!r}"
    )
    # The original keys are all present.
    assert manifest["runtimeVersion"] == "V8"
    assert manifest["exceptionLogging"] == "STACKDRIVER"
    assert manifest["webapp"]["executeAs"] == "USER_DEPLOYING"
    assert manifest["webapp"]["access"] == "ANYONE_ANONYMOUS"


def test_build_manifest_with_project_adds_cloudPlatform_block():
    """When a project number is supplied, the manifest gets a
    ``cloudPlatform.projectId`` block carrying the supplied value.
    Note: the field name is ``projectId`` per Apps Script's documented
    schema, but the value is the project NUMBER (numeric)."""
    from google_docs_mcp.setup_apps_script import _build_manifest

    manifest = _build_manifest("123456789012")
    assert manifest["cloudPlatform"] == {"projectId": "123456789012"}


def test_build_manifest_does_not_mutate_base_manifest():
    """Defensive copy invariant. Two consecutive calls with different
    project numbers must not corrupt each other through shared mutable
    state."""
    from google_docs_mcp.setup_apps_script import _BASE_MANIFEST, _build_manifest

    snapshot_keys = set(_BASE_MANIFEST.keys())
    snapshot_webapp_keys = set(_BASE_MANIFEST["webapp"].keys())

    _ = _build_manifest("111111111111")
    _ = _build_manifest("222222222222")
    _ = _build_manifest(None)

    # The base manifest should be unchanged after all those calls.
    assert set(_BASE_MANIFEST.keys()) == snapshot_keys, (
        f"_BASE_MANIFEST top-level keys mutated. After 3 calls: "
        f"{set(_BASE_MANIFEST.keys())!r} vs original {snapshot_keys!r}"
    )
    assert set(_BASE_MANIFEST["webapp"].keys()) == snapshot_webapp_keys
    assert "cloudPlatform" not in _BASE_MANIFEST


# ---------------------------------------------------------------------
# _resolve_gcp_project_number — env-var read helper
# ---------------------------------------------------------------------


def test_resolve_gcp_project_number_returns_None_when_unset(monkeypatch):
    """Default state: env var unset → None. Personal-user default."""
    monkeypatch.delenv("GCP_PROJECT_NUMBER", raising=False)
    from google_docs_mcp.setup_apps_script import _resolve_gcp_project_number

    assert _resolve_gcp_project_number() is None


@pytest.mark.parametrize("blank", ["", " ", "\t", "  \n  "])
def test_resolve_gcp_project_number_treats_blank_as_unset(monkeypatch, blank):
    """Empty / whitespace-only env var = unset (matches the env-var
    convention used by ``MCP_LICENSE_KEY`` and elsewhere in this repo).
    Avoids "blank project number sent to Apps Script" failure mode."""
    monkeypatch.setenv("GCP_PROJECT_NUMBER", blank)
    from google_docs_mcp.setup_apps_script import _resolve_gcp_project_number

    assert _resolve_gcp_project_number() is None


def test_resolve_gcp_project_number_strips_surrounding_whitespace(monkeypatch):
    """Operators occasionally paste env-var values with stray whitespace;
    the helper strips so Apps Script doesn't see the noise."""
    monkeypatch.setenv("GCP_PROJECT_NUMBER", "  987654321098  \n")
    from google_docs_mcp.setup_apps_script import _resolve_gcp_project_number

    assert _resolve_gcp_project_number() == "987654321098"


# ---------------------------------------------------------------------
# _current_manifest — the boundary the pipeline calls
# ---------------------------------------------------------------------


def test_current_manifest_reads_env_at_call_time(monkeypatch):
    """The contract: ``_current_manifest()`` resolves the env var on
    each call (not at module import). Tests can monkeypatch the env
    var per-test without reloading the module."""
    from google_docs_mcp.setup_apps_script import _current_manifest

    # Unset → no cloudPlatform.
    monkeypatch.delenv("GCP_PROJECT_NUMBER", raising=False)
    assert "cloudPlatform" not in _current_manifest()

    # Set → cloudPlatform present with the env value.
    monkeypatch.setenv("GCP_PROJECT_NUMBER", "111111111111")
    assert _current_manifest()["cloudPlatform"] == {"projectId": "111111111111"}

    # Re-unset → back to no cloudPlatform on the very next call.
    # (Pins the at-call-time semantics: if a snapshot were cached,
    # this would still show the old project number.)
    monkeypatch.delenv("GCP_PROJECT_NUMBER", raising=False)
    assert "cloudPlatform" not in _current_manifest()


def test_current_manifest_change_triggers_content_hash_change():
    """A manifest change MUST yield a different content_hash so the
    setup-state ledger's "manifest changed → re-deploy" reset path
    works for GCP linking flips. Otherwise an operator who flips
    GCP_PROJECT_NUMBER from unset to set wouldn't see a re-deploy
    and the Apps Script project would never get the cloudPlatform
    block — silent no-op."""
    from google_docs_mcp import setup_state
    from google_docs_mcp.setup_apps_script import _build_manifest

    files = {"Code": "function doGet() {}"}
    hash_without = setup_state.compute_content_hash(
        _build_manifest(None), files,
    )
    hash_with_project = setup_state.compute_content_hash(
        _build_manifest("555555555555"), files,
    )
    hash_with_different_project = setup_state.compute_content_hash(
        _build_manifest("666666666666"), files,
    )

    assert hash_without != hash_with_project, (
        "Adding cloudPlatform must change the content_hash so the "
        "setup-state ledger triggers a re-deploy."
    )
    assert hash_with_project != hash_with_different_project, (
        "Changing the GCP project number must change the content_hash "
        "so an operator who migrates to a different GCP project sees "
        "the manifest update."
    )
