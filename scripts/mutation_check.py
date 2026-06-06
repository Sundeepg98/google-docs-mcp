#!/usr/bin/env python
"""Mutation testing — prove each named regression guard catches its bug.

For each defined mutation: apply a known bug-injecting patch, run the
FULL unit suite, then classify by what failed:

  caught            -> exactly the targeted test failed (clean catch)
  asleep_guard      -> target test did NOT fail (guard didn't notice)
  stale_patch       -> patch's `find` text isn't in source any more,
                       OR patch applied but caused zero failures
                       (the mutation no longer models its bug)
  imprecise_patch   -> target test failed AND unrelated tests also did
                       (mutation is too broad — collateral damage)

Writes ``mutation-check.json`` with per-guard outcome + aggregate
buckets. The Docker image bakes it in alongside ``test-results.json``;
the runtime surfaces it via ``gdocs_server_info.test_suite.mutation_check``.

Build passes only when every mutation == "caught". Any stale_patch or
imprecise_patch fails the build — these are the failure modes that
silently hollow out the gate (v1.2.2: stale-patch rot defense).

The 8 named regression guards from the v1.1.x cycle:
  1. test_trash_file_id_accepts_str_or_list             [v1.2.0]
  2. test_deploy_webapp_body_does_not_include_entryPoints [v1.2.0]
  3. test_owned_by_app_agrees_with_trash_outcome        [v1.2.1]
  4. test_inject_matches_fragmented_runs                [v1.2.1]
  5. test_preview_flags_what_convert_truncates          [v1.2.1]
  6. test_auth_pkce_consistency_every_url               [v1.2.1]
  7. test_tool_descriptions_truthful                    [v1.2.0]
  8. test_tool_discoverability_via_server_info          [v1.2.1]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Mutation:
    guard: str             # test name expected to catch this mutation
    test_path: str         # pytest nodeid (param suffix matched as prefix)
    description: str       # one-line: what bug this mutation reintroduces
    file: str              # source file to mutate
    find: str              # text to find (must be uniquely present)
    replace: str           # text to substitute (the "bug")
    # Other tests we EXPECT this mutation to break — sibling guards
    # testing the same code path. Failures matching these are NOT
    # imprecise; they're defense in depth. Each entry matches by
    # exact equality or as a parametrized prefix (same rule as test_path).
    expected_collateral: list[str] = field(default_factory=list)


MUTATIONS: list[Mutation] = [
    Mutation(
        guard="test_trash_file_id_accepts_str_or_list",
        test_path="tests/unit/test_tool_schemas.py::test_trash_file_id_accepts_str_or_list[gdocs_trash_file]",
        description="revert `file_id: str | list[str]` -> `file_id: str` on gdocs_trash_file",
        # M3 moved this from server.py to services/drive/tools.py and
        # added a `creds` first parameter (PR #103/#104).
        file="src/appscriptly/services/drive/tools.py",
        find="def gdocs_trash_file(creds, file_id: str | list[str]) -> dict:",
        replace="def gdocs_trash_file(creds, file_id: str) -> dict:",
    ),
    Mutation(
        guard="test_deploy_webapp_body_does_not_include_entryPoints",
        # M3 moved this from tests/unit/test_gas_deploy.py to the
        # service-co-located tests/unit/services/gas_deploy/test_api.py.
        test_path="tests/unit/services/gas_deploy/test_api.py::test_deploy_webapp_body_does_not_include_entryPoints",
        description="re-add entryPoints to deployments.create body (v1.1.1 bug)",
        # M3 moved gas_deploy/client.py -> services/gas_deploy/api.py.
        file="src/appscriptly/services/gas_deploy/api.py",
        find='                    "description": description,\n                },\n            )',
        replace='                    "description": description,\n                    "entryPoints": [{"entryPointType": "WEB_APP"}],\n                },\n            )',
    ),
    Mutation(
        guard="test_tool_descriptions_truthful",
        test_path="tests/unit/test_tool_schemas.py::test_tool_descriptions_truthful",
        description="inject 'works without setup' into a tool description (no OAuth clarifier nearby)",
        # M3 moved gdocs_make_tabbed_doc from server.py to
        # services/docs/tools.py.
        file="src/appscriptly/services/docs/tools.py",
        find='"""DEFAULT tool for building a tabbed Google Doc from text content.',
        replace='"""DEFAULT tabbed Google Doc builder. Works without setup.',
    ),
    Mutation(
        guard="test_preview_flags_what_convert_truncates",
        test_path="tests/unit/test_preview_threshold_consistency.py::test_preview_flags_what_convert_truncates",
        description="drift preview's title-truncation threshold 50 -> 60",
        file="src/appscriptly/preview.py",
        find="TITLE_MAX_CHARS = 50  # Google Docs API hard limit (returns 400 above this)",
        replace="TITLE_MAX_CHARS = 60  # Google Docs API hard limit (returns 400 above this)",
    ),
    Mutation(
        guard="test_inject_matches_fragmented_runs",
        test_path="tests/unit/test_retrofit_text_normalization.py::test_inject_matches_fragmented_runs",
        description="add break after first <w:t> read -> only first run extracted",
        file="src/appscriptly/retrofit.py",
        find='        if tag == qn("w:t"):\n            parts.append(node.text or "")',
        replace='        if tag == qn("w:t"):\n            parts.append(node.text or "")\n            break',
        # _extract_visible_text is shared by both fragmented_runs and
        # nbsp_via_sym tests; this regression legitimately trips both.
        # Declare the sibling as expected (defense in depth, not collateral).
        expected_collateral=[
            "tests/unit/test_retrofit_text_normalization.py::test_inject_matches_nbsp_via_sym",
        ],
    ),
    Mutation(
        guard="test_auth_pkce_consistency_every_url",
        test_path="tests/unit/test_oauth_google.py::test_auth_pkce_consistency_every_url",
        description="override both PKCE paths after explicit assignment -> URLs lose code_challenge",
        file="src/appscriptly/oauth_google.py",
        find='    import secrets as _secrets\n    code_verifier = _secrets.token_urlsafe(48)  # 64 chars, within RFC 7636 limits\n    flow.code_verifier = code_verifier\n\n    state = sign_state(\n        user_id, signing_key, ttl_seconds=ttl_seconds,\n        code_verifier=code_verifier,\n    )',
        replace='    import secrets as _secrets\n    code_verifier = _secrets.token_urlsafe(48)  # 64 chars, within RFC 7636 limits\n    flow.code_verifier = code_verifier\n    flow.code_verifier = None\n    flow.autogenerate_code_verifier = False\n\n    state = sign_state(\n        user_id, signing_key, ttl_seconds=ttl_seconds,\n        code_verifier=code_verifier,\n    )',
    ),
    Mutation(
        guard="test_owned_by_app_agrees_with_trash_outcome",
        test_path="tests/unit/test_soft_failure_contracts.py::test_owned_by_app_agrees_with_trash_outcome",
        description="flip 403-probe branch from False to True -> probe lies about writability",
        # M3 moved drive_api.py -> services/drive/api.py.
        file="src/appscriptly/services/drive/api.py",
        find='                elif isinstance(exception, HttpError) and exception.status_code == 403:\n                    # Any 403 means we can\'t write. The specific\n                    # reason we care about is appNotAuthorizedToFile,\n                    # but any 403 is "not writable for our purposes."\n                    write_results[fid] = False',
        replace='                elif isinstance(exception, HttpError) and exception.status_code == 403:\n                    # Any 403 means we can\'t write. The specific\n                    # reason we care about is appNotAuthorizedToFile,\n                    # but any 403 is "not writable for our purposes."\n                    write_results[fid] = True',
    ),
    Mutation(
        guard="test_tool_discoverability_via_server_info",
        test_path="tests/unit/test_tool_schemas.py::test_tool_discoverability_via_server_info",
        description="drop alphabetically-first tool from server_info.tools via slice",
        # v2.2.2/PR #114 moved gdocs_server_info from server.py to the
        # new services/admin/ folder along with the other 6 admin tools.
        file="src/appscriptly/services/admin/tools.py",
        find="        tool_names = sorted(t.name for t in tools)",
        replace="        tool_names = sorted(t.name for t in tools)[1:]",
        # server_info.tool_count derives from the same list; dropping
        # one tool also makes count diverge from len(list_tools), which
        # test_server_info_self_consistency notices. Defense in depth.
        expected_collateral=[
            "tests/unit/test_server_info.py::test_server_info_self_consistency",
        ],
    ),
]


def apply_mutation(m: Mutation) -> str | None:
    """Apply the mutation's find/replace. Return the ORIGINAL file
    contents on success so the caller can revert from memory; return
    None when the find pattern isn't uniquely present (stale_patch).

    Returning the original bytes (instead of relying on `git checkout`)
    keeps the gate safe to run against an unclean working tree —
    uncommitted edits in mutated source files (which v1.2.2 hit
    locally) would otherwise be silently wiped on revert.
    """
    path = Path(m.file)
    original = path.read_text(encoding="utf-8")
    if m.find not in original:
        print(f"  !!! find pattern NOT present in {m.file}")
        print(f"      pattern: {m.find!r}")
        return None
    count = original.count(m.find)
    if count > 1:
        print(f"  !!! find pattern matches {count} times — must be unique")
        return None
    path.write_text(original.replace(m.find, m.replace, 1), encoding="utf-8")
    return original


def revert(m: Mutation, original: str | None) -> None:
    """Restore the original file contents from memory. Safe to call
    even when nothing was mutated (original is None → no-op)."""
    if original is None:
        return
    Path(m.file).write_text(original, encoding="utf-8")


def run_full_unit_suite() -> tuple[int, list[str]]:
    """Run the FULL unit suite. Return (exit_code, failed_nodeids).

    We need the whole suite, not just the targeted test, so we can
    distinguish "the targeted guard caught it" (clean catch) from
    "the patch broke unrelated tests too" (imprecise_patch).

    Hypothesis seed is pinned (--hypothesis-seed=0) so property-based
    tests in tests/unit/test_docx_import.py and similar generate the
    SAME inputs every run. Without this, hypothesis picks a fresh
    random seed per invocation, which means a mutation that's truly
    unrelated to a property test can still trip it via input drift
    and surface as a spurious `imprecise_patch` (this is what was
    keeping deploy red after v2.2.2/PR #114).
    """
    import os
    fd, json_path = tempfile.mkstemp(suffix=".json", prefix="mutation_pytest_")
    # Close the handle immediately — Windows can't unlink an open
    # file. Pytest will overwrite json_path via --json-report-file.
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "python", "-m", "pytest", "tests/unit",
                "-q", "--tb=no", "--no-header",
                "--hypothesis-seed=0",
                "--json-report", f"--json-report-file={json_path}",
            ],
            capture_output=True, text=True,
        )
        try:
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return result.returncode, []
        failed = [
            t.get("nodeid", "")
            for t in data.get("tests", [])
            if t.get("outcome") == "failed"
        ]
        return result.returncode, failed
    finally:
        Path(json_path).unlink(missing_ok=True)


def _matches_nodeid(nodeid: str, declared: str) -> bool:
    """Does a pytest nodeid match a declared test_path?

    Exact match, OR parametrized variant (`declared[A]`). Allows the
    Mutation to declare a base name like `test_X` and match
    parametrized failures like `test_X[case1]`, `test_X[case2]`, etc.
    """
    return nodeid == declared or nodeid.startswith(declared + "[")


def classify_outcome(m: Mutation, applied: bool, failed_tests: list[str]) -> dict:
    """Classify a mutation outcome. Pure function — testable.

    Returns a dict with key "outcome" in {"caught", "stale_patch",
    "imprecise_patch", "asleep_guard"} plus optional context fields.

    A failure counts as "the target" if it matches `m.test_path` by
    exact equality or by parametrized prefix. Failures matching any
    entry in `m.expected_collateral` are treated as declared siblings
    (defense in depth) and don't count as unexpected.
    """
    if not applied:
        return {
            "outcome": "stale_patch",
            "reason": "find pattern not present in source",
        }
    if not failed_tests:
        return {
            "outcome": "stale_patch",
            "reason": "patch applied but caused zero test failures — mutation no longer models its bug",
        }

    target_failed = any(_matches_nodeid(t, m.test_path) for t in failed_tests)

    def is_expected(nodeid: str) -> bool:
        if _matches_nodeid(nodeid, m.test_path):
            return True
        return any(_matches_nodeid(nodeid, sib) for sib in m.expected_collateral)

    unexpected = [t for t in failed_tests if not is_expected(t)]

    if target_failed and not unexpected:
        return {"outcome": "caught"}
    if target_failed and unexpected:
        return {
            "outcome": "imprecise_patch",
            "unexpected_failures": unexpected[:10],
        }
    # target_failed is False, but other tests failed → guard is asleep
    return {
        "outcome": "asleep_guard",
        "collateral_failures": unexpected[:10],
    }


def aggregate(results: list[dict]) -> dict:
    """Build final payload from per-mutation results. Pure function."""
    ran = len(results)
    caught = sum(1 for r in results if r["outcome"] == "caught")
    asleep_guards = [r["guard"] for r in results if r["outcome"] == "asleep_guard"]
    stale_patches = [r["guard"] for r in results if r["outcome"] == "stale_patch"]
    imprecise_patches = [r["guard"] for r in results if r["outcome"] == "imprecise_patch"]

    # Priority: stale_patch is most fundamental (the gate itself is
    # broken — we can't trust ANY catch claim until it's fixed). Then
    # imprecise (the gate over-reaches), then asleep (a specific
    # guard is broken). Passed only when nothing is wrong.
    if ran == 0:
        status = "failed"
    elif caught == ran:
        status = "passed"
    elif stale_patches:
        status = "stale_patch"
    elif imprecise_patches:
        status = "imprecise_patch"
    else:
        status = "asleep_guard"

    return {
        "ran": ran,
        "caught": caught,
        "status": status,
        "asleep_guards": asleep_guards,
        "stale_patches": stale_patches,
        "imprecise_patches": imprecise_patches,
        "results": results,
    }


def run_mutations(mutations: list[Mutation]) -> dict:
    """Orchestrator: apply / run-suite / classify / revert per mutation."""
    results: list[dict] = []
    for m in mutations:
        print(f"\n=== mutating: {m.guard} ===")
        print(f"    {m.description}")
        t0 = time.monotonic()
        original = apply_mutation(m)
        applied = original is not None
        failed_tests: list[str] = []
        try:
            if applied:
                _, failed_tests = run_full_unit_suite()
            outcome = classify_outcome(m, applied, failed_tests)
            duration_ms = int((time.monotonic() - t0) * 1000)

            label_map = {
                "caught": "CAUGHT",
                "stale_patch": "STALE PATCH",
                "imprecise_patch": "IMPRECISE PATCH (collateral)",
                "asleep_guard": "ASLEEP GUARD",
            }
            label = label_map.get(outcome["outcome"], outcome["outcome"])
            print(f"    {label} ({duration_ms}ms, {len(failed_tests)} test(s) failed)")
            if outcome.get("unexpected_failures"):
                print(f"    unexpected failures: {outcome['unexpected_failures']}")
            if outcome.get("reason"):
                print(f"    reason: {outcome['reason']}")

            entry = {
                "guard": m.guard,
                "outcome": outcome["outcome"],
                # back-compat: keep `caught` as bool so older readers don't break
                "caught": outcome["outcome"] == "caught",
                "duration_ms": duration_ms,
            }
            for k in ("reason", "unexpected_failures", "collateral_failures"):
                if k in outcome:
                    entry[k] = outcome[k]
            results.append(entry)
        finally:
            revert(m, original)
    return aggregate(results)


def main() -> int:
    payload = run_mutations(MUTATIONS)
    Path("mutation-check.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )
    print("\n=== mutation-check.json ===")
    print(json.dumps(payload, indent=2))

    status = payload["status"]
    if status == "passed":
        print(f"\n[OK] all {payload['caught']}/{payload['ran']} mutations caught cleanly.")
        return 0

    print(f"\n[X] mutation_check status: {status}")
    if payload["stale_patches"]:
        print(f"    stale_patches:     {payload['stale_patches']}")
        print(f"    -> their `find` text no longer matches the source, or the patch")
        print(f"       no longer trips any test. Rewrite the mutation to match HEAD.")
    if payload["imprecise_patches"]:
        print(f"    imprecise_patches: {payload['imprecise_patches']}")
        print(f"    -> the patch breaks unrelated tests. Narrow it.")
    if payload["asleep_guards"]:
        print(f"    asleep_guards:     {payload['asleep_guards']}")
        print(f"    -> the named guard didn't notice its bug. Strengthen the test.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
