"""Admin / introspection / auth MCP tool registrations (Gap #7 — v2.2.2).

This module defines the ``@workspace_tool``-decorated tool functions
for the 7 admin-service tools previously in ``server.py``. Importing
this module triggers registration with the live ``mcp`` instance —
``server.py`` performs the import at the bottom of its module, AFTER
constructing ``mcp`` and AFTER ``decorators.register(mcp, ...)`` wires
the ``@workspace_tool`` decorator.

**Namespace cleanup (chore/tool-namespace-cleanup).** These tools are
admin / introspection / auth, not Docs, so they were renamed off the
historical ``gdocs_`` prefix to honest prefixes (``server_`` for
introspection, ``admin_`` for admin-gated, ``account_`` for auth-state,
``gdrive_`` for the Drive-bound signed-upload URL). Every old ``gdocs_``
name stays registered as a DEPRECATED ALIAS (dual-registration; planned
removal v3.0) so nothing breaks — the same model PR-α used for
``gdocs_install_automation`` / ``gdocs_setup_apps_script``. Each alias
emits a ``DeprecationWarning`` (via
``appscriptly._deprecation.warn_deprecated_alias``) and delegates to the
canonical body.

**Tools registered here** (8 admin-service tools — canonical → alias):

1. ``server_info``               (alias ``gdocs_server_info``)           — server identity + tool inventory + CI status
2. ``server_test_manifest``      (alias ``gdocs_test_manifest``)         — full test inventory + per-test outcomes
3. ``server_guide``              (alias ``gdocs_guide``)                 — orientation as a structured payload
4. ``server_help``               (alias ``gdocs_help``)                  — error-message recovery guidance
5. ``server_health``             (NO alias; new in the 2026-07 wave)     — server / Google API / automation-runtime health report
6. ``gdrive_get_signed_upload_url`` (alias ``gdocs_get_signed_upload_url``) — mint one-shot signed upload URL
7. ``account_reset_authorization`` (alias ``gdocs_reset_authorization``) — clear stored OAuth credentials
8. ``admin_audit``               (alias ``gdocs_admin_audit``)           — forensic timeline (admin-token gated)

**Several tools use ``creds=False``** (decorator's standard credentials
injection is the wrong shape for each):

- ``server_info`` / ``server_test_manifest`` / ``server_guide`` /
  ``server_help`` — no Google API call (local introspection / lookup).
- ``gdrive_get_signed_upload_url`` — mints HMAC URL via ``keys.get_key``;
  handles its own ``current_user_id_or_none()`` check.
- ``account_reset_authorization`` — DELETES creds (inverse of normal auth
  path); pre-fetching creds would break the reset for users whose creds
  are already broken.
- ``admin_audit`` — gated by ``MCP_ADMIN_TOKEN`` (not user OAuth);
  reads ``user_store`` directly.

NOTE: the signed-upload-URL tool keeps ``service="admin"`` (it lives in
this admin folder; the ``service=`` tag follows the folder, the prefix
follows the domain) — its declaration stays in ``admin/_expected_tools.py``.

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
from googleapiclient.errors import HttpError

from appscriptly._deprecation import warn_deprecated_alias
from appscriptly.auth import default_data_dir
from appscriptly.credentials import current_user_id_or_none
from appscriptly.errors import friendly_http_error_message
from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service
from appscriptly.setup_apps_script import WebAppHealth, probe_webapp_health
from appscriptly.crypto import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TTL_SECONDS,
    MAX_MAX_BYTES,
    MAX_TTL_SECONDS,
    MIN_MAX_BYTES,
    sign_upload_url,
)
from appscriptly.keys import (
    get_first_call_timestamps,
    get_key,
    get_shim_hit_counters,
    get_total_call_counters,
)
from appscriptly.server import mcp, workspace_tool
from appscriptly.tool_schemas import (
    GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
    GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    GDOCS_GUIDE_OUTPUT_SCHEMA,
    GDOCS_HELP_OUTPUT_SCHEMA,
    GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
    SERVER_HEALTH_OUTPUT_SCHEMA,
)

_log = logging.getLogger("appscriptly.services.admin")


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
            "to enable admin_audit."
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
async def server_info() -> dict:
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
    ``server_guide()`` (workflows + rules + tool groupings) and
    ``server_test_manifest()`` (full per-test inventory). Cheap; no
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
    #
    # PR-Δ5.5 (2026-05-27): the PyPI distribution name was renamed
    # from ``google-docs-mcp`` to ``appscriptly``. New installs find
    # the package under ``appscriptly``; older installs (pre-rename
    # wheels still pinned via uv.lock at deploy time) find it under
    # the legacy name. Try the new name first, fall back to the old,
    # then "unknown". The fallback chain stays until the legacy
    # ``google-docs-mcp`` PyPI artifact is fully retired — same
    # horizon as the CLI-binary backward-compat alias.
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    ver = "unknown"
    for candidate in ("appscriptly", "google-docs-mcp"):
        try:
            ver = _pkg_version(candidate)
            break
        except PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            # Defensive: any non-PackageNotFoundError from importlib
            # bubbles to "unknown" rather than crashing server_info.
            break

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


@workspace_tool(
    title="DEPRECATED alias of server_info",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
)
async def gdocs_server_info() -> dict:
    """DEPRECATED — use ``server_info`` instead.

    Renamed off the historical ``gdocs_`` prefix (this is server
    introspection, not a Docs tool). Behavior is identical; the old name
    stays registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_server_info", "server_info")
    return await server_info()


# ---------------------------------------------------------------------
# 2. gdocs_test_manifest
# ---------------------------------------------------------------------


@workspace_tool(
    title="List CI test manifest",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
)
def server_test_manifest() -> dict:
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
    payload (same logic as server_info.test_suite); "ok"
    otherwise.

    Choreography: pairs with ``server_info.test_suite``. The
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


@workspace_tool(
    title="DEPRECATED alias of server_test_manifest",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
)
def gdocs_test_manifest() -> dict:
    """DEPRECATED — use ``server_test_manifest`` instead.

    Renamed off the historical ``gdocs_`` prefix (this is server
    introspection, not a Docs tool). Behavior is identical; the old name
    stays registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_test_manifest", "server_test_manifest")
    return server_test_manifest()


# ---------------------------------------------------------------------
# 3. gdocs_guide
# ---------------------------------------------------------------------


@workspace_tool(
    title="Orientation guide (local, no API)",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_GUIDE_OUTPUT_SCHEMA,
)
def server_guide() -> dict:
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
    ``server_info()`` (version + verified CI test status).

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
    from appscriptly import __version__

    return {
        "server": {
            "name": "google-docs-fly",
            "version": __version__,
            "what_it_does": (
                "Workspace Automation MCP. Generates persistent workflows "
                "(time-driven jobs, custom menus, reactive automations) "
                "that live in your Google Workspace and run on Google's "
                "infrastructure. Also creates, edits, reads, and manages "
                "Google Docs with native sidebar Tabs (Oct 2024+ feature) "
                "plus Sheets, Slides, Drive, and Apps Script projects."
            ),
            # Load-bearing for clients building tool calls: tools are
            # prefixed by DOMAIN. ``gdocs_`` is the Docs/native-tabs
            # surface; the other domains use their own honest prefix
            # (see additional_tool_prefixes). The historical ``gdocs_``
            # names for Drive / admin / auth tools are kept as DEPRECATED
            # ALIASES (planned removal v3.0) — prefer the canonical name.
            "all_tools_prefixed": "gdocs_",
            "additional_tool_prefixes": {
                "gdrive_": (
                    "Google Drive file management (find / move / trash / "
                    "share / export / signed-upload-URL)."
                ),
                "gsheets_": "Google Sheets tools.",
                "gslides_": "Google Slides tools.",
                "gforms_": "Google Forms tools.",
                "gcal_": "Google Calendar tools.",
                "gtasks_": "Google Tasks tools.",
                "gcontacts_": "Google Contacts (People API) tools.",
                "as_": (
                    "appscriptly-native automation tools (e.g. "
                    "as_generate_bound_script, as_install_automation)."
                ),
                "server_": (
                    "server introspection (server_info / server_guide / "
                    "server_help / server_test_manifest)."
                ),
                "admin_": "admin-gated operator tools (admin_audit).",
                "account_": "account/auth state (account_reset_authorization).",
            },
            "deprecated_aliases_note": (
                "Tools renamed off the historical gdocs_ prefix keep the "
                "old gdocs_ name registered as a deprecated alias (planned "
                "removal v3.0): e.g. gdocs_find_file -> gdrive_find_file, "
                "gdocs_server_info -> server_info, gdocs_admin_audit -> "
                "admin_audit. Prefer the canonical name."
            ),
            "more_info": (
                "Call server_info for build version + verified CI "
                "test status (digest, ci_run_url, mutation_check), and for "
                "the authoritative full tool inventory (tools list)."
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
                    "gdrive_get_signed_upload_url",
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
                    "gdrive_trash_file",
                    "gdrive_untrash_file",
                ],
                "notes": (
                    "ONLY acts on files this app created; others return "
                    "app_not_authorized. file_id accepts a string or "
                    "list (batch)."
                ),
            },
            {
                "name": "install_automation",
                "goal": (
                    "Make something keep working AFTER the chat ends — a "
                    "recurring job, a custom menu, a reaction to the user's "
                    "own future edits (persistent Workspace automation)"
                ),
                "tool_sequence": [
                    "as_install_automation",
                    "as_generate_bound_script",
                ],
                "notes": (
                    "One-time: as_install_automation provisions the "
                    "per-user Apps Script runtime (returns "
                    "status='needs_authorization' with an authorize_url if "
                    "consent is missing — surface that link, then retry). "
                    "Then as_generate_bound_script(container_id, "
                    "script_body, manifest?) binds a script with menus / "
                    "triggers / sidebars INTO a specific Doc/Sheet/Slides. "
                    "Use for 'every morning', 'when I edit', 'add a button' "
                    "— NOT for a one-off edit (use the direct tools)."
                ),
            },
            {
                "name": "spreadsheet",
                "goal": "Create a Google Sheet and put tabular data in it",
                "tool_sequence": [
                    "gsheets_create_spreadsheet",
                    "gsheets_write_range",
                    "gsheets_read_range",
                ],
                "notes": (
                    "create returns spreadsheet_id; write/read take that ID "
                    "+ an A1 range like 'Sheet1!A1:C10'. write needs the "
                    "tab to exist already. To write into an EXISTING sheet, "
                    "skip create and use its ID directly."
                ),
            },
            {
                "name": "presentation",
                "goal": "Create a Google Slides deck or fill a templated one",
                "tool_sequence": [
                    "gslides_create_presentation",
                    "gslides_get_outline",
                    "gslides_replace_all_text",
                ],
                "notes": (
                    "create returns presentation_id; get_outline discovers "
                    "slide/object IDs (don't guess them); replace_all_text "
                    "swaps literal placeholder tokens like '{{name}}' across "
                    "every slide. To edit an EXISTING deck, skip create."
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
            (
                "For persistent automation (recurring jobs, menus, onEdit "
                "reactions) use the install_automation workflow. For a "
                "one-off edit, use the direct docs/sheets/slides tools — "
                "no script needed."
            ),
            (
                "On any error, pass the raw error text to server_help for a "
                "structured next-action. An auth error's body carries a "
                "'Click here to authorize' link — surface it to the user; "
                "do NOT silently retry."
            ),
        ],
        "tool_groups": {
            "build_new": ["gdocs_make_tabbed_doc"],
            "convert_existing": [
                "gdocs_preview_tab_split",
                "gdocs_tab_existing_doc",
                "gdrive_get_signed_upload_url",
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
                "gdrive_find_doc_by_title",
                "gdrive_move_to_folder",
                "gdrive_trash_file",
                "gdrive_untrash_file",
            ],
            "setup_and_auth": [
                # as_install_automation is the canonical name (the
                # gdocs_install_automation and gdocs_setup_apps_script
                # deprecation aliases are registered for backward
                # compatibility but omitted here so the orientation
                # surface stays clean).
                "as_install_automation",
                "account_reset_authorization",
            ],
            "automation": [
                # Persistent Workspace automation (the appscriptly moat):
                # install the runtime, then generate bound scripts.
                "as_install_automation",
                "as_generate_bound_script",
            ],
            "spreadsheets": [
                "gsheets_create_spreadsheet",
                "gsheets_write_range",
                "gsheets_read_range",
            ],
            "presentations": [
                "gslides_create_presentation",
                "gslides_get_outline",
                "gslides_replace_all_text",
            ],
            "introspection": [
                "server_info",
                "server_test_manifest",
                "server_guide",
                # server_help: pass a raw error string, get the recovery
                # action. Belongs in the discoverable surface.
                "server_help",
            ],
        },
    }


@workspace_tool(
    title="DEPRECATED alias of server_guide",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_GUIDE_OUTPUT_SCHEMA,
)
def gdocs_guide() -> dict:
    """DEPRECATED — use ``server_guide`` instead.

    Renamed off the historical ``gdocs_`` prefix (this is server
    orientation, not a Docs tool). Behavior is identical; the old name
    stays registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_guide", "server_guide")
    return server_guide()


# ---------------------------------------------------------------------
# 4. gdocs_help
# ---------------------------------------------------------------------


@workspace_tool(
    title="Help for an error message (local, no API)",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_HELP_OUTPUT_SCHEMA,
)
def server_help(error_message: str) -> dict:
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
    to a different tool. Pairs with server_info() when filing
    bug reports for the unexpected_exception case.
    """
    # Lazy-import the recovery table to avoid forcing resources.py
    # load at this module's import time. resources.py registers
    # gdocs://error-recovery MCP resources as a side-effect of import;
    # server.py already triggers that import at module bottom, so by
    # the time this tool is *called* the table is populated.
    from appscriptly.resources import _RECOVERY_TABLE

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
            "call server_info() to capture version + commit, "
            "and consider filing an issue at the project repo with "
            "the raw error string so a new entry can be added."
        ),
    }


@workspace_tool(
    title="DEPRECATED alias of server_help",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_HELP_OUTPUT_SCHEMA,
)
def gdocs_help(error_message: str) -> dict:
    """DEPRECATED — use ``server_help`` instead.

    Renamed off the historical ``gdocs_`` prefix (this is an error-recovery
    lookup, not a Docs tool). Behavior is identical; the old name stays
    registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_help", "server_help")
    return server_help(error_message)


# ---------------------------------------------------------------------
# 4b. server_health (T1.2, 2026-07-10) — canonical only, NO alias
# ---------------------------------------------------------------------


_APPS_SCRIPT_USERSETTINGS_URL = "https://script.google.com/home/usersettings"

# Substrings (lowercased) that identify Google's "user has not enabled
# the Apps Script API" 403 - the distinctive signature the T1.2 health
# check looks for. Google's message reads: "User has not enabled the
# Apps Script API. Enable it by visiting
# https://script.google.com/home/usersettings then retry."
_APPS_SCRIPT_API_DISABLED_MARKERS = (
    "has not enabled the apps script api",
    "script.google.com/home/usersettings",
)


def _is_apps_script_api_disabled(e: HttpError) -> bool:
    """True iff ``e`` is Google's Apps-Script-API-disabled 403."""
    text = " ".join(
        str(part)
        for part in (
            e,
            getattr(e, "reason", "") or "",
            getattr(e, "error_details", "") or "",
        )
    ).lower()
    return any(marker in text for marker in _APPS_SCRIPT_API_DISABLED_MARKERS)


def _peek_credentials_non_interactive():
    """Resolve Google creds WITHOUT ever starting an interactive flow.

    A health check must never pop a browser consent (stdio) or raise a
    ToolError (HTTP) - it REPORTS auth state instead. Returns
    ``(creds | None, status, detail)`` where status is the tool's
    ``google_api`` value so far ("ok" pending the live probe,
    "unauthorized", or "error").
    """
    user_id = current_user_id_or_none()

    if user_id is not None:
        # HTTP / multi-tenant: the standard resolver, with
        # NeedsReauthError reported as data instead of raised.
        from appscriptly.credentials import (
            NeedsReauthError,
            get_credentials_for_user,
        )
        from appscriptly.oauth_google import resolve_runtime_oauth_config

        try:
            creds = get_credentials_for_user(
                user_id, **resolve_runtime_oauth_config()
            )
            return creds, "ok", None
        except NeedsReauthError as e:
            return None, "unauthorized", (
                f"Google authorization required. Open {e.auth_url} "
                f"and grant access, then re-run."
            )
        except RuntimeError as e:
            return None, "error", f"Server OAuth config error: {e}"

    # Stdio / single-tenant: peek at the cached token file only. The
    # normal loader (auth.load_credentials) LAUNCHES a browser consent
    # flow when the token is missing/stale - never acceptable from a
    # health probe - so this replicates just its non-interactive prefix.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as _UserCredentials

    from appscriptly.auth import SCOPES

    token_file = default_data_dir() / "token.json"
    if not token_file.exists():
        return None, "unauthorized", (
            "No cached OAuth token. Run any Google-backed tool once "
            "to complete the consent flow."
        )
    try:
        creds = _UserCredentials.from_authorized_user_file(
            str(token_file), SCOPES
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                return None, "unauthorized", (
                    "Cached OAuth token is not usable (expired with no "
                    "refresh token). Re-run a Google-backed tool to "
                    "re-consent."
                )
        return creds, "ok", None
    except Exception as e:  # noqa: BLE001 - report, never raise, in a health probe
        return None, "unauthorized", (
            f"Cached OAuth token could not be loaded/refreshed: "
            f"{type(e).__name__}: {e}"
        )


def _probe_google_api(creds) -> tuple[str, str | None]:
    """One cheap credentialed round-trip: ``drive.about.get``.

    Classifies the Google API layer as ok / unauthorized / error.
    ``about.get`` is accepted under ``drive.file`` (no extra scope) and
    returns a tiny payload.
    """
    from google.auth.exceptions import GoogleAuthError

    try:
        drive = get_service("drive", "v3", credentials=creds)
        execute_with_retry(
            lambda: drive.about().get(fields="user").execute(),
            idempotent=True,
            op_name="drive.about.get.health",
        )
        return "ok", None
    except HttpError as e:
        status = getattr(e, "status_code", None)
        if status in (401, 403):
            return "unauthorized", friendly_http_error_message(e)
        return "error", friendly_http_error_message(e)
    except GoogleAuthError as e:
        # RefreshError and friends: the token is revoked/broken.
        return "unauthorized", f"OAuth token refresh failed: {e}"
    except Exception as e:  # noqa: BLE001 - report, never raise, in a health probe
        return "error", f"{type(e).__name__}: {e}"


def _read_runtime_state() -> tuple[str | None, str | None]:
    """Return ``(script_id, exec_url)`` for the calling identity.

    HTTP mode reads the caller's user_store row; stdio reads the local
    setup-state ledger. Either value may be None (never installed, or
    a partial install).
    """
    user_id = current_user_id_or_none()
    if user_id is not None:
        from appscriptly import user_store

        row = user_store.get_state(user_id)
        return (
            row.get("apps_script_script_id"),
            row.get("apps_script_url"),
        )
    from appscriptly import setup_state

    state = setup_state.load_state(default_data_dir())
    return state.get("script_id"), state.get("url")


@workspace_tool(
    title="Health check: server, Google API, automation runtime",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    # creds=False: a health check REPORTS auth failures as data
    # (google_api: "unauthorized") - the standard creds envelope would
    # turn them into a raised ToolError and hide the rest of the report.
    output_schema=SERVER_HEALTH_OUTPUT_SCHEMA,
)
def server_health() -> dict:
    """Report the health of the server, Google API access, and the automation runtime.

    USE WHEN: something Apps-Script-backed is failing and you need to
    know WHERE it is broken before retrying - or as a preflight before
    an ``as_*`` automation install. One call answers three questions:

    - ``server``: is the MCP server itself responding ("ok" - if you
      got a response at all, it is).
    - ``google_api``: can the calling account make a real Google API
      round-trip right now ("ok" | "unauthorized" | "error", with
      ``google_api_detail`` explaining any non-ok).
    - ``automation_runtime``: the per-user Apps Script web app that
      powers ``as_*`` automations - ``{installed, exec, gates,
      remediation_url, detail}`` where ``exec`` is one of:

      - ``"serving"``: the /exec endpoint answered like a live script.
      - ``"needs_activation"``: deployed, but Google refuses requests
        (403 door page) until the owning user opens the script once
        interactively and clicks Allow. ``remediation_url`` is the
        script editor to do that in.
      - ``"api_disabled"``: the Google account has the Apps Script API
        toggled OFF - nothing script-related can be managed until it
        is enabled. ``remediation_url`` is
        https://script.google.com/home/usersettings (toggle ON, retry).
      - ``"not_installed"``: no runtime recorded for this account (or
        the recorded project no longer exists). Run
        ``as_install_automation``.
      - ``"unknown"``: the liveness probe hit transport trouble
        (timeout / DNS); says nothing about the deployment - retry.

    SCOPE OF IMPACT (R3, 2026-07-10 retest 2): the automation runtime
    gates the ``as_*`` automation family ONLY. Conversions
    (/api/convert, gdocs_tab_existing_doc, gdocs_make_tabbed_doc) and
    every gdocs/gdrive/gsheets/gslides tool run on plain Google REST
    APIs and work fine while ``exec`` reads ``needs_activation`` or
    ``api_disabled`` - do NOT abort a convert on those readings. The
    ``gates`` field in the payload states this machine-readably.

    Read-only: one Drive ``about.get``, at most one Apps Script
    ``projects.get``, and one anonymous GET of the /exec URL. Never
    triggers a consent flow and never raises for auth problems - it
    reports them.

    Returns:
        ``{"server": "ok", "google_api": "ok"|"unauthorized"|"error",
        "google_api_detail": str|null, "automation_runtime":
        {"installed": bool, "exec": str, "gates": str,
        "remediation_url": str|null, "detail": str|null}}``.

    Choreography: run before / after ``as_install_automation`` to
    verify runtime state, or first thing when an ``as_*`` tool or
    ``server_info`` reports something odd.
    """
    creds, api_status, api_detail = _peek_credentials_non_interactive()
    if creds is not None:
        api_status, api_detail = _probe_google_api(creds)

    # R3: every runtime state carries WHAT it gates, so a caller seeing
    # needs_activation does not false-abort a convert (retest 2 proved
    # 7/7 converts succeed while the runtime needs activation - the
    # convert path is pure REST since #222).
    runtime: dict = {
        "gates": (
            "as_* automation tools only; convert (/api/convert, "
            "gdocs_tab_existing_doc, gdocs_make_tabbed_doc) and all "
            "gdocs/gdrive/gsheets/gslides tools are unaffected"
        ),
    }
    try:
        script_id, exec_url = _read_runtime_state()
    except Exception as e:  # noqa: BLE001 - report, never raise, in a health probe
        script_id, exec_url = None, None
        runtime["detail"] = (
            f"Could not read the runtime install ledger: "
            f"{type(e).__name__}: {e}"
        )

    # The truthiness check doubles as pyright narrowing: past this
    # guard, script_id and exec_url are non-None strings.
    if not script_id or not exec_url:
        runtime["installed"] = False
        runtime.setdefault(
            "detail",
            "Automation runtime is not installed for this account. "
            "Run as_install_automation to install it.",
        )
        runtime["exec"] = "not_installed"
        runtime["remediation_url"] = None
        return {
            "server": "ok",
            "google_api": api_status,
            "google_api_detail": api_detail,
            "automation_runtime": runtime,
        }

    runtime["installed"] = True

    # The distinctive T1.2 case first: with creds available, one cheap
    # Apps Script API call tells us whether the account has the API
    # toggled off (its 403 carries an unmistakable message). Checked
    # BEFORE the URL probe because a disabled API makes every
    # script-management operation fail regardless of what /exec says.
    if creds is not None:
        try:
            script = get_service("script", "v1", credentials=creds)
            execute_with_retry(
                lambda: script.projects().get(scriptId=script_id).execute(),
                idempotent=True,
                op_name="script.projects.get.health",
            )
        except HttpError as e:
            if _is_apps_script_api_disabled(e):
                runtime["exec"] = "api_disabled"
                runtime["remediation_url"] = _APPS_SCRIPT_USERSETTINGS_URL
                runtime["detail"] = (
                    "The Apps Script API is turned OFF for this Google "
                    "account, so the automation runtime cannot be "
                    "managed. Open the remediation URL, toggle 'Google "
                    "Apps Script API' ON, then retry."
                )
                return {
                    "server": "ok",
                    "google_api": api_status,
                    "google_api_detail": api_detail,
                    "automation_runtime": runtime,
                }
            if getattr(e, "status_code", None) == 404:
                runtime["installed"] = False
                runtime["exec"] = "not_installed"
                runtime["remediation_url"] = None
                runtime["detail"] = (
                    "The recorded Apps Script project no longer exists "
                    "(deleted from Drive?). Re-run "
                    "as_install_automation to reinstall."
                )
                return {
                    "server": "ok",
                    "google_api": api_status,
                    "google_api_detail": api_detail,
                    "automation_runtime": runtime,
                }
            # Any other script-API error: not the distinctive case;
            # fall through to the URL probe, which needs no creds.
            runtime["detail"] = (
                f"Apps Script API check inconclusive: "
                f"{friendly_http_error_message(e)}"
            )
        except Exception as e:  # noqa: BLE001 - report, never raise, in a health probe
            runtime["detail"] = (
                f"Apps Script API check inconclusive: "
                f"{type(e).__name__}: {e}"
            )

    health = probe_webapp_health(exec_url)
    if health is WebAppHealth.HEALTHY:
        runtime["exec"] = "serving"
        runtime["remediation_url"] = None
        runtime.setdefault("detail", None)
    elif health is WebAppHealth.DEAD:
        runtime["exec"] = "needs_activation"
        runtime["remediation_url"] = (
            f"https://script.google.com/d/{script_id}/edit"
        )
        runtime["detail"] = (
            "The /exec endpoint refuses requests (Google's 403 door "
            "page). The deployment needs its one-time interactive "
            "activation: open the remediation URL as the installing "
            "user, run any function once (e.g. doGet), and click "
            "Allow on the consent prompt. Requests serve normally "
            "after that. This gates as_* automation tools ONLY - "
            "document conversion and the gdocs/gdrive/gsheets/gslides "
            "tools do not use this runtime and keep working."
        )
    else:
        runtime["exec"] = "unknown"
        runtime["remediation_url"] = None
        runtime.setdefault(
            "detail",
            "The /exec liveness probe hit transport trouble (timeout "
            "or connection failure) - this says nothing about the "
            "deployment itself. Retry in a moment.",
        )

    return {
        "server": "ok",
        "google_api": api_status,
        "google_api_detail": api_detail,
        "automation_runtime": runtime,
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
def gdrive_get_signed_upload_url(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
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
        max_bytes: Upload size cap baked into the signature and ENFORCED
            by /api/convert — an upload whose body exceeds it is rejected
            with HTTP 413. Defaults to 50 MB (Drive's converter ceiling);
            must be in [1, 100 MB].

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
    # Validate the cap up front so a bad value surfaces as a clean
    # ToolError (Markdown-renderable in the connector UI) rather than the
    # ValueError sign_upload_url would otherwise raise. Bounds are the
    # crypto-layer floor/ceiling — now that the cap is enforced, ≤0 would
    # brick every upload and an unbounded value would defeat the cap.
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)  # True/False are ints in Python
        or max_bytes < MIN_MAX_BYTES
        or max_bytes > MAX_MAX_BYTES
    ):
        raise ToolError(
            f"max_bytes must be an int in [{MIN_MAX_BYTES}, {MAX_MAX_BYTES}], "
            f"got {max_bytes!r}"
        )

    # v2.1: every signed URL is bound to the calling user. Without a
    # FastMCP auth context we have no user — stdio callers don't need
    # /api/convert at all (they have direct tool access), so refuse
    # rather than mint an operator-scoped URL that would write into
    # the wrong Drive.
    user_id = current_user_id_or_none()
    if user_id is None:
        raise ToolError(
            "gdrive_get_signed_upload_url requires an authenticated MCP "
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
    # T3.2: FULL parameter parity documented here (the endpoint was
    # silently accepting fields the hint never mentioned). Keep this
    # hint in lockstep with convert_endpoint's form parsing.
    minted["usage_hint"] = (
        "requests.post(URL, files={'file': ('doc.docx', open('/path/to/doc.docx','rb'), "
        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document')}, "
        "data=params, timeout=600). All params optional: "
        "split_by ('heading_1'|'heading_2'|'page_break'|'auto'), "
        "nest_by ('heading_2', only with split_by='heading_1': H2 sections become child tabs), "
        "title (document title, single input only), "
        "icons_by_title (JSON string, tab-title fragment to emoji), "
        "placeholder_behavior ('delete'|'rename'|'keep'), "
        "markers (JSON list of {'marker_text','tab_title'}: injects Heading 1s into a styled docx first), "
        "on_conflict ('new'|'replace'|'skip', by title; 'replace' trashes ALL app-visible "
        "same-title priors and lists them in replaced_doc_ids), "
        "async ('1': immediate 202 {job_id, status_url}; poll status_url until a terminal "
        "status: 'done' MEANS SUCCESS and carries the result; 'error' carries the failure "
        "(message at error.message) plus any recovery data. RECOMMENDED for docs with many "
        "sections; sync requests can take 60s+ and risk client timeouts), "
        "drive_file_id (convert an existing app-accessible Drive docx/Doc: pass data only, "
        "no files=; an explicit title is honored verbatim on every entry point), "
        "drive_file_ids (JSON list) or repeated 'file' parts (batch: always 202 {jobs:[...]}). "
        "Conversion survives client disconnects (job model): re-POSTing the identical request "
        "within 15 min attaches to the same in-flight or succeeded job instead of duplicating "
        "docs, even though the URL is single-use. If a status poll reads 'stalled' (server "
        "redeployed mid-run), the same re-POST resumes that job. FAILED attempts are not "
        "deduplicated: re-POST with a fresh signed URL to run a new conversion."
    )
    return minted


@workspace_tool(
    title="DEPRECATED alias of gdrive_get_signed_upload_url",
    service="admin",
    readonly=False, destructive=False, idempotent=False, external=True,
    output_schema=GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
)
def gdocs_get_signed_upload_url(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """DEPRECATED — use ``gdrive_get_signed_upload_url`` instead.

    Renamed off the historical ``gdocs_`` prefix (the signed URL uploads
    to Drive, not Docs specifically). Behavior is identical; the old name
    stays registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias(
        "gdocs_get_signed_upload_url", "gdrive_get_signed_upload_url"
    )
    return gdrive_get_signed_upload_url(
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
    )


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
def account_reset_authorization(full: bool = False) -> dict:
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
        from appscriptly import user_store
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
    from appscriptly import _tool_helpers
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


@workspace_tool(
    title="DEPRECATED alias of account_reset_authorization",
    service="admin",
    readonly=False, destructive=True, idempotent=True, external=True,
    output_schema=GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
)
def gdocs_reset_authorization(full: bool = False) -> dict:
    """DEPRECATED — use ``account_reset_authorization`` instead.

    Renamed off the historical ``gdocs_`` prefix (this resets the account
    OAuth state, not a Docs object). Behavior is identical; the old name
    stays registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias(
        "gdocs_reset_authorization", "account_reset_authorization"
    )
    return account_reset_authorization(full=full)


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
def admin_audit(
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
        "admin_audit: user=%s window=%dh",
        user_id[:8], since_hours,
    )

    # Lazy import to keep server.py module-load lean and avoid
    # circular-import risk if user_store ever grows server-side deps.
    from appscriptly import user_store

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


# ---------------------------------------------------------------------
# Deprecated alias — gdocs_admin_audit → admin_audit
# ---------------------------------------------------------------------


@workspace_tool(
    title="DEPRECATED alias of admin_audit",
    service="admin",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
)
def gdocs_admin_audit(
    admin_token: str, user_id: str, since_hours: int = 24,
) -> dict:
    """DEPRECATED — use ``admin_audit`` instead.

    Renamed off the historical ``gdocs_`` prefix (this is an admin-gated
    forensic query, not a Docs tool). Behavior is identical; the old name
    stays registered as an alias and is slated for removal in v3.0.
    """
    warn_deprecated_alias("gdocs_admin_audit", "admin_audit")
    return admin_audit(admin_token, user_id, since_hours=since_hours)
