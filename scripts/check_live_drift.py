"""Live-prod vs main drift monitor (deploy-standard hardening, 2026-07-02).

Compares the LIVE server's public OAuth scope surface against the scope
set derived from THIS checkout's source, and asserts /health is serving.
Run by ``.github/workflows/prod-drift.yml`` on a 6h cron; a non-zero exit
fails that workflow, which is the alert (plus an auto-filed GitHub issue).

Why this exists: ``DEPLOY_ENABLED=false`` turns merge=deploy into
merge=nothing SILENTLY (a skipped deploy job renders as a green run).
That hid 18+ days of prod-vs-main drift, including an undeployed
security fix, until a manual audit found it (see
``_audit/2026-06-29-internal-engineering-audit.md``, facet 4). This
script is the machine backstop for every stale-prod cause observed so
far: the toggle left on, a forgotten resume, a failed dispatch (Fly
billing 403), and out-of-band deploys of the wrong ref.

What it checks (in order):

1. EXPECTED scope surface, derived from SOURCE (never hardcoded):
   ``WORKSPACE_SCOPES`` is AST-parsed out of ``src/appscriptly/auth.py``
   and ``IDENTITY_SCOPES`` out of ``src/appscriptly/oauth_google.py``.
   The live endpoint serves ``sorted(GOOGLE_API_SCOPES)`` where
   ``GOOGLE_API_SCOPES = [*IDENTITY_SCOPES, *WORKSPACE_SCOPES]``
   (see ``oauth_google.py`` + ``http_server/routes/observability.py``),
   so expected == identity + workspace. AST parsing (not import) keeps
   this script stdlib-only: ``auth.py`` imports google-auth at module
   load, so importing it would drag the whole dependency tree into a
   monitor that only needs two string lists. The parse is loud: if a
   refactor turns either list into anything but a literal list of
   strings, this script exits non-zero with instructions (and
   ``tests/unit/test_check_live_drift.py`` pins parser == import truth).

2. LIVE scope surface: ``GET
   https://mcp.appscriptly.com/.well-known/oauth-protected-resource``
   -> ``scopes_supported``. FAIL (exit 1) when the live SET differs
   from the expected set. Sets, not just counts: counts can coincide
   while contents differ. The googleapis-auth scope COUNTS are also
   reported explicitly on both sides for the human reading the log.

3. /health == HTTP 200 with ``"ok": true`` on BOTH hostnames (the
   canonical custom domain AND the fly.dev app hostname). FAIL on
   either being down: an unreachable /health is exactly the
   billing-suspension outage class this monitor exists to catch.

4. SOFT commit check (warning, never a failure): when the live /health
   payload carries ``git_commit`` (ships with this same change) and
   ``--expect-commit`` was passed, a mismatch prints a WARNING that
   main is ahead of prod. Warning-not-failure because a merge that
   changes no scopes legitimately races the deploy for a few minutes;
   the hard gate stays the scope set.

Repo guardrail honored: this runs as a FILE (``python
scripts/check_live_drift.py``), never ``python -c``.

Exit codes: 0 = no drift, everything healthy; 1 = drift or outage or
a parse/fetch failure (all loud).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_PY = REPO_ROOT / "src" / "appscriptly" / "auth.py"
OAUTH_GOOGLE_PY = REPO_ROOT / "src" / "appscriptly" / "oauth_google.py"

CANONICAL_BASE = "https://mcp.appscriptly.com"
FLY_BASE = "https://sundeepg98-docs-mcp.fly.dev"
METADATA_PATH = "/.well-known/oauth-protected-resource"
HEALTH_PATH = "/health"

GOOGLEAPIS_AUTH_PREFIX = "https://www.googleapis.com/auth/"

FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAY_S = 10
FETCH_TIMEOUT_S = 20


def _annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions annotation when running in Actions;
    plain stderr otherwise. ``level`` is ``error`` or ``warning``."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Annotation payloads are single-line; collapse newlines.
        print(f"::{level}::{message}".replace("\n", " "))
    else:
        print(f"{level.upper()}: {message}", file=sys.stderr)


class DriftCheckError(RuntimeError):
    """Raised for any condition that must fail the monitor loudly."""


def extract_scope_list(module_path: Path, var_name: str) -> list[str]:
    """AST-extract a module-level ``var_name = [str, ...]`` literal.

    Loud by design: any shape surprise (missing assignment, non-list,
    non-string element) raises ``DriftCheckError`` telling the editor
    to update this script alongside the refactor. The companion unit
    test asserts this parse equals the imported runtime value, so the
    two cannot silently diverge while CI is green.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    value = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == var_name:
                value = node.value
        if value is None:
            continue
        if not isinstance(value, ast.List):
            raise DriftCheckError(
                f"{var_name} in {module_path} is no longer a plain list "
                f"literal; scripts/check_live_drift.py must be updated to "
                f"match the refactor (its unit test should have caught this)."
            )
        scopes: list[str] = []
        for element in value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                raise DriftCheckError(
                    f"{var_name} in {module_path} contains a non-string-"
                    f"literal element; scripts/check_live_drift.py only "
                    f"understands literal string lists. Update the script."
                )
            scopes.append(element.value)
        return scopes
    raise DriftCheckError(
        f"could not find a module-level `{var_name} = [...]` assignment "
        f"in {module_path}; was it renamed? Update scripts/check_live_drift.py."
    )


def expected_connector_scopes() -> tuple[list[str], list[str]]:
    """Return ``(identity_scopes, workspace_scopes)`` derived from source."""
    workspace = extract_scope_list(AUTH_PY, "WORKSPACE_SCOPES")
    identity = extract_scope_list(OAUTH_GOOGLE_PY, "IDENTITY_SCOPES")
    if not workspace:
        raise DriftCheckError(f"WORKSPACE_SCOPES parsed EMPTY from {AUTH_PY}")
    if not identity:
        raise DriftCheckError(f"IDENTITY_SCOPES parsed EMPTY from {OAUTH_GOOGLE_PY}")
    return identity, workspace


def fetch_json(url: str) -> dict:
    """GET ``url`` and parse the JSON body, retrying transient failures.

    Any persistent failure raises ``DriftCheckError`` -> exit 1. An
    unreachable endpoint IS an alert condition (billing-suspension
    outages take /health down entirely), never something to skip past.
    """
    last_error: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "appscriptly-prod-drift-monitor"}
            )
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
            if status != 200:
                raise DriftCheckError(f"GET {url} returned HTTP {status}")
            return json.loads(body)
        except DriftCheckError:
            raise
        except Exception as exc:  # URLError, timeout, JSON decode, ...
            last_error = exc
            if attempt < FETCH_ATTEMPTS:
                print(
                    f"  attempt {attempt}/{FETCH_ATTEMPTS} for {url} failed "
                    f"({exc!r}); retrying in {FETCH_RETRY_DELAY_S}s"
                )
                time.sleep(FETCH_RETRY_DELAY_S)
    raise DriftCheckError(
        f"GET {url} failed after {FETCH_ATTEMPTS} attempts: {last_error!r}"
    )


def _count_googleapis(scopes: list[str]) -> int:
    return sum(1 for s in scopes if s.startswith(GOOGLEAPIS_AUTH_PREFIX))


def run_checks(expect_commit: str | None) -> tuple[list[str], list[str]]:
    """Run every check; return ``(failures, warnings)`` as report lines."""
    failures: list[str] = []
    warnings: list[str] = []

    # ---- 1. expected surface, derived from source ---------------------
    identity, workspace = expected_connector_scopes()
    expected = sorted(set(identity) | set(workspace))
    expected_workspace_count = len(workspace)
    expected_googleapis_count = _count_googleapis(expected)
    print(f"expected (from src/appscriptly/auth.py WORKSPACE_SCOPES): "
          f"{expected_workspace_count} workspace scopes")
    print(f"expected (plus oauth_google.py IDENTITY_SCOPES): "
          f"{len(expected)} connector scopes total, "
          f"{expected_googleapis_count} of them googleapis auth scopes")

    # ---- 2. live scope surface ----------------------------------------
    metadata_url = CANONICAL_BASE + METADATA_PATH
    print(f"fetching {metadata_url}")
    try:
        metadata = fetch_json(metadata_url)
        live = metadata.get("scopes_supported")
        if not isinstance(live, list) or not all(isinstance(s, str) for s in live):
            failures.append(
                f"{metadata_url} returned no usable scopes_supported list: "
                f"{live!r}"
            )
        else:
            live_sorted = sorted(live)
            live_googleapis_count = _count_googleapis(live_sorted)
            print(f"live: {len(live_sorted)} connector scopes, "
                  f"{live_googleapis_count} of them googleapis auth scopes")
            missing = sorted(set(expected) - set(live_sorted))
            unexpected = sorted(set(live_sorted) - set(expected))
            if missing or unexpected:
                lines = [
                    "SCOPE DRIFT: live scopes_supported != scope set derived "
                    "from main's source.",
                    f"  expected count: {len(expected)} "
                    f"(googleapis auth scopes: {expected_googleapis_count})",
                    f"  live count:     {len(live_sorted)} "
                    f"(googleapis auth scopes: {live_googleapis_count})",
                ]
                if missing:
                    lines.append("  missing from live (main is ahead of prod?):")
                    lines.extend(f"    - {s}" for s in missing)
                if unexpected:
                    lines.append("  live-only (prod ahead of main / wrong ref?):")
                    lines.extend(f"    - {s}" for s in unexpected)
                lines.append(
                    "  likely causes: DEPLOY_ENABLED=false left on, a failed "
                    "dispatch (Fly billing 403), or an out-of-band flyctl "
                    "deploy. Runbook: docs/runbooks/deploy-rollback.md"
                )
                failures.append("\n".join(lines))
            else:
                print("scope surface matches (set equality, counts equal)")
    except DriftCheckError as exc:
        failures.append(str(exc))

    # ---- 3. /health on both hostnames ----------------------------------
    live_commit: str | None = None
    for base in (CANONICAL_BASE, FLY_BASE):
        health_url = base + HEALTH_PATH
        print(f"fetching {health_url}")
        try:
            payload = fetch_json(health_url)
            if payload.get("ok") is not True:
                failures.append(
                    f"{health_url} answered 200 but ok != true: {payload!r}"
                )
            else:
                print(f"  healthy: {payload!r}")
            if live_commit is None and isinstance(payload.get("git_commit"), str):
                live_commit = payload["git_commit"]
        except DriftCheckError as exc:
            failures.append(str(exc))

    # ---- 4. soft commit comparison (warning only) -----------------------
    if expect_commit:
        if live_commit is None or live_commit == "unknown":
            warnings.append(
                "live /health carries no git_commit stamp yet (image predates "
                "the deploy-standard stamping, or was built without the "
                "GIT_COMMIT build-arg). Expected after the next CI deploy."
            )
        elif not (
            live_commit.startswith(expect_commit)
            or expect_commit.startswith(live_commit)
        ):
            warnings.append(
                f"live git_commit={live_commit} != main {expect_commit}: main "
                f"is ahead of prod (or prod runs a different ref). Not a "
                f"failure by itself (deploys race merges by minutes), but if "
                f"this persists across runs the deploy pipe is jammed. See "
                f"docs/runbooks/deploy-rollback.md"
            )
        else:
            print(f"live git_commit {live_commit} matches main {expect_commit}")

    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "prod-drift monitor").splitlines()[0])
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="also write the failure/warning report to this file "
        "(used by prod-drift.yml as the GitHub-issue body)",
    )
    parser.add_argument(
        "--expect-commit",
        default=None,
        help="short SHA the live server is expected to serve; mismatch is a "
        "WARNING (main may legitimately be minutes ahead of prod)",
    )
    args = parser.parse_args(argv)

    try:
        failures, warnings = run_checks(args.expect_commit)
    except DriftCheckError as exc:
        # Derivation failures (source parse) land here: also loud.
        failures, warnings = [str(exc)], []

    for warning in warnings:
        _annotate("warning", warning)
    for failure in failures:
        _annotate("error", failure)

    report_lines: list[str] = []
    if failures:
        report_lines.append("## prod-drift monitor: FAILED")
        report_lines.append("")
        for failure in failures:
            report_lines.append("```")
            report_lines.append(failure)
            report_lines.append("```")
    else:
        report_lines.append("## prod-drift monitor: OK")
    if warnings:
        report_lines.append("")
        report_lines.append("Warnings (non-fatal):")
        for warning in warnings:
            report_lines.append(f"- {warning}")
    report = "\n".join(report_lines) + "\n"

    if args.report is not None:
        args.report.write_text(report, encoding="utf-8")
        print(f"report written to {args.report}")

    if failures:
        print(f"FAILED: {len(failures)} drift/outage finding(s)")
        return 1
    print("OK: live prod matches main's scope surface and /health is serving")
    return 0


if __name__ == "__main__":
    sys.exit(main())
