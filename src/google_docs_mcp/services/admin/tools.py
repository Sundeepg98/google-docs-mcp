"""Admin / introspection / auth MCP tool registrations (Gap #7 — v2.2.2).

This module defines the ``@workspace_tool``-decorated tool functions
for the 7 admin-service tools previously in ``server.py``. Importing
this module triggers registration with the live ``mcp`` instance —
``server.py`` performs the import at the bottom of its module, AFTER
constructing ``mcp`` and AFTER ``decorators.register(mcp, ...)`` wires
the ``@workspace_tool`` decorator.

**Tools registered here** (7 admin-service tools):

1. ``gdocs_server_info``           — server identity + tool inventory + CI status
2. ``gdocs_test_manifest``         — full test inventory + per-test outcomes
3. ``gdocs_guide``                 — orientation as a structured payload
4. ``gdocs_help``                  — error-message recovery guidance
5. ``gdocs_get_signed_upload_url`` — mint one-shot signed upload URL
6. ``gdocs_reset_authorization``   — clear stored OAuth credentials
7. ``gdocs_admin_audit``           — forensic timeline (admin-token gated)

**Several tools use ``creds=False``** (decorator's standard credentials
injection is the wrong shape for each):

- ``gdocs_server_info`` / ``gdocs_test_manifest`` / ``gdocs_guide`` /
  ``gdocs_help`` — no Google API call (local introspection / lookup).
- ``gdocs_get_signed_upload_url`` — mints HMAC URL via ``keys.get_key``;
  handles its own ``current_user_id_or_none()`` check.
- ``gdocs_reset_authorization`` — DELETES creds (inverse of normal auth
  path); pre-fetching creds would break the reset for users whose creds
  are already broken.
- ``gdocs_admin_audit`` — gated by ``MCP_ADMIN_TOKEN`` (not user OAuth);
  reads ``user_store`` directly.

**Import discipline.** Imports the 2 shared helpers
(``_get_credentials``, ``_format_http_error``) directly from
``_tool_helpers`` per the M3 Phase C 3-consumer extraction trigger.
The decorator itself (``workspace_tool``) still lives in ``server.py``
because it's bound to the live ``mcp`` instance.

**Admin-domain helpers** moved here alongside the tools:

- ``_find_test_results_path`` / ``_canonical_digest`` /
  ``_read_test_suite_status`` / ``_read_mutation_check`` —
  CI-artifact reading + tamper detection.
- ``_check_admin_token`` + the ``_ADMIN_AUDIT_*`` constants — admin-
  token gating for ``gdocs_admin_audit``.
- ``_log`` — module-local logger.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from pathlib import Path

from fastmcp.exceptions import ToolError

from google_docs_mcp.auth import default_data_dir
from google_docs_mcp.credentials import current_user_id_or_none
from google_docs_mcp.crypto import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    sign_upload_url,
)
from google_docs_mcp.keys import (
    get_first_call_timestamps,
    get_key,
    get_shim_hit_counters,
    get_total_call_counters,
)
from google_docs_mcp.server import mcp, workspace_tool
from google_docs_mcp.tool_schemas import (
    GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
    GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    GDOCS_GUIDE_OUTPUT_SCHEMA,
    GDOCS_HELP_OUTPUT_SCHEMA,
    GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
)

_log = logging.getLogger("google_docs_mcp.services.admin")


# ---------------------------------------------------------------------
# CI-artifact reading + tamper detection
# (used by gdocs_server_info + gdocs_test_manifest)
# ---------------------------------------------------------------------


def _find_test_results_path() -> Path | None:
    """Locate the test-results.json artifact.

    Container path first (/app/test-results.json, populated by
    Dockerfile COPY), then CWD as local-dev fallback. Evaluated at
    call time — NOT at import — so monkeypatched cwds in tests work.
    """
    candidates = [
        Path("/app/test-results.json"),
        Path.cwd() / "test-results.json",
    ]
    return next((p for p in candidates if p.exists()), None)


def _canonical_digest(data: dict) -> str:
    """SHA-256 of the JSON with ``_meta`` removed, sorted-key serialized.

    The digest is computed over everything EXCEPT the ``_meta`` block
    (because the digest itself lives inside _meta — chicken/egg).
    Canonicalization (sort_keys + tight separators) gives a stable
    hash regardless of Python's dict-iteration order.
    """
    import hashlib
    import json as _json
    payload = {k: v for k, v in data.items() if k != "_meta"}
    canon = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _read_test_suite_status(deployed_commit: str) -> dict:
    """Surface the CI test-suite status baked into the build.

    deploy.sh writes ``test-results.json`` via pytest-json-report,
    embeds ``_git_commit`` + ``_ci_run_url`` + ``_meta.digest``, and
    the Dockerfile COPIes it into the image. If the file's absent or
    unparseable (vanilla `docker build` skips it; SKIP_TESTS writes a
    stub), return ``{"status": "unknown"}`` per the documented
    contract.

    **Tamper detection.** At read time we re-canonicalize the JSON
    (minus ``_meta``) and compare the recomputed digest against the
    stored one. If they diverge, somebody edited the file
    post-build — return ``status: "tampered"`` so a caller can
    distinguish "the suite passed but someone fiddled with the
    numbers" from a legitimate pass.

    ``test_suite.commit`` should equal the running build's
    ``git_commit``; divergence means the image shipped without a
    matching test run — itself a red flag worth surfacing.
    """
    import json
    from datetime import datetime, timezone

    # mutation_check is independent state (separate artifact), so it
    # gets attached to whatever we return — even the unknown branches.
    # Callers can rely on the field always being present.
    mutation_check = _read_mutation_check()

    path = _find_test_results_path()
    if path is None:
        return {"status": "unknown", "mutation_check": mutation_check}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "mutation_check": mutation_check}

    summary = data.get("summary") or {}
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", 0))
    skipped = int(summary.get("skipped", 0))

    # pytest-json-report's "created" is a unix timestamp; convert to
    # ISO 8601 UTC. SKIP_TESTS stub doesn't include "created" — fall
    # back to "unknown".
    created_ts = data.get("created")
    if isinstance(created_ts, (int, float)):
        last_run = datetime.fromtimestamp(
            created_ts, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
    else:
        last_run = "unknown"

    # Test-suite commit + CI run URL written by deploy.sh.
    test_commit = data.get("_git_commit", "unknown")
    ci_run_url = data.get("_ci_run_url", "")

    # Report digest verification — tamper detection.
    stored_meta = data.get("_meta") or {}
    stored_digest = stored_meta.get("digest", "")
    recomputed_digest = _canonical_digest(data)
    digest_matches = bool(stored_digest) and stored_digest == recomputed_digest

    # Status logic: must have a populated summary AND zero failures
    # AND the digest must verify. SKIP_TESTS stub has empty summary
    # → status="unknown" naturally. Mismatched digest → "tampered"
    # even if the numbers look green.
    if not summary:
        status = "unknown"
    elif stored_digest and not digest_matches:
        status = "tampered"
    elif failed == 0 and passed > 0:
        status = "passed"
    else:
        status = "failed"

    return {
        "last_run": last_run,
        "commit": test_commit,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "status": status,
        "ci_run_url": ci_run_url,
        "report_digest": stored_digest,
        "mutation_check": mutation_check,
    }


def _read_mutation_check() -> dict:
    """Surface mutation-test results baked into the build.

    Reads /app/mutation-check.json (CWD fallback for local dev),
    produced by scripts/mutation_check.py in CI. Summarizes to
    {ran, caught, status, asleep_guards, stale_patches,
    imprecise_patches}. Missing file → unknown.

    Failure modes the gate distinguishes (v1.2.2+):
      asleep_guards     — patch applied but the named guard didn't
                          notice the bug (test rot).
      stale_patches     — patch's `find` text is gone, or applied
                          without tripping anything (mutation rot).
      imprecise_patches — patch broke the target AND unrelated tests
                          (over-broad mutation).

    Status "passed" only when caught == ran AND all three buckets are
    empty. Pre-1.2.2 artifacts (no stale/imprecise fields) default
    the new fields to [] for back-compat.
    """
    import json

    candidates = [
        Path("/app/mutation-check.json"),
        Path.cwd() / "mutation-check.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return {"status": "unknown", "ran": 0, "caught": 0}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "ran": 0, "caught": 0}

    return {
        "ran": int(data.get("ran", 0)),
        "caught": int(data.get("caught", 0)),
        "status": data.get("status", "unknown"),
        "asleep_guards": list(data.get("asleep_guards", [])),
        "stale_patches": list(data.get("stale_patches", [])),
        "imprecise_patches": list(data.get("imprecise_patches", [])),
    }


# ---------------------------------------------------------------------
# Admin-token gating (used by gdocs_admin_audit)
# ---------------------------------------------------------------------


_ADMIN_AUDIT_MIN_HOURS = 1
_ADMIN_AUDIT_MAX_HOURS = 168  # 1 week


def _check_admin_token(provided: object) -> None:
    """Gate admin-only tool calls. Raises ToolError on any failure mode.

    Three failure modes, each with a distinct message so the operator
    can tell them apart in a 500 trace without ambiguous "auth failed":

    - env unset → admin surface is disabled at the server
    - arg not a string → caller signature error
    - arg != env → wrong token (uses ``hmac.compare_digest`` so a
      timing-side-channel attacker can't probe the env value by
      measuring response latency).

    Read the env at CALL time, not module-load time, so an operator
    can rotate ``MCP_ADMIN_TOKEN`` via ``fly secrets set`` and have it
    take effect without a server restart.
    """
    expected = os.environ.get("MCP_ADMIN_TOKEN")
    if not expected:
        raise ToolError(
            "admin disabled; set MCP_ADMIN_TOKEN env var on the server "
            "to enable gdocs_admin_audit."
        )
    if not isinstance(provided, str):
        raise ToolError(
            "admin_token must be a string"
        )
    # compare_digest expects equal-length operands; pad the shorter
    # side rather than short-circuiting on length, so the timing
    # signal doesn't leak the env value's length either.
    if not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8"),
    ):
        raise ToolError("admin_token does not match MCP_ADMIN_TOKEN")


# ---------------------------------------------------------------------
# 1. gdocs_server_info
# ---------------------------------------------------------------------


@workspace_tool(
    title="Server identity + tool inventory",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
)
async def gdocs_server_info() -> dict:
    """Server identity + full tool inventory — for change detection across sessions.

    USE WHEN: you want to confirm what version of the MCP you're
    talking to, detect renames/additions/removals between sessions,
    or verify a redeploy actually rolled out.

    The ``tools`` list is the COMPLETE registered tool inventory
    direct from the server's own registry — not filtered or summarized.
    Counting it and diffing across sessions is the canonical way for a
    caller to detect drift between what their cache thinks the server
    has and what it actually has.

    Returns:
        ``{"version", "build_time", "git_commit", "tool_count",
        "tools": [...]}``.
        ``build_time`` and ``git_commit`` are baked in at Docker build
        time via --build-arg; if the deploy script didn't pass them
        they show as ``"unknown"``.

    Choreography: typical introspection trio — pair with
    ``gdocs_guide()`` (workflows + rules + tool groupings) and
    ``gdocs_test_manifest()`` (full per-test inventory). Cheap; no
    Google API call required.
    """
    # FastMCP's tool registry is async-accessed via list_tools().
    # Making this whole tool async lets us await it directly without
    # nested-event-loop gymnastics.
    try:
        tools = await mcp.list_tools()
        tool_names = sorted(t.name for t in tools)
    except Exception:  # noqa: BLE001
        tool_names = []

    # Read version via importlib.metadata to avoid the circular-import
    # trap (__init__.py imports server.main, so server can't import
    # __version__ from the partially-loaded package at module-load
    # time). Reading from installed package metadata is also more
    # honest — it reflects the wheel that's actually deployed.
    from importlib.metadata import version as _pkg_version
    try:
        ver = _pkg_version("google-docs-mcp")
    except Exception:  # noqa: BLE001
        ver = "unknown"

    # Append GIT_COMMIT as semver build metadata so every deploy from
    # a distinct commit reports a unique version string — without
    # requiring a manual pyproject bump on every hot-fix. Format
    # follows semver §10: `version+buildmetadata`. PEP 440 also
    # tolerates `+local` segments for the same purpose.
    git_commit = os.environ.get("GIT_COMMIT", "unknown")
    if git_commit and git_commit != "unknown":
        ver = f"{ver}+{git_commit}"

    return {
        "version": ver,
        "build_time": os.environ.get("BUILD_TIME", "unknown"),
        "git_commit": git_commit,
        "tool_count": len(tool_names),
        "tools": tool_names,
        "test_suite": _read_test_suite_status(git_commit),
        # v1.5: per-purpose shim-path hit counters so operators can
        # soak-test (deploy v1.5, wait 3+ days, check that this stays
        # at zero for the trailing 24h) before shipping v2.0b's
        # strict-flip, which would invalidate any key minted via the
        # shim. Process-local counter — aggregate across replicas at
        # read time.
        "key_back_compat_shim_active_hits": get_shim_hit_counters(),
        # v1.5.1 (#28): denominator for the shim-hit telemetry above.
        # Counts every successful get_key() call regardless of which
        # path served the key. Preflight asserts shim==0 AND total>=N
        # so "0 shim hits" can't mean "0 calls". Process-local; same
        # aggregation caveat as the shim counter.
        "key_call_totals": get_total_call_counters(),
        # v2.6 (#48): observability for the soak-window gate. The
        # preflight script demands BOTH shim_hits==0 AND total>=N AND
        # first_call_age_seconds >= 1h30 (i.e. real traffic has flowed
        # since boot) before declaring it safe to ship v2.0b's strict-
        # flip. ``first_call_age_seconds[purpose]`` is None if that
        # purpose has never been requested in this process (e.g.
        # api_bearer on a server that has only served signed URLs).
        # Process-local; aggregate across replicas at read time.
        "key_observability": {
            "first_call_age_seconds": {
                purpose: (time.time() - ts) if ts is not None else None
                for purpose, ts in get_first_call_timestamps().items()
            },
        },
    }


# ---------------------------------------------------------------------
# 2. gdocs_test_manifest
# ---------------------------------------------------------------------


@workspace_tool(
    title="List CI test manifest",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
)
def gdocs_test_manifest() -> dict:
    """List every test in the CI artifact + its pass/fail outcome.

    Read / verify / audit / inspect / list the test inventory of the
    running build. Use to: confirm specific named regression guards
    (e.g. test_owned_by_app_consistency) actually exist and passed,
    spot-check what "203 passed" means, find which test failed if
    test_suite.status is not "passed".

    Returned shape:
        {
          status: "ok" | "unknown" | "tampered",
          total: int,
          tests: [{nodeid: str, outcome: "passed"|"failed"|"skipped"}, ...],
          named_regression_guards: {
            present: [list of named-guard test ids found in the suite],
            missing: [list of named guards NOT found — should be empty],
          },
        }

    Status "unknown" when the artifact's missing/unparseable;
    "tampered" when the report_digest doesn't match the canonicalized
    payload (same logic as gdocs_server_info.test_suite); "ok"
    otherwise.

    Choreography: pairs with ``gdocs_server_info.test_suite``. The
    summary is in server_info; this tool gives the full per-test
    breakdown. No Google API call.
    """
    import json

    path = _find_test_results_path()
    if path is None:
        return {
            "status": "unknown",
            "reason": "test-results.json not found in container",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "reason": "test-results.json unparseable",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    stored_digest = (data.get("_meta") or {}).get("digest", "")
    digest_matches = stored_digest and stored_digest == _canonical_digest(data)
    if stored_digest and not digest_matches:
        return {
            "status": "tampered",
            "reason": "report_digest mismatch — file was edited after CI",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    tests_raw = data.get("tests") or []
    tests = [
        {"nodeid": t.get("nodeid", ""), "outcome": t.get("outcome", "")}
        for t in tests_raw
    ]

    # The 8 named regression guards from v1.1.x — see CHANGELOG and
    # tests/unit/test_*.py docstrings. If any are missing the suite's
    # coverage of cycle bugs has regressed.
    REQUIRED_GUARDS = [
        "test_owned_by_app_agrees_with_trash_outcome",
        "test_trash_file_id_accepts_str_or_list",
        "test_inject_matches_fragmented_runs",
        "test_deploy_webapp_body_does_not_include_entryPoints",
        "test_preview_flags_what_convert_truncates",
        "test_auth_pkce_consistency_every_url",
        "test_tool_descriptions_truthful",
        "test_tool_discoverability_via_server_info",
    ]
    test_names = {t["nodeid"].split("::")[-1].split("[")[0] for t in tests}
    present = [g for g in REQUIRED_GUARDS if g in test_names]
    missing = [g for g in REQUIRED_GUARDS if g not in test_names]

    return {
        "status": "ok",
        "total": len(tests),
        "tests": tests,
        "named_regression_guards": {"present": present, "missing": missing},
    }


# ---------------------------------------------------------------------
# 3. gdocs_guide
# ---------------------------------------------------------------------


@workspace_tool(
    title="Orientation guide (local, no API)",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_GUIDE_OUTPUT_SCHEMA,
)
def gdocs_guide() -> dict:
    """Orientation payload — the "start here" / --help for this server.

    Returns the same content as the connect-time server ``instructions``
    string, as a structured dict so it is machine-readable and always
    callable. Use when:

    - the client truncated or ignored connect-time instructions
    - you want machine-readable workflow choreography / tool groupings
    - you need to confirm which tools belong to which workflow before
      sequencing a multi-tool plan

    No arguments. No side effects. Cheap (no API calls). Typically the
    first call an agent makes after connecting — pairs naturally with
    ``gdocs_server_info()`` (version + verified CI test status).

    Returned shape:
        {
          server: {name, version, what_it_does, all_tools_prefixed,
                   more_info},
          workflows: [{name, goal, tool_sequence, notes}, ...],
          operating_rules: [str, ...],
          tool_groups: {build_new: [...], convert_existing: [...],
                        edit_tabs: [...], read: [...],
                        drive_management: [...], setup_and_auth: [...],
                        introspection: [...]},
        }
    """
    from google_docs_mcp import __version__

    return {
        "server": {
            "name": "google-docs-fly",
            "version": __version__,
            "what_it_does": (
                "Create, edit, read, and manage Google Docs with native "
                "sidebar Tabs (October 2024+ feature)."
            ),
            "all_tools_prefixed": "gdocs_",
            "more_info": (
                "Call gdocs_server_info for build version + verified CI "
                "test status (digest, ci_run_url, mutation_check)."
            ),
        },
        "workflows": [
            {
                "name": "new_doc",
                "goal": "Build a tabbed doc from content composed in chat",
                "tool_sequence": ["gdocs_make_tabbed_doc"],
                "notes": (
                    "ONE call. No file. No upload. DEFAULT for any 'make "
                    "me a doc with sections X, Y, Z' request."
                ),
            },
            {
                "name": "convert_doc_with_headings",
                "goal": (
                    "Convert an existing Drive doc that already has "
                    "Heading 1 paragraphs into tabs"
                ),
                "tool_sequence": [
                    "gdocs_preview_tab_split",
                    "gdocs_tab_existing_doc",
                    "gdocs_get_doc_outline",
                ],
                "notes": (
                    "Preview first to validate the split; convert; then "
                    "outline to verify the result. Conversion is one-way."
                ),
            },
            {
                "name": "retrofit_styled_doc",
                "goal": (
                    "Retrofit a styled doc that has NO Heading 1 "
                    "paragraphs (e.g. banners inside styled tables)"
                ),
                "tool_sequence": [
                    "gdocs_tab_existing_doc(markers=[...])",
                ],
                "notes": (
                    "Same tool as convert_doc_with_headings; passing "
                    "`markers` triggers retrofit mode (injects synthetic "
                    "H1s before each marker block, then converts). NEVER "
                    "rebuild a styled .docx from text — formatting would "
                    "be lost."
                ),
            },
            {
                "name": "convert_sandbox_docx",
                "goal": (
                    "Convert a .docx that exists only as bytes in the "
                    "caller's sandbox (cloud chat scenario)"
                ),
                "tool_sequence": [
                    "gdocs_get_signed_upload_url",
                    "POST {url}",
                ],
                "notes": (
                    "`docx_path` does NOT work from cloud chat — the "
                    "server cannot see the caller's filesystem. The POST "
                    "is equivalent to gdocs_tab_existing_doc; use this "
                    "route when the .docx is in your sandbox."
                ),
            },
            {
                "name": "cleanup",
                "goal": "Trash / restore Drive files this app created",
                "tool_sequence": [
                    "gdocs_trash_file",
                    "gdocs_untrash_file",
                ],
                "notes": (
                    "ONLY acts on files this app created; others return "
                    "app_not_authorized. file_id accepts a string or "
                    "list (batch)."
                ),
            },
        ],
        "operating_rules": [
            (
                "Never rebuild a styled .docx from text. Use retrofit "
                "(workflow `retrofit_styled_doc`) to preserve formatting."
            ),
            (
                "`docx_path` arguments do NOT work from cloud chat — the "
                "server cannot see the caller's filesystem. Use "
                "signed-URL upload (workflow `convert_sandbox_docx`) or "
                "drive_file_id."
            ),
            (
                "`placeholder_behavior='rename'` preserves a title / "
                "index page; the default 'remove' deletes it. Use "
                "'rename' when the source has a meaningful cover page."
            ),
            (
                "Trash tools only act on files THIS app created. Drive "
                "returns appNotAuthorizedToFile (403) on others; the "
                "file belongs to its owner and only they can trash it."
            ),
            (
                "First use requires interactive Google OAuth consent. "
                "The client must open the consent URL in a browser — "
                "this cannot be automated. Subsequent calls reuse the "
                "cached token until it expires."
            ),
        ],
        "tool_groups": {
            "build_new": ["gdocs_make_tabbed_doc"],
            "convert_existing": [
                "gdocs_preview_tab_split",
                "gdocs_tab_existing_doc",
                "gdocs_get_signed_upload_url",
            ],
            "edit_tabs": [
                "gdocs_rename_tab",
                "gdocs_delete_tab",
                "gdocs_set_tab_icons",
                "gdocs_replace_all_text",
                "gdocs_add_tabs",
                "gdocs_append_to_tab",
            ],
            "read": [
                "gdocs_get_doc_outline",
                "gdocs_read_doc",
                "gdocs_get_tab_url",
            ],
            "drive_management": [
                "gdocs_find_doc_by_title",
                "gdocs_move_to_folder",
                "gdocs_trash_file",
                "gdocs_untrash_file",
            ],
            "setup_and_auth": [
                # PR-α canonical name. The deprecation alias
                # gdocs_setup_apps_script is still registered for
                # backward compatibility but is intentionally omitted
                # from the user-facing groups so the orientation
                # surface stays clean.
                "gdocs_install_automation",
                "gdocs_reset_authorization",
            ],
            "introspection": [
                "gdocs_server_info",
                "gdocs_test_manifest",
                "gdocs_guide",
            ],
        },
    }


# ---------------------------------------------------------------------
# 4. gdocs_help
# ---------------------------------------------------------------------


@workspace_tool(
    title="Help for an error message (local, no API)",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_HELP_OUTPUT_SCHEMA,
)
def gdocs_help(error_message: str) -> dict:
    """Look up recovery guidance for a server error string.

    USE WHEN: a previous gdocs_* tool call returned an error / warning
    payload and you (the LLM) are not sure how to proceed. Pass the
    raw error text and gdocs_help returns the structured recovery
    entry (what to do, what to tell the user, whether to retry, etc.).

    Pure lookup. No Google API calls. No OAuth required. Cheap to
    call as a debugging / recovery shortcut. Backed by the same
    table exposed at the MCP resource ``gdocs://error-recovery``
    and documented in ``docs/LLM_RECOVERY.md``.

    Args:
        error_message: The error string / warning text you want to
            decode. Substring-matched (case-INsensitive — both sides
            lowercased before comparison) against every registered
            pattern; first hit wins. Pass the raw error verbatim
            (JSON, Python repr, ToolError body — all work).

    Returns:
        On match::

            {
              "matched": true,
              "matched_pattern": "<the pattern that hit>",
              "key": "<recovery_key>",
              "pattern": "<same as matched_pattern>",
              "severity": "info" | "warning" | "error",
              "retriable": bool,
              "wait_seconds": int | null,
              "do": "<imperative recovery action>",
              "user_message": "<what to tell the user>",
              "related_tool": "<gdocs_xxx>" | null,
              "planned": bool  # True = aspirational entry, no live emitter
            }

        On miss::

            {
              "matched": false,
              "available_patterns": [<all registered patterns>],
              "suggestion": "<hint to use server_info or file an issue>"
            }

    Choreography: typically called RIGHT AFTER a failing tool call,
    before deciding whether to retry, surface to the user, or pivot
    to a different tool. Pairs with gdocs_server_info() when filing
    bug reports for the unexpected_exception case.
    """
    # Lazy-import the recovery table to avoid forcing resources.py
    # load at this module's import time. resources.py registers
    # gdocs://error-recovery MCP resources as a side-effect of import;
    # server.py already triggers that import at module bottom, so by
    # the time this tool is *called* the table is populated.
    from google_docs_mcp.resources import _RECOVERY_TABLE

    # Case-insensitive substring match — LLMs sometimes lowercase /
    # normalize the error text before passing it back. errors.py:69
    # also lowercases its details_str before its own substring search,
    # so case-insensitive here keeps gdocs_help symmetric with the
    # rest of the error-handling code. Both pattern AND haystack are
    # lowercased before comparison; the on-wire ``matched_pattern``
    # / ``pattern`` fields still report the canonical case.
    error_lower = error_message.lower()
    for key, entry in _RECOVERY_TABLE.items():
        if entry["pattern"].lower() in error_lower:
            return {
                "matched": True,
                "matched_pattern": entry["pattern"],
                "key": key,
                "pattern": entry["pattern"],
                "severity": entry["severity"],
                "retriable": entry["retriable"],
                "wait_seconds": entry.get("wait_seconds"),
                "do": entry["do"],
                "user_message": entry["user_message"],
                "related_tool": entry.get("related_tool"),
                "planned": entry.get("planned", False),
            }

    return {
        "matched": False,
        "available_patterns": [
            e["pattern"] for e in _RECOVERY_TABLE.values()
        ],
        "suggestion": (
            "No registered recovery pattern matched the error text "
            "(matching is case-insensitive substring). Fetch the "
            "resource gdocs://error-recovery for the full table, "
            "call gdocs_server_info() to capture version + commit, "
            "and consider filing an issue at the project repo with "
            "the raw error string so a new entry can be added."
        ),
    }


# ---------------------------------------------------------------------
# 5. gdocs_get_signed_upload_url
# ---------------------------------------------------------------------


@workspace_tool(
    title="Mint a one-shot signed upload URL",
    service="admin",
    readonly=False, destructive=False, idempotent=False, external=True,
    # creds=False: this tool mints an HMAC URL; no Google API call here.
    # It handles its own user_id check via current_user_id_or_none().
    output_schema=GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
)
def gdocs_get_signed_upload_url(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = 50 * 1024 * 1024,
) -> dict:
    """Mint a signed URL ONLY for uploading an existing .docx file's bytes.

    USE WHEN: you genuinely have an existing .docx file in your Python
    sandbox (e.g. one a user uploaded, one a pipeline produced) and
    need to POST its raw bytes to /api/convert from cloud chat. The
    signed URL is the credential — no Authorization header needed.

    DO NOT USE when:
    - You are composing new content from text. Use ``gdocs_make_tabbed_doc``
      — it takes markdown directly and skips this upload dance entirely.
      Building a .docx in the sandbox just to upload it here is pointless
      extra work.
    - The .docx already lives on Drive. Use
      ``gdocs_tab_existing_doc(drive_file_id=...)`` instead.

    The URL is single-use (the server tracks consumed nonces) and
    expires after ``ttl_seconds`` (default 10 min, max 1 hour).

    **v2.1 — bound to your user identity.** The URL embeds the calling
    user's Google ``sub`` claim, so the /api/convert request lands in
    YOUR Drive (using YOUR Apps Script Web App), not the operator's.
    Stdio callers (no FastMCP auth context) cannot mint signed URLs —
    they have direct tool access and don't need the REST detour.

    Args:
        ttl_seconds: How long the URL stays valid. Default 600s; keep
            short to limit blast radius if the URL leaks into a chat
            transcript.
        max_bytes: Advisory upload size cap baked into the signature.
            Defaults to 50 MB (Drive's converter ceiling).

    Returns:
        ``{"url", "expires_at", "max_bytes", "nonce", "user_id",
        "usage_hint"}``. ``usage_hint`` is a one-line Python snippet
        showing how to use the URL — the model copies it into the
        sandbox.

    Choreography: this is the FIRST step of the `convert_sandbox_docx`
    workflow. Mint the URL here, then POST the .docx bytes to that URL
    from the sandbox. The POST is equivalent to
    ``gdocs_tab_existing_doc`` — use this route when the .docx lives
    only as bytes in the sandbox rather than on Drive.

    NOTE: ``docx_path`` arguments on other tools do NOT work from
    cloud chat (server can't see the caller's filesystem); this
    signed-URL upload flow is the sandbox-bytes path.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "https://sundeepg98-docs-mcp.fly.dev")
    # v2.6 (#48): purpose-routed via keys.get_key("signed_url") so the
    # v2.0b strict-flip activates HKDF-derivation for signed-URL HMACs
    # without further edits here. get_key raises RuntimeError on missing
    # MCP_BEARER_TOKEN; translate to ToolError so the surface stays
    # user-facing (Markdown-renderable in claude.ai's connector UI).
    # v2.0b: keys.get_key() returns bytes; pass through to sign_upload_url
    # without the pre-flip .decode("utf-8") (which crashed on HKDF
    # output that isn't UTF-8 in general).
    try:
        signing_key = get_key("signed_url")
    except RuntimeError as e:
        raise ToolError(
            "MCP_BEARER_TOKEN env var not set on the server — "
            "signed URLs require it as the HMAC key."
        ) from e
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ToolError(
            f"ttl_seconds must be 1..{MAX_TTL_SECONDS}, got {ttl_seconds}"
        )

    # v2.1: every signed URL is bound to the calling user. Without a
    # FastMCP auth context we have no user — stdio callers don't need
    # /api/convert at all (they have direct tool access), so refuse
    # rather than mint an operator-scoped URL that would write into
    # the wrong Drive.
    user_id = current_user_id_or_none()
    if user_id is None:
        raise ToolError(
            "gdocs_get_signed_upload_url requires an authenticated MCP "
            "session (cloud / HTTP mode). Stdio callers should pass "
            "docx_path directly to gdocs_tab_existing_doc instead."
        )

    minted = sign_upload_url(
        base_url=f"{base}/api/convert",
        signing_key=signing_key,
        user_id=user_id,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
    )
    minted["usage_hint"] = (
        "requests.post(URL, files={'file': ('doc.docx', open('/path/to/doc.docx','rb'), "
        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document')}, "
        "data={'split_by': 'heading_1', 'icons_by_title': '<json-string>'})"
    )
    return minted


# ---------------------------------------------------------------------
# 6. gdocs_reset_authorization
# ---------------------------------------------------------------------


@workspace_tool(
    title="Reset user authorization / revoke tokens",
    service="admin",
    readonly=False, destructive=True, idempotent=True, external=True,
    # creds=False: this tool DELETES creds (it's the inverse of the
    # usual auth path). Wrapping with the standard creds injection
    # would try to fetch creds first, breaking the user's reset path
    # when their creds are already broken.
    output_schema=GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
)
def gdocs_reset_authorization(full: bool = False) -> dict:
    """Reset / revoke / clear stored Google OAuth credentials. Force re-consent.

    Use this tool to: sign out, re-authorize, re-consent after a scope
    change, switch Google accounts, recover from a stale or revoked
    grant, force a fresh OAuth flow for testing (PKCE / consent
    screen), or roll back to the needs_authorization state. Equivalent
    in spirit to "log out and log back in" for the Google Drive / Docs
    / Apps Script API access this server uses on your behalf.

    USE WHEN: you want to force a fresh OAuth consent flow on the next
    call — for testing PKCE / re-consenting after a scope change /
    recovering from a stale or revoked grant / switching the Google
    account this server acts as.

    HTTP mode (cloud chat, claude.ai connector):
      - Default (``full=False``): clears only the stored Google
        credentials (``google_creds_json``). The user's Apps Script
        Web App setup (URL, script_id, deployment_id) is preserved.
        Next tool call that needs creds returns
        ``status: "needs_authorization"`` with a fresh auth_url.
      - ``full=True``: clears the entire user_store row, including
        the Apps Script setup. Next call to ``gdocs_setup_apps_script``
        will create a NEW project in Drive.

    Stdio mode (Claude Desktop / Code on a developer laptop):
      - Default: deletes the cached OAuth token at
        ``~/.google-docs-mcp/token.json``. Next tool call triggers
        the local browser-consent flow.
      - ``full=True``: also deletes the local Apps Script
        ``setup-state.json`` ledger and the URL in ``config.json`` —
        next ``setup-apps-script`` CLI run will create a new project.

    DOES NOT trash any Apps Script projects in your Drive — those
    remain (you can manually delete them in Drive if you want to
    free up space). Just clears the local/server-side record of the
    authorization.

    Args:
        full: If True, also clear Apps Script setup state, not just
            credentials. Default False (least destructive).

    Returns:
        ``{status: "reset", message: str, cleared: [list of what
        was cleared]}``.

    Choreography: after reset, the very next tool call that needs
    creds will return ``needs_authorization`` with a fresh consent
    URL. Re-running ``gdocs_setup_apps_script`` afterwards is
    typical if you also passed ``full=True``.
    """
    user_id = current_user_id_or_none()
    cleared: list[str] = []

    if user_id is not None:
        # HTTP / multi-tenant mode
        from google_docs_mcp import user_store
        if full:
            user_store.clear_state(user_id)
            cleared.append("user_store row (creds + apps_script_*)")
        else:
            # Only nuke google_creds_json; preserve apps_script_*
            user_store.save_state(user_id, {"google_creds_json": None})
            cleared.append("google_creds_json")
        return {
            "status": "reset",
            "message": (
                "Authorization cleared for your account. The next tool "
                "call that needs Google API access will return "
                "'needs_authorization' with a fresh auth URL — click it "
                "to re-consent."
            ),
            "cleared": cleared,
        }

    # Stdio / no-auth-context mode
    data_dir = default_data_dir()
    token_file = data_dir / "token.json"
    if token_file.exists():
        token_file.unlink()
        cleared.append(str(token_file))
    if full:
        setup_state_file = data_dir / "setup-state.json"
        if setup_state_file.exists():
            setup_state_file.unlink()
            cleared.append(str(setup_state_file))
        cfg_file = data_dir / "config.json"
        if cfg_file.exists():
            cfg_file.unlink()
            cleared.append(str(cfg_file))

    # Bust the module-level creds cache so the next tool call doesn't
    # return the in-memory token that we just deleted from disk.
    # M3 Phase C (v2.1.5): the cache moved with _get_credentials to
    # _tool_helpers.py; reset via module attribute since `global`
    # only declares names from THIS module's scope.
    from google_docs_mcp import _tool_helpers
    _tool_helpers._creds_cache = None

    return {
        "status": "reset",
        "message": (
            "Local OAuth token cleared. The next tool call will trigger "
            "the local browser-consent flow."
            + (" Apps Script setup state also cleared." if full else "")
        ),
        "cleared": cleared,
    }


# ---------------------------------------------------------------------
# 7. gdocs_admin_audit
# ---------------------------------------------------------------------
#
# Operator-facing primitive added per R29-B's pressure-test finding: a
# customer-reported cross-tenant data leak was unresolvable because no
# per-user audit log existed. The tool returns the user_state row's
# timestamp bounds (the only audit-grade signal currently persisted) so
# the operator can correlate against flyctl logs without asking the
# customer for more info.
#
# Gated by ``MCP_ADMIN_TOKEN`` env var (separate from MCP_BEARER_TOKEN —
# admin auth MUST NOT share the surface that talks to claude.ai's
# connector framework). If the env is unset the tool registers but
# refuses to run, so operators see one consistent error path whether
# they forgot to set the env or supplied a wrong token.
#
# Honest limits surfaced via the ``notes`` field — user_state.db tracks
# per-row updated_at, not per-operation. A v2.x audit-log table would
# upgrade this; for now this is the best we can do server-side without
# bouncing back to the customer. Documented in RUNBOOK §2.8.


@workspace_tool(
    title="Admin: query user_state forensic timeline (admin-token gated)",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    # creds=False: this tool reads user_store SQLite ledger directly,
    # gated by an admin token (not user OAuth). No Google API call.
    output_schema=GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
)
def gdocs_admin_audit(
    admin_token: str, user_id: str, since_hours: int = 24,
) -> dict:
    """Return server-side state for ``user_id`` within a time window — admin only.

    USE WHEN: a customer reports a cross-tenant data leak or other
    operation-specific incident, and you need to correlate server-side
    state against their account. Operator-facing forensic primitive;
    NOT for LLM tool routing in a normal conversation.

    Requires ``MCP_ADMIN_TOKEN`` env var set on the server AND the
    ``admin_token`` arg matching it (constant-time comparison). If
    the env is unset, the tool registers but always errors — so the
    admin surface is OFF by default in dev/test environments.

    Args:
        admin_token: Must equal the server's ``MCP_ADMIN_TOKEN`` env
            var. Separate token from ``MCP_BEARER_TOKEN`` on purpose
            (admin auth must not share the surface that talks to
            claude.ai's connector framework).
        user_id: The Google ``sub`` claim (or email fallback) of the
            user under investigation. Truncated to first 8 chars in
            any server-side logs to avoid PII leakage into logs that
            may be shipped to third-party log aggregators.
        since_hours: Audit window size, 1-168 (1 hour to 1 week).
            Defaults to 24h. Validated to that range — wider windows
            would return huge responses; narrower is rounding noise.

    Returns::

        {
          "user_id_prefix": "<first 8 chars>",
          "window_hours": <since_hours>,
          "total_entries": 0 | 1,
          "entries": [
            {
              "timestamp": <unix epoch seconds of updated_at>,
              "operation_type": "user_state_updated",
              "doc_id": null,           # not tracked at this granularity
              "success": true,
            }
          ],
          "notes": "user_state.db tracks ..."
        }

    Honest limits: user_state.db tracks ``created_at`` / ``updated_at``
    per user row — NOT per Google API call. This means the tool can
    answer "did this user's session touch the server in the last N
    hours?" but not "what specific docs were created?". For finer
    granularity the operator must also grep flyctl logs by the
    ``user_id_prefix`` value returned here. A v2.x audit-log table
    would close this gap; tracked in #25.
    """
    _check_admin_token(admin_token)

    if not isinstance(user_id, str) or not user_id:
        raise ToolError("user_id must be a non-empty string")

    if (
        not isinstance(since_hours, int)
        or isinstance(since_hours, bool)  # True/False are ints in Python
        or since_hours < _ADMIN_AUDIT_MIN_HOURS
        or since_hours > _ADMIN_AUDIT_MAX_HOURS
    ):
        raise ToolError(
            f"since_hours must be an int in "
            f"[{_ADMIN_AUDIT_MIN_HOURS}, {_ADMIN_AUDIT_MAX_HOURS}] "
            f"(got {since_hours!r})"
        )

    # Log the call with user_id TRUNCATED to first 8 chars — full
    # user_id is PII (Google sub claim) and must not land in logs
    # that may be shipped to third-party aggregators.
    _log.info(
        "gdocs_admin_audit: user=%s window=%dh",
        user_id[:8], since_hours,
    )

    # Lazy import to keep server.py module-load lean and avoid
    # circular-import risk if user_store ever grows server-side deps.
    from google_docs_mcp import user_store

    state = user_store.get_state(user_id)
    cutoff = int(time.time()) - since_hours * 3600

    entries: list[dict] = []
    updated_at = state.get("updated_at")
    if updated_at is not None and int(updated_at) >= cutoff:
        entries.append({
            "timestamp": int(updated_at),
            "operation_type": "user_state_updated",
            "doc_id": None,  # not tracked at this granularity
            "success": True,  # row presence implies completed write
        })

    return {
        "user_id_prefix": user_id[:8],
        "window_hours": since_hours,
        "total_entries": len(entries),
        "entries": entries,
        "notes": (
            "user_state.db tracks updated_at per user row, not per "
            "Google API call. Use this to confirm whether the user's "
            "session was active in the window; for per-operation detail "
            "grep flyctl logs by the user_id_prefix returned here. "
            "Finer-grained audit logging is tracked in issue #25."
        ),
    }
