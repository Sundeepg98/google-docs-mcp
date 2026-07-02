"""Tests for scripts/check_live_drift.py (the prod-drift monitor).

Guards against:
- the AST scope derivation drifting from the RUNTIME truth (the parser
  must return exactly ``appscriptly.auth.WORKSPACE_SCOPES`` and
  ``appscriptly.oauth_google.IDENTITY_SCOPES`` - this is what makes the
  monitor's expected count "computed from source", never hardcoded)
- scope drift NOT failing the check (the whole point of the monitor)
- a /health outage NOT failing the check (billing-suspension class)
- a live-vs-main commit mismatch escalating beyond a warning (deploys
  legitimately race merges by minutes; the hard gate is the scope set)
- exit codes / report file breaking the prod-drift.yml wiring

No network: every test monkeypatches ``fetch_json``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ isn't on the package path; add it explicitly so the test
# can import check_live_drift without being co-located.
# Mirrors tests/unit/test_mutation_check.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import check_live_drift as drift  # noqa: E402  # pyright: ignore[reportMissingImports]


# ---------------------------------------------------------------------
# 1. Source derivation == runtime truth (the never-hardcode contract)
# ---------------------------------------------------------------------


def test_parsed_workspace_scopes_match_runtime_value():
    """The monitor AST-parses WORKSPACE_SCOPES out of auth.py instead of
    importing it (stdlib-only constraint). This pin makes the parse and
    the import inseparable: refactor auth.py in a way the parser can't
    follow and THIS fails in CI before the monitor silently derives a
    wrong expected set."""
    from appscriptly.auth import WORKSPACE_SCOPES

    parsed = drift.extract_scope_list(drift.AUTH_PY, "WORKSPACE_SCOPES")
    assert parsed == WORKSPACE_SCOPES


def test_parsed_identity_scopes_match_runtime_value():
    from appscriptly.oauth_google import IDENTITY_SCOPES

    parsed = drift.extract_scope_list(drift.OAUTH_GOOGLE_PY, "IDENTITY_SCOPES")
    assert parsed == IDENTITY_SCOPES


def test_expected_connector_set_matches_google_api_scopes():
    """The live endpoint serves ``sorted(GOOGLE_API_SCOPES)``; the
    monitor's expected set must be exactly that, derived from source."""
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    identity, workspace = drift.expected_connector_scopes()
    expected = sorted(set(identity) | set(workspace))
    assert expected == sorted(GOOGLE_API_SCOPES)
    # Count identity: expected count comes out of the source lists, not
    # a literal (identity and workspace do not overlap today).
    assert len(expected) == len(identity) + len(workspace)


def test_extract_scope_list_fails_loud_on_missing_variable(tmp_path):
    """A rename of WORKSPACE_SCOPES must crash the monitor (loud),
    never return an empty/partial set (silent wrong answer)."""
    module = tmp_path / "fake.py"
    module.write_text("SOMETHING_ELSE = ['a']\n", encoding="utf-8")
    with pytest.raises(drift.DriftCheckError, match="WORKSPACE_SCOPES"):
        drift.extract_scope_list(module, "WORKSPACE_SCOPES")


def test_extract_scope_list_fails_loud_on_non_list_shape(tmp_path):
    module = tmp_path / "fake.py"
    module.write_text(
        "WORKSPACE_SCOPES = tuple(['a'])\n", encoding="utf-8"
    )
    with pytest.raises(drift.DriftCheckError, match="plain list"):
        drift.extract_scope_list(module, "WORKSPACE_SCOPES")


# ---------------------------------------------------------------------
# 2. Behavioral checks (fetch_json monkeypatched; no network)
# ---------------------------------------------------------------------


def _live_payloads(scopes, *, git_commit="abc1234", healthy=True):
    """Build a fetch_json fake serving ``scopes`` + /health payloads."""

    def fake_fetch(url: str) -> dict:
        if drift.METADATA_PATH in url:
            return {"scopes_supported": list(scopes)}
        if drift.HEALTH_PATH in url:
            if not healthy:
                raise drift.DriftCheckError(f"GET {url} returned HTTP 503")
            return {"ok": True, "service": "appscriptly", "git_commit": git_commit}
        raise AssertionError(f"unexpected URL fetched: {url}")

    return fake_fetch


def _expected_scopes():
    identity, workspace = drift.expected_connector_scopes()
    return sorted(set(identity) | set(workspace))


def test_run_checks_green_when_live_matches_source(monkeypatch):
    monkeypatch.setattr(
        drift, "fetch_json", _live_payloads(_expected_scopes())
    )
    failures, warnings = drift.run_checks(expect_commit=None)
    assert failures == []
    assert warnings == []


def test_run_checks_fails_on_missing_scope(monkeypatch):
    """The core drift signal: live serving FEWER scopes than main's
    source (the 13-vs-17 incident class) must produce a failure that
    names the missing scopes and both counts."""
    expected = _expected_scopes()
    stale = [s for s in expected if not s.endswith("gmail.send")]
    assert len(stale) == len(expected) - 1, "fixture: drop exactly one"
    monkeypatch.setattr(drift, "fetch_json", _live_payloads(stale))
    failures, _warnings = drift.run_checks(expect_commit=None)
    assert len(failures) == 1
    assert "SCOPE DRIFT" in failures[0]
    assert "gmail.send" in failures[0]
    assert f"expected count: {len(expected)}" in failures[0]
    assert f"live count:     {len(stale)}" in failures[0]


def test_run_checks_fails_on_unexpected_live_scope(monkeypatch):
    """Prod serving a scope main does NOT have (out-of-band deploy of a
    wrong ref) is drift too - set comparison, not just count."""
    extra = _expected_scopes() + ["https://www.googleapis.com/auth/fake.extra"]
    monkeypatch.setattr(drift, "fetch_json", _live_payloads(extra))
    failures, _warnings = drift.run_checks(expect_commit=None)
    assert len(failures) == 1
    assert "fake.extra" in failures[0]


def test_run_checks_fails_when_health_is_down(monkeypatch):
    """An unreachable /health (billing suspension took the app down) is
    an alert condition on BOTH hostnames - two findings, plus none for
    the scope surface which still matched."""
    monkeypatch.setattr(
        drift,
        "fetch_json",
        _live_payloads(_expected_scopes(), healthy=False),
    )
    failures, _warnings = drift.run_checks(expect_commit=None)
    # scope fetch uses METADATA_PATH (still healthy in this fake); the
    # two health URLs each contribute a failure.
    assert len(failures) == 2
    assert all("HTTP 503" in f for f in failures)


def test_commit_mismatch_is_warning_not_failure(monkeypatch):
    monkeypatch.setattr(
        drift,
        "fetch_json",
        _live_payloads(_expected_scopes(), git_commit="1111111"),
    )
    failures, warnings = drift.run_checks(expect_commit="2222222")
    assert failures == []
    assert len(warnings) == 1
    assert "main is ahead of prod" in warnings[0].replace("\n", " ")


def test_missing_commit_stamp_is_warning_not_failure(monkeypatch):
    """Live images built before the git_commit stamp report nothing (or
    "unknown"); that's a warning, not an outage."""
    monkeypatch.setattr(
        drift,
        "fetch_json",
        _live_payloads(_expected_scopes(), git_commit="unknown"),
    )
    failures, warnings = drift.run_checks(expect_commit="2222222")
    assert failures == []
    assert len(warnings) == 1
    assert "no git_commit stamp" in warnings[0]


def test_commit_prefix_match_is_clean(monkeypatch):
    """Short-SHA lengths differ across git configs (7 vs 8+ chars);
    prefix agreement in either direction counts as a match."""
    monkeypatch.setattr(
        drift,
        "fetch_json",
        _live_payloads(_expected_scopes(), git_commit="abc1234"),
    )
    failures, warnings = drift.run_checks(expect_commit="abc12345")
    assert failures == []
    assert warnings == []


# ---------------------------------------------------------------------
# 3. main(): exit codes + report file (the prod-drift.yml wiring)
# ---------------------------------------------------------------------


def test_main_exits_zero_and_writes_report_when_green(monkeypatch, tmp_path):
    monkeypatch.setattr(
        drift, "fetch_json", _live_payloads(_expected_scopes())
    )
    report = tmp_path / "drift-report.md"
    rc = drift.main(["--report", str(report)])
    assert rc == 0
    assert "OK" in report.read_text(encoding="utf-8")


def test_main_exits_one_and_report_names_drift(monkeypatch, tmp_path):
    expected = _expected_scopes()
    monkeypatch.setattr(
        drift, "fetch_json", _live_payloads(expected[:-1])
    )
    report = tmp_path / "drift-report.md"
    rc = drift.main(["--report", str(report)])
    assert rc == 1
    text = report.read_text(encoding="utf-8")
    assert "FAILED" in text
    assert "SCOPE DRIFT" in text
