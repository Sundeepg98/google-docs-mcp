"""gdocs_server_info contract tests.

The tools list MUST match tool_count MUST match the actual registered
count. Inconsistency here means the info tool is lying — a regression
trap for change-detection workflows.
"""
from __future__ import annotations

import asyncio


def test_server_info_self_consistency():
    """tool_count == len(tools) == number of FastMCP-registered tools."""
    from google_docs_mcp.server import mcp, gdocs_server_info

    # gdocs_server_info is registered as an async MCP tool; the
    # FastMCP wrapper makes it callable as a coroutine.
    info = asyncio.run(gdocs_server_info())

    assert info["tool_count"] == len(info["tools"])

    # Also verify against the live registry — same source of truth.
    live_count = len(asyncio.run(mcp.list_tools()))
    assert info["tool_count"] == live_count


def test_server_info_tools_is_sorted():
    """Sorted output gives a stable diff for change detection."""
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert info["tools"] == sorted(info["tools"])


def test_server_info_version_string_present():
    """version must be a non-empty string for deploy fingerprinting."""
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert isinstance(info["version"], str)
    assert info["version"]
    assert info["version"] != "unknown", (
        "version came back 'unknown' — package metadata isn't installed; "
        "run `pip install -e .` in the project root for tests."
    )


def test_server_info_includes_build_provenance_keys():
    """build_time and git_commit keys must exist even if values are 'unknown'."""
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert "build_time" in info
    assert "git_commit" in info


def test_canonical_digest_excludes_meta_block_and_is_stable():
    """The digest computed at deploy time must be reproducible at read
    time with the same canonicalization rules. Tests the hashing
    contract: sort_keys + tight separators + _meta excluded.
    """
    from google_docs_mcp.server import _canonical_digest

    # Same payload, different dict-iteration order → identical digest.
    a = {"summary": {"passed": 5}, "_git_commit": "abc", "_meta": {"digest": "old"}}
    b = {"_meta": {"digest": "different"}, "_git_commit": "abc", "summary": {"passed": 5}}
    assert _canonical_digest(a) == _canonical_digest(b)
    assert _canonical_digest(a).startswith("sha256:")

    # Tampering with the payload changes the digest.
    tampered = {"summary": {"passed": 999}, "_git_commit": "abc"}
    assert _canonical_digest(tampered) != _canonical_digest(a)


def test_test_suite_status_tampered_when_digest_mismatches(tmp_path, monkeypatch):
    """The killer guard: edit the numbers in test-results.json without
    re-signing → server reports status='tampered', not 'passed'."""
    import json
    from google_docs_mcp.server import _read_test_suite_status, _canonical_digest

    # Build a legit results file with correct digest.
    legit = {
        "created": 1748600000.0,
        "summary": {"passed": 203, "failed": 0, "skipped": 0},
        "_git_commit": "abc1234",
        "_ci_run_url": "https://github.com/x/y/actions/runs/1",
    }
    legit["_meta"] = {"digest": _canonical_digest(legit)}

    path = tmp_path / "test-results.json"
    path.write_text(json.dumps(legit))
    monkeypatch.chdir(tmp_path)

    # Sanity: legit file → status=passed.
    result = _read_test_suite_status("abc1234")
    assert result["status"] == "passed", f"sanity check failed: {result!r}"

    # Now tamper: bump the passed count without recomputing the digest.
    tampered = json.loads(path.read_text())
    tampered["summary"]["passed"] = 9999
    path.write_text(json.dumps(tampered))

    result = _read_test_suite_status("abc1234")
    assert result["status"] == "tampered", (
        f"editing the count without re-signing should report "
        f"status='tampered', got: {result!r}"
    )


def test_server_info_includes_test_suite_block():
    """v1.1.2+ contract: test_suite block surfaces CI status.

    Must always be present — even when the test-results.json file is
    missing (vanilla docker build without deploy.sh) the block returns
    {"status": "unknown"} per the documented contract. Omitting the
    field entirely would break the agreement that a single shape can
    be relied on.
    """
    from google_docs_mcp.server import gdocs_server_info

    info = asyncio.run(gdocs_server_info())
    assert "test_suite" in info, (
        "test_suite block missing from gdocs_server_info — "
        "the v1.1.2+ contract requires it always be present"
    )
    suite = info["test_suite"]
    assert isinstance(suite, dict)
    assert "status" in suite
    assert suite["status"] in ("passed", "failed", "unknown")

    # When status is "passed" the full shape applies.
    if suite["status"] == "passed":
        for key in ("last_run", "commit", "passed", "failed", "skipped",
                    "ci_run_url", "report_digest"):
            assert key in suite, (
                f"test_suite.{key} missing when status='passed'; "
                f"got: {suite!r}"
            )
        assert suite["failed"] == 0, (
            f"status='passed' but failed={suite['failed']} — contradiction"
        )
        # report_digest must start with the hash-algorithm prefix so
        # callers can pin the algorithm without parsing.
        assert suite["report_digest"].startswith("sha256:"), (
            f"report_digest format unexpected: {suite['report_digest']!r}"
        )

    # mutation_check block must always be present (v1.2.0+ contract).
    assert "mutation_check" in suite
    mc = suite["mutation_check"]
    assert "status" in mc
    # v1.2.2 added stale_patch, imprecise_patch, asleep_guard as
    # specific failure subtypes that replace the catch-all "failed".
    assert mc["status"] in (
        "passed", "failed", "unknown",
        "stale_patch", "imprecise_patch", "asleep_guard",
    )
    assert "ran" in mc and "caught" in mc


def test_gdocs_test_manifest_exists_and_returns_required_shape():
    """gdocs_test_manifest must return tests + named_regression_guards.
    Status varies (ok/unknown/tampered) depending on artifact state.
    Shape is constant."""
    import asyncio
    from google_docs_mcp.server import gdocs_test_manifest

    result = asyncio.run(gdocs_test_manifest()) if asyncio.iscoroutinefunction(
        gdocs_test_manifest,
    ) else gdocs_test_manifest()
    assert "status" in result
    assert result["status"] in ("ok", "unknown", "tampered")
    assert "total" in result
    assert "tests" in result and isinstance(result["tests"], list)
    assert "named_regression_guards" in result
    guards = result["named_regression_guards"]
    assert "present" in guards and isinstance(guards["present"], list)
    assert "missing" in guards and isinstance(guards["missing"], list)


def test_gdocs_guide_shape_includes_all_5_workflows_and_rules():
    """v1.3.0 self-documenting contract: gdocs_guide must return a
    structured payload an agent can use INSTEAD of any external doc.

    The 5 workflows by name (the acceptance criterion: an agent can
    correctly choose and sequence tools for these without any
    external file) and the 5 operating rules (the failure modes that
    used to require trial-and-error to discover).
    """
    from google_docs_mcp.server import gdocs_guide

    guide = gdocs_guide()

    # Top-level keys are the contract.
    for key in ("server", "workflows", "operating_rules", "tool_groups"):
        assert key in guide, f"gdocs_guide missing top-level key: {key}"

    # server block identifies the build so callers can correlate
    # with gdocs_server_info.
    for key in ("name", "version", "what_it_does",
                "all_tools_prefixed", "more_info"):
        assert key in guide["server"], f"guide.server missing {key}"
    assert guide["server"]["all_tools_prefixed"] == "gdocs_"

    # All 5 named workflows present. If any of these names changes
    # the external doc is no longer the canonical source — update
    # this list deliberately.
    expected_workflow_names = {
        "new_doc",
        "convert_doc_with_headings",
        "retrofit_styled_doc",
        "convert_sandbox_docx",
        "cleanup",
    }
    actual_workflow_names = {w["name"] for w in guide["workflows"]}
    assert actual_workflow_names == expected_workflow_names, (
        f"workflow names drifted: expected {expected_workflow_names}, "
        f"got {actual_workflow_names}"
    )

    # Each workflow has the choreography fields.
    for w in guide["workflows"]:
        for key in ("name", "goal", "tool_sequence", "notes"):
            assert key in w, f"workflow {w.get('name')} missing {key}"
        assert isinstance(w["tool_sequence"], list)
        assert w["tool_sequence"], (
            f"workflow {w['name']} has empty tool_sequence"
        )

    # All 5 operating rules present. We check by topic keyword rather
    # than exact text so wording can evolve without breaking the test.
    rules_blob = " ".join(guide["operating_rules"]).lower()
    for topic in (
        "retrofit",        # never rebuild styled .docx
        "docx_path",       # cloud-chat filesystem rule
        "placeholder",     # placeholder_behavior="rename" rule
        "trash",           # only own files
        "oauth",           # interactive consent
    ):
        assert topic in rules_blob, (
            f"operating_rules missing the '{topic}' rule. "
            f"Got: {guide['operating_rules']}"
        )

    # tool_groups partition the tool list — each registered tool
    # should appear in exactly one group (so the guide really is a
    # map). Skip the registry assertion here (covered by
    # test_tool_schemas.py) and just verify the buckets exist.
    for bucket in ("build_new", "convert_existing", "edit_tabs",
                   "read", "drive_management", "setup_and_auth",
                   "introspection"):
        assert bucket in guide["tool_groups"], (
            f"tool_groups missing {bucket}"
        )
        assert guide["tool_groups"][bucket], (
            f"tool_groups[{bucket}] is empty — should list its tools"
        )
