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
  3. test_owned_by_app_agrees_with_trash_outcome        TODO (needs probe revert)
  4. test_inject_matches_fragmented_runs                TODO (deep retrofit change)
  5. test_preview_flags_what_convert_truncates          TODO (threshold drift)
  6. test_auth_pkce_consistency_every_url               TODO (lib auto-enables PKCE
                                                              via path we don't yet
                                                              isolate — needs URL
                                                              post-strip)
  7. test_tool_descriptions_truthful                    [v1.2.0]
  8. test_tool_discoverability_via_server_info          TODO (filter tool out)

v1.2.0 ships mutations for the 3 cleanest patches. The TODOs each
need a more involved diff than a single string replace. Adding any
of them sharpens the gate by one notch. Document the patch shape
inline when adding so future-me can verify the pattern is unique
before committing.
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
