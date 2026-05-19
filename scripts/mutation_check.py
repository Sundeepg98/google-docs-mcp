#!/usr/bin/env python
"""Mutation testing — prove each named regression guard catches its bug.

For each defined mutation: apply a known bug-injecting patch, run
pytest filtered to the named guard, assert the test goes red. If any
guard fails to catch its mutation, the script exits non-zero (CI
fails the build) — meaning that guard is asleep and can't be trusted.

Writes ``mutation-check.json`` with the per-guard outcome. The
Docker image bakes it in alongside ``test-results.json``; the runtime
surfaces it via ``gdocs_server_info.test_suite.mutation_check``.

The 8 named regression guards from the v1.1.x cycle:
  1. test_trash_file_id_accepts_str_or_list             [v1.2.0]
  2. test_deploy_webapp_body_does_not_include_entryPoints [v1.2.0]
  3. test_owned_by_app_agrees_with_trash_outcome        [v1.2.1]
  4. test_inject_matches_fragmented_runs                [v1.2.1]
  5. test_preview_flags_what_convert_truncates          [v1.2.1]
  6. test_auth_pkce_consistency_every_url               [v1.2.1]
  7. test_tool_descriptions_truthful                    [v1.2.0]
  8. test_tool_discoverability_via_server_info          [v1.2.1]

v1.2.1: all 8 named guards now have automated mutations. Full
coverage. If a future guard is added, add its mutation here
alongside; the gate is only as sharp as its asleep guards.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Mutation:
    guard: str             # test name expected to catch this mutation
    test_path: str         # pytest nodeid
    description: str       # one-line: what bug this mutation reintroduces
    file: str              # source file to mutate
    find: str              # text to find (must be uniquely present)
    replace: str           # text to substitute (the "bug")


MUTATIONS: list[Mutation] = [
    Mutation(
        guard="test_trash_file_id_accepts_str_or_list",
        test_path="tests/unit/test_tool_schemas.py::test_trash_file_id_accepts_str_or_list[gdocs_trash_file]",
        description="revert `file_id: str | list[str]` -> `file_id: str` on gdocs_trash_file",
        file="src/google_docs_mcp/server.py",
        find="def gdocs_trash_file(file_id: str | list[str]) -> dict:",
        replace="def gdocs_trash_file(file_id: str) -> dict:",
    ),
    Mutation(
        guard="test_deploy_webapp_body_does_not_include_entryPoints",
        test_path="tests/unit/test_gas_deploy.py::test_deploy_webapp_body_does_not_include_entryPoints",
        description="re-add entryPoints to deployments.create body (v1.1.1 bug)",
        file="src/google_docs_mcp/gas_deploy/client.py",
        find='                    "description": description,\n                },\n            )',
        replace='                    "description": description,\n                    "entryPoints": [{"entryPointType": "WEB_APP"}],\n                },\n            )',
    ),
    # NOTE: PKCE mutation removed from v1.2.0 — commenting out
    # `flow.code_verifier = code_verifier` didn't break the test
    # because the installed google_auth_oauthlib appears to enable PKCE
    # via another path. Needs a deeper patch (intercept the URL and
    # strip code_challenge post-build). Documented under TODO above.
    Mutation(
        guard="test_tool_descriptions_truthful",
        test_path="tests/unit/test_tool_schemas.py::test_tool_descriptions_truthful",
        description="inject 'works without setup' into a tool description (no OAuth clarifier nearby)",
        file="src/google_docs_mcp/server.py",
        # Pure 'without setup' phrase with NO OAuth/authoriz/consent
        # synonym within 150 chars — the qualifier check should miss.
        find='"""DEFAULT tool for building a tabbed Google Doc from text content.',
        replace='"""DEFAULT tabbed Google Doc builder. Works without setup.',
    ),
    Mutation(
        guard="test_preview_flags_what_convert_truncates",
        test_path="tests/unit/test_preview_threshold_consistency.py::test_preview_flags_what_convert_truncates",
        description="drift preview's title-truncation threshold 50 -> 60",
        file="src/google_docs_mcp/preview.py",
        # Two failure mechanisms triggered simultaneously: the test's
        # fixture heading is exactly 60 chars, so `60 > 60` becomes
        # False and no warning fires; even if one fired, the message
        # would interpolate '60' not '50' (test asserts '50' in msg).
        find="TITLE_MAX_CHARS = 50  # Google Docs API hard limit (returns 400 above this)",
        replace="TITLE_MAX_CHARS = 60  # Google Docs API hard limit (returns 400 above this)",
    ),
    Mutation(
        guard="test_inject_matches_fragmented_runs",
        test_path="tests/unit/test_retrofit_text_normalization.py::test_inject_matches_fragmented_runs",
        description="add break after first <w:t> read -> only first run extracted",
        file="src/google_docs_mcp/retrofit.py",
        # Reintroduces the pre-v0.15.1 bug exactly: extraction stopped
        # after the first text run, so phrases split across multiple
        # <w:r> elements (Word's spell-check/rPr fragmentation pattern)
        # extracted to just the first chunk. Fixture paragraph
        # ["Sec", "tion", " ", "Banner"] becomes just "Sec".
        find='        if tag == qn("w:t"):\n            parts.append(node.text or "")',
        replace='        if tag == qn("w:t"):\n            parts.append(node.text or "")\n            break',
    ),
    Mutation(
        guard="test_auth_pkce_consistency_every_url",
        test_path="tests/unit/test_oauth_google.py::test_auth_pkce_consistency_every_url",
        description="override both PKCE paths after explicit assignment -> URLs lose code_challenge",
        file="src/google_docs_mcp/oauth_google.py",
        # The v1.2.0 attempt commented out `flow.code_verifier = ...`
        # but Flow.authorization_url() auto-generates a 128-char
        # verifier when code_verifier is None AND
        # autogenerate_code_verifier=True (Flow.__init__ default).
        # PKCE survived via the fallback. This mutation kills BOTH
        # paths AFTER the original assignment (modeling a real
        # regression where someone adds "cleanup" code that
        # inadvertently nukes PKCE):
        #   - flow.code_verifier = None  -> skips the second
        #     `if self.code_verifier:` block that emits code_challenge
        #   - autogenerate_code_verifier = False  -> skips the
        #     first guard's auto-population path
        find='    import secrets as _secrets\n    code_verifier = _secrets.token_urlsafe(48)  # 64 chars, within RFC 7636 limits\n    flow.code_verifier = code_verifier\n\n    state = sign_state(\n        user_id, signing_key, ttl_seconds=ttl_seconds,\n        code_verifier=code_verifier,\n    )',
        replace='    import secrets as _secrets\n    code_verifier = _secrets.token_urlsafe(48)  # 64 chars, within RFC 7636 limits\n    flow.code_verifier = code_verifier\n    flow.code_verifier = None\n    flow.autogenerate_code_verifier = False\n\n    state = sign_state(\n        user_id, signing_key, ttl_seconds=ttl_seconds,\n        code_verifier=code_verifier,\n    )',
    ),
    Mutation(
        guard="test_owned_by_app_agrees_with_trash_outcome",
        test_path="tests/unit/test_soft_failure_contracts.py::test_owned_by_app_agrees_with_trash_outcome",
        description="flip 403-probe branch from False to True -> probe lies about writability",
        file="src/google_docs_mcp/drive_api.py",
        # Reintroduces the v0.19.0 bug exactly: probe wrongly reports
        # owned_by_app=True even when the no-op update returns 403
        # appNotAuthorizedToFile. In the external-file scenario, find
        # claims owned_by_app=True but trash_drive_file still 403s
        # → cross-tool inconsistency, the v0.19.0 bug class.
        find='                elif isinstance(exception, HttpError) and exception.status_code == 403:\n                    # Any 403 means we can\'t write. The specific\n                    # reason we care about is appNotAuthorizedToFile,\n                    # but any 403 is "not writable for our purposes."\n                    write_results[fid] = False',
        replace='                elif isinstance(exception, HttpError) and exception.status_code == 403:\n                    # Any 403 means we can\'t write. The specific\n                    # reason we care about is appNotAuthorizedToFile,\n                    # but any 403 is "not writable for our purposes."\n                    write_results[fid] = True',
    ),
    Mutation(
        guard="test_tool_discoverability_via_server_info",
        test_path="tests/unit/test_tool_schemas.py::test_tool_discoverability_via_server_info",
        description="drop alphabetically-first tool from server_info.tools via slice",
        file="src/google_docs_mcp/server.py",
        # Minimal slice mutation: tools list is the same source of
        # truth as mcp.list_tools(), but server_info filters one out
        # → set-equality assertion fires AND tool_count diverges
        # (20 vs 21). Models a future refactor that adds a "private"
        # tool filter that accidentally drops a public tool.
        find="        tool_names = sorted(t.name for t in tools)",
        replace="        tool_names = sorted(t.name for t in tools)[1:]",
    ),
]


def apply_mutation(m: Mutation) -> bool:
    path = Path(m.file)
    original = path.read_text(encoding="utf-8")
    if m.find not in original:
        print(f"  !!! find pattern NOT present in {m.file}")
        print(f"      pattern: {m.find!r}")
        return False
    count = original.count(m.find)
    if count > 1:
        print(f"  !!! find pattern matches {count} times — must be unique")
        return False
    path.write_text(original.replace(m.find, m.replace, 1), encoding="utf-8")
    return True


def revert(m: Mutation) -> None:
    """Restore the original via git checkout — bullet-proof."""
    subprocess.run(
        ["git", "checkout", "--", m.file],
        check=True, capture_output=True,
    )


def run_pytest(test_path: str) -> int:
    """Run pytest filtered to one test; return exit code (0=passed, 1+=failed)."""
    result = subprocess.run(
        [
            "python", "-m", "pytest", test_path,
            "-q", "--no-header", "-x",
        ],
        capture_output=True, text=True,
    )
    return result.returncode


def main() -> int:
    results: list[dict] = []
    for m in MUTATIONS:
        print(f"\n=== mutating: {m.guard} ===")
        print(f"    {m.description}")
        t0 = time.monotonic()
        applied = apply_mutation(m)
        try:
            if not applied:
                results.append({
                    "guard": m.guard, "caught": False,
                    "reason": "mutation pattern not present in source — out of date?",
                    "duration_ms": 0,
                })
                continue
            exit_code = run_pytest(m.test_path)
            duration_ms = int((time.monotonic() - t0) * 1000)
            caught = exit_code != 0  # nonzero = pytest failed = guard caught it
            print(f"    pytest exit={exit_code}; guard {'CAUGHT' if caught else 'ASLEEP'} ({duration_ms}ms)")
            results.append({
                "guard": m.guard, "caught": caught, "duration_ms": duration_ms,
            })
        finally:
            revert(m)

    ran = len(results)
    caught = sum(1 for r in results if r["caught"])
    asleep = [r["guard"] for r in results if not r["caught"]]
    status = "passed" if (ran > 0 and caught == ran) else "failed"

    payload = {
        "ran": ran,
        "caught": caught,
        "status": status,
        "asleep_guards": asleep,
        "results": results,
    }
    Path("mutation-check.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )
    print("\n=== mutation-check.json ===")
    print(json.dumps(payload, indent=2))

    if status != "passed":
        print(f"\n[X] {len(asleep)} guard(s) ASLEEP -- failing build.")
        return 1
    print(f"\n[OK] all {caught}/{ran} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
