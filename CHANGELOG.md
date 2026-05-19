# Changelog

All notable changes to `google-docs-mcp`.

This project follows [Semantic Versioning](https://semver.org/).

## [1.3.1] — 2026-05-19

Security hotfix. Closes a cluster of pre-production hardening gaps
surfaced during the L10 architecture audit and bumps four
transitive deps off known-CVE versions. No protocol changes, no
user re-consent required. Existing `MCP_BEARER_TOKEN` continues to
work unchanged.

### Why

Audit findings R3–R20 identified three classes of pre-production
risk: (1) unbounded request bodies on `/api/convert` could OOM the
512 MB Fly VM with a crafted zip-bomb, (2) missing `Host` header
validation left the public endpoint exposed to host-confusion
attacks, (3) four transitive dependencies were pinned to versions
with disclosed CVEs (notably `cryptography` 45.x with the
cert-validation path issue fixed in 46.0.7). Each is shippable in
isolation; bundling them reduces deploy churn and keeps the
middleware stack coherent.

### Added

- **`keys.py`** — HKDF key-derivation scaffolding. Today the shim
  path returns the raw `MCP_BEARER_TOKEN` for back-compat with all
  three derived purposes (`api_bearer`, `oauth_state`,
  `signed_url`), so existing signed URLs and OAuth states minted
  pre-v1.3.1 verify cleanly after upgrade. Derived path is
  inactive; v2.0 ships the strict-flip with a planned mass-token
  rotation window. Operators may set
  `MCP_API_BEARER_KEY` / `OAUTH_STATE_SIGNING_KEY` /
  `SIGNED_URL_SIGNING_KEY` anytime to override individual keys.
- **`TrustedHostMiddleware`** with `derive_trusted_hosts()` helper
  reading `FLY_APP_NAME` + `localhost` + `*.fly.dev`. Fail-closed
  startup assertion if `FLY_REGION` is set without `FLY_APP_NAME`
  (catches misconfigured Fly deploys at boot, not at first request).
- **`BodySizeLimitMiddleware`** — rejects `/api/*` requests with
  declared `Content-Length` > 10 MB at 413 before any body bytes
  are read. Chunked-encoding bypass is closed in v1.4 via
  Starlette's built-in `request.form(max_part_size=...)`.
- **`_validate_title()`** — rejects control characters
  (U+0000–001F, U+007F) and titles > 1024 chars before they reach
  Google's API. Applied to `gdocs_make_tabbed_doc`,
  `gdocs_tab_existing_doc`, `gdocs_rename_tab`, `gdocs_add_tabs`.
  Pre-fix, control chars in titles returned a confusing 400
  from Google with no per-field hint.

### Changed

- **`setup_state.save_state`** now writes via tmpfile + `os.replace`
  for atomic persistence. A crash mid-write (machine SIGKILL,
  disk-full, container OOM) no longer corrupts the setup ledger;
  the prior file remains intact and the partial tmpfile is
  discarded.
- **`server.py::_get_credentials`** retains the existing
  `NeedsReauthError → ToolError` mapping unchanged. This will be
  subsumed by the `@gdocs_tool` decorator in v1.5; pre-announced
  here so the v1.5 PR is a pure refactor with no behavior delta.
- **Dependency floors** bumped to clear disclosed CVEs:
  `cryptography ≥ 46.0.7` (cert-validation),
  `pyjwt ≥ 2.12.0` (algorithm-confusion patch),
  `urllib3 ≥ 2.7.0` (redirect-header injection),
  `requests ≥ 2.33.0` (carries the urllib3 bump),
  `starlette ≥ 0.40` (explicit pin so it's no longer purely
  transitive). `uv.lock` regenerated against current PyPI; no
  resolver conflicts.

### Tests

- `test_keys_back_compat_purposes_all_return_raw_master` — all
  three purposes return `MCP_BEARER_TOKEN` when no override is set.
- `test_keys_short_master_fails_loud` — `MCP_BEARER_TOKEN` < 32
  chars raises `RuntimeError` at first `get_key` call.
- `test_derive_trusted_hosts_*` — three cases (`FLY_APP_NAME` set,
  `TRUSTED_HOSTS` override, both unset → fail-open with WARNING).
- `test_bodysize_413_when_content_length_exceeds` — multipart POST
  with declared 60 MB rejected at 413 before body read.
- `test_validate_title_rejects_control_chars` — `title="x\x00y"`
  raises `ToolError` from `gdocs_make_tabbed_doc`.
- `test_setup_state_save_atomic_under_crash` — patches `os.replace`
  to raise; asserts original file untouched and tmpfile cleaned up.

All 240 existing unit tests pass. Mutation gate: 8/8 caught.

### Deferred

Per audit R30-A merge-blocker triage, the following land in later
releases rather than gate this hotfix:

- `_FIELD_VALIDATORS` for `user_store` row-level validation → v1.4
- Integration tests + `e2e.yml` workflow → v1.4
- Migration script for legacy user_state rows → v2.0
- `docs/THREAT_MODEL.md`, `RUNBOOK.md`, `TOOL_CONTRACT.md`,
  `CONTRIBUTING.md` → v2.2 (batched docs release)
- `LLM_RECOVERY.md` + `@mcp.resource("gdocs://error-recovery")` +
  `gdocs_help` tool → v2.2 batch
- HKDF derived-path activation (mass-token rotation event) → v2.0

### Acceptance

A v1.3.1 deploy survives a `Host: evil.com` probe with 400, a
60 MB upload to `/api/convert` with 413, and a `title="x\x00"`
tool call with `ToolError` — all before reaching any code that
would have crashed or hit Google's API with bad input. Existing
signed upload URLs and OAuth states minted on v1.3.0 continue
to verify against v1.3.1's `keys.get_key()` calls.

## [1.3.0] — 2026-05-19

Make the MCP self-documenting — the external
`google-docs-fly_MCP_Reference.md` becomes redundant by design.

### Why

Using this server previously required out-of-band documentation: a
hand-written reference file. That's a design smell. An agent
connecting to the server got 21 isolated tool descriptions and no
orientation — no statement of what the server does, no workflow
choreography, no surfacing of the operating rules that were only
learned by hitting errors. v1.3.0 moves that knowledge into the
MCP so it travels with the server.

### Added: connect-time orientation

`_SERVER_INSTRUCTIONS` (the protocol-level instructions string) now
contains:

- One sentence on what the server does.
- The **5 named workflows** as goal → tool sequence:
  `new_doc`, `convert_doc_with_headings`, `retrofit_styled_doc`,
  `convert_sandbox_docx`, `cleanup`.
- The **5 non-obvious operating rules** (never rebuild a styled
  .docx; `docx_path` doesn't work from cloud chat; `placeholder_
  behavior="rename"` preserves a title page; trash tools only act
  on files this app created; first use needs interactive OAuth
  consent).
- Pointer to `gdocs_server_info` for build + verified test status.

### Added: gdocs_guide tool

Zero-argument tool that returns the same orientation as a structured
payload (workflows, rules, tool_groups). Rationale: server
instructions is seen only at connect time and some clients truncate
or ignore it. `gdocs_guide` is always reachable as the "start here"
/ `--help` entry point.

Shape:
```
{
  server: {name, version, what_it_does, all_tools_prefixed, more_info},
  workflows: [{name, goal, tool_sequence, notes}, ...],
  operating_rules: [str, ...],
  tool_groups: {build_new, convert_existing, edit_tabs, read,
                drive_management, setup_and_auth, introspection},
}
```

### Changed: tool descriptions are now workflow-aware

Every tool description now ends with a `Choreography:` line stating
what typically comes before / after it, and (where applicable) a
`NOTE:` block for known failure modes an agent would otherwise
discover by hitting an error:

- `gdocs_preview_tab_split` — Typically called before
  `gdocs_tab_existing_doc`. NOTE: `docx_path` doesn't work from
  cloud chat.
- `gdocs_tab_existing_doc` — Typically preceded by
  `gdocs_preview_tab_split`; follow with `gdocs_get_doc_outline`.
  NOTE: `docx_path` doesn't work from cloud chat.
- `gdocs_get_signed_upload_url` — POST is equivalent to
  `gdocs_tab_existing_doc`; sandbox-bytes route only. NOTE:
  `docx_path` arguments don't work from cloud chat.
- `gdocs_trash_file` / `gdocs_untrash_file` — NOTE: only works on
  files this app created; others return `app_not_authorized`.
- `gdocs_setup_apps_script` — NOTE: First call returns
  `needs_authorization` with a URL the user must open — consent
  cannot be automated.

All 21 existing tools touched; the new `gdocs_guide` makes 22.

### Tests

- `test_tool_schemas.py::EXPECTED_TOOLS` updated with `gdocs_guide`
  (22 tools); `no_arg_tools` extended.
- New `test_server_info.py::test_gdocs_guide_shape_includes_all_5_
  workflows_and_rules` — asserts gdocs_guide returns the 5 named
  workflows, 5 operating-rule topics, all 7 tool_group buckets.

### Acceptance

A fresh agent that has only (a) the server instructions and (b) the
tool list — with no external reference file — can correctly choose
and sequence tools for all 5 core workflows, and avoids the known
failure modes without first triggering them. `gdocs_guide` returns
the orientation as a callable fallback.

The external `google-docs-fly_MCP_Reference.md` is now redundant by
design.

## [1.2.3] — 2026-05-19

Hot-fix: v1.2.2 shipped with the CHANGELOG / mutation_check.py /
test changes but `_read_mutation_check()` in `src/google_docs_mcp/
server.py` still had the v1.2.1 four-field return shape. Live
`gdocs_server_info.test_suite.mutation_check` was missing
`stale_patches` and `imprecise_patches` — making v1.2.2's headline
acceptance criterion non-verifiable from cloud chat.

### Root cause

`scripts/mutation_check.py`'s `revert()` used `git checkout --
<file>` to restore mutated sources. That works against a clean
working tree but **wipes uncommitted edits** in any file that
mutation_check also mutates. Three of the eight mutations target
`server.py`. The v1.2.2 edit to `_read_mutation_check` was applied
in the working tree, then locally-run `mutation_check.py` mutated
`server.py` for `test_trash_file_id_accepts_str_or_list` and
reverted via git checkout — silently restoring `server.py` to HEAD
and wiping the new return shape. The commit captured the wiped
state; CI built that; live runtime served the old shape.

### Fix

- `_read_mutation_check()` now actually returns `stale_patches` and
  `imprecise_patches` (the v1.2.2 intent, restored).
- `apply_mutation()` returns the **original file bytes** on success
  (was: `bool`). `revert()` writes those bytes back from memory —
  never touches git. Uncommitted edits in mutated source files now
  survive `mutation_check.py` runs.

### Tests

Two new tests in `tests/unit/test_mutation_check.py`:
- `test_revert_restores_original_bytes_not_via_git` — direct check
  that `revert` writes the saved bytes, not whatever git would have.
- `test_revert_is_noop_when_original_is_none` — covers the
  stale-patch branch where nothing was mutated.

19/19 unit tests pass. Local `mutation_check.py` reports 8/8 caught
cleanly with the new revert path, and the uncommitted
`_read_mutation_check` edit survives the run.

## [1.2.2] — 2026-05-19

Preventive maintenance for the mutation gate itself: detect when an
injection patch has rotted instead of silently reporting "caught"
for a bug that was never actually introduced.

### Why

Each mutation in `scripts/mutation_check.py` is a fixed `find`/`replace`
diff against a specific source line. As the codebase evolves, that
line can move or be rewritten — at which point the `find` text no
longer matches, the patch silently no-ops, the targeted test passes
(because nothing was mutated), and `mutation_check` reports the
guard as caught. The verification looks green while being hollow.
This was the one quiet way the self-evidencing gate could degrade.

### Added

`scripts/mutation_check.py` now classifies each mutation into one of
four outcomes instead of binary caught/asleep:

| Outcome           | Meaning                                                    |
|-------------------|------------------------------------------------------------|
| `caught`          | Patch applied, only the targeted test failed (clean catch) |
| `stale_patch`     | `find` text gone, OR patch applied but 0 tests failed      |
| `imprecise_patch` | Target failed AND unrelated tests also failed (collateral) |
| `asleep_guard`    | Patch applied, but the named guard didn't notice the bug   |

To distinguish these, the gate now runs the **full unit suite** per
mutation (not just the targeted test) — ~11s × 8 mutations adds ~90s
to CI's mutation job. The cost buys collateral-damage detection,
which is the only way to tell `caught-cleanly` from
`caught-but-the-patch-is-over-broad`.

### Surfaced

`gdocs_server_info.test_suite.mutation_check` adds two fields:

```
mutation_check: {
  ran, caught, status, asleep_guards,        # existing
  stale_patches:     [guard names whose patch no longer applies],
  imprecise_patches: [guard names whose patch broke unrelated tests]
}
```

`status` is `"passed"` only when `caught == ran` AND `stale_patches`,
`imprecise_patches`, `asleep_guards` are all empty. When something
fails, status takes a specific subtype value (most-fundamental first):
`stale_patch` > `imprecise_patch` > `asleep_guard`. Pre-1.2.2
`mutation-check.json` artifacts default the new fields to `[]` for
back-compat.

### Refinement: expected_collateral

The strict "exactly one named failure" rule punished legitimate
defense in depth — cases where multiple tests genuinely test the
same code path and a faithful mutation trips all of them. Mutation
now accepts an `expected_collateral: list[str]` field declaring
known sibling guards; failures matching these are forgiven, but
any other surprise still flags `imprecise_patch`. Two of the v1.1.x
mutations declared siblings on first audit:

- `test_inject_matches_fragmented_runs` declares
  `test_inject_matches_nbsp_via_sym` (shared `_extract_visible_text`)
- `test_tool_discoverability_via_server_info` declares
  `test_server_info_self_consistency` (both notice tool_count drift)

Also: `test_path` now matches parametrized failures by prefix
(`base` matches `base[case1]`), since pytest reports parametrized
ids and the historical Mutations declared base names.

### Tests

`tests/unit/test_mutation_check.py` (new, 17 tests): unit tests for
`apply_mutation` (find text absent/ambiguous), `_matches_nodeid`
(parametrized prefix match), `classify_outcome` (all four branches
plus expected_collateral), and `aggregate` (status priority). The
classification is pure-function-testable; the integration
"deliberately rot a real patch → CI reports stale_patch" path is
verifiable any time someone refactors source under the existing
mutation list.

### Priority note

Low — this is preventive maintenance, not an active bug. Worth doing
once so `mutation_check` can't silently hollow out, then leave the
MCP alone.

## [1.2.1] — 2026-05-18

Closes the last v1.2.0 gap: `mutation_check.ran` goes 3 → 8.
Every named regression guard now has an automated mutation
proving it actually catches its named bug pattern.

### Added

Mutations for the 5 guards that v1.2.0 deferred. Researched in
parallel by 5 named subagents (one per guard), each VERIFIED
empirically (apply patch → run pytest → expect exit ≠ 0 → revert).
All 5 came back with working diffs; 8/8 caught on local run.

- **`test_owned_by_app_agrees_with_trash_outcome`** — flip the
  403-probe branch from `write_results[fid] = False` to `True`.
  Probe lies about writability → find claims `owned_by_app=True`
  but trash still 403s → cross-tool inconsistency assertion fires.
  Reintroduces the v0.19.0 bug pattern.

- **`test_inject_matches_fragmented_runs`** — add `break` after
  the first `<w:t>` text append in `_extract_visible_text`.
  Extraction stops at the first run; fragmented paragraph
  `["Sec", "tion", " ", "Banner"]` becomes just `"Sec"`. Marker
  "Section Banner" can't match. Reintroduces the pre-v0.15.1
  text-extraction bug.

- **`test_preview_flags_what_convert_truncates`** — drift
  `TITLE_MAX_CHARS` from 50 to 60 in `preview.py`. The test's
  fixture heading is exactly 60 chars, so `60 > 60` becomes False
  and no warning fires; even if one fired, the message would
  interpolate "60" not "50" (test asserts "50" in msg). Double
  failure mechanism — robust catch.

- **`test_auth_pkce_consistency_every_url`** — the hard one from
  v1.2.0. Original attempt commented out `flow.code_verifier = ...`
  but `Flow.authorization_url()` auto-generates a 128-char verifier
  when `code_verifier=None AND autogenerate_code_verifier=True`
  (lib default). PKCE survived via the fallback. New mutation
  overrides BOTH paths AFTER the original assignment:
  `flow.code_verifier = None; flow.autogenerate_code_verifier = False`.
  URL emits no `code_challenge`. Test's first assertion fires
  immediately.

- **`test_tool_discoverability_via_server_info`** — slice `[1:]`
  on the sorted tool list returned by `gdocs_server_info`. Drops
  the alphabetically-first tool (`gdocs_add_tabs`) while leaving
  `mcp.list_tools()` intact → set-equality fails AND `tool_count`
  diverges (20 vs 21).

### Process note

The 5 mutations were dispatched as parallel subagents. Each had
the same brief: read the test, read the code, design a minimal
find/replace, VERIFY by running pytest on the mutated state,
revert via `git checkout --`. All 5 returned in ~3 minutes
elapsed (vs ~15 min serial). Each agent's `verified_exit_code`
field made the integration step purely mechanical — no guessing
whether a mutation would actually fire.

### Tests

`scripts/mutation_check.py` now contains 8 mutations. CI's
`mutation` job runs them all in ~30s. Local run confirms
`8/8 mutations caught`. After this commit deploys via CI,
`gdocs_server_info.test_suite.mutation_check.caught/ran` reports
`8/8` with `asleep_guards: []`.

## [1.2.0] — 2026-05-18

Closes the "is the gate real?" gap. Two big additions: CI-driven
deploys (so a broken commit literally cannot reach production) and
automated mutation testing (so the suite proves it catches bugs
on every build, not just once when written).

### Added

- **CI-gated deploys via GitHub Actions.** New
  `.github/workflows/deploy.yml`:
  - Triggers on push to `main` (and `workflow_dispatch`).
  - Jobs: `unit` → `mutation` → `deploy`. Each depends on the
    previous; deploy runs ONLY if both test jobs are green.
  - The `unit` job runs `pytest` + injects provenance into
    `test-results.json` (git_commit, ci_run_url from
    `$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID`,
    sha256 digest of the canonicalized payload).
  - The `mutation` job runs `scripts/mutation_check.py` — see below.
  - The `deploy` job downloads both artifacts, runs `flyctl deploy`
    with provenance build-args, runs a /health smoke check.
  - Concurrency: cancels in-flight deploys when a newer commit
    supersedes.

  **One-time setup:** the `FLY_API_TOKEN` repo secret must be set:
  ```
  flyctl tokens create deploy -a sundeepg98-docs-mcp -j | jq -r .token | gh secret set FLY_API_TOKEN
  ```
  Pipes the token directly from flyctl to gh — it never appears in
  shell history or chat.

  After this is set, every push to main runs CI; if any test fails,
  the deploy doesn't happen. `ci_run_url` in the test_suite block
  becomes a real GitHub Actions URL (replacing `"local"` for
  CI-built deploys).

- **`scripts/mutation_check.py` — automated mutation testing.**
  For each named regression guard, applies a known bug-injecting
  patch (e.g. revert `file_id: str | list[str]` → `file_id: str`),
  runs pytest filtered to that guard, asserts the guard goes red.
  If any guard does NOT catch its mutation, the build fails — that
  guard is "asleep" and can't be trusted.

  v1.2.0 ships mutations for 3 of the 8 named guards (the cleanest
  string-replace patches):
  - `test_trash_file_id_accepts_str_or_list` (file_id schema)
  - `test_deploy_webapp_body_does_not_include_entryPoints` (the
    v1.1.1 Apps Script bug)
  - `test_tool_descriptions_truthful` (the v1.1.1 docstring lie)

  Other 5 guards (`test_owned_by_app_*`, `test_inject_matches_*`,
  `test_preview_*`, `test_auth_pkce_*`, `test_tool_discoverability_*`)
  need multi-line diffs or library-quirk workarounds and are
  documented as TODOs in the script. Each one added = the gate
  gets one notch sharper. Iterative ship.

- **`test_suite.mutation_check` block** in `gdocs_server_info`:
  ```
  mutation_check: {
    ran: int,
    caught: int,
    status: "passed" | "failed" | "unknown",
    asleep_guards: [list of guards that failed to catch their bug],
  }
  ```
  Populated from `mutation-check.json` baked into the image by CI.
  `status: "passed"` requires `ran > 0 AND caught == ran`. Empty
  artifact or local-only deploy → `"unknown"`.

### Changed

- **Local `deploy.sh` is now the emergency fallback.** Primary
  deploy path is push-to-main → CI. Local `./deploy.sh` still
  works for hot-fixes that need to bypass CI (e.g. CI itself is
  broken); in that case `ci_run_url` reports `"local"` and
  `mutation_check.status` reports `"unknown"` — those values are
  the signals "this build didn't go through CI."

### Tests

+ extended `test_server_info_includes_test_suite_block` to assert
  the new `mutation_check` sub-block is always present with a valid
  status. Total: 212 unit + 5 live, all green.

## [1.1.4] — 2026-05-18

Closes the two gaps surfaced by the gdocs_test_manifest audit on
v1.1.3.

### Fixed

- **`named_regression_guards.missing` was non-empty** because two
  named guards lived only in `tests/integration/` (gated behind
  `--live`) and so didn't appear in the deploy artifact's
  test-results.json (which comes from `pytest tests/unit -q`).

  - **`test_owned_by_app_agrees_with_trash_outcome`** added as a
    new unit test in `test_soft_failure_contracts.py`. Mocks both
    the write-probe (used by `find_doc_by_title`) and the trash
    update (used by `trash_drive_file`) to share a single backing
    behavior, then asserts they agree across both the app-owned and
    external-file scenarios. Complements the existing live
    integration test (which still runs the full real-Drive E2E
    when invoked with `--live`).

  - **`test_preview_flags_what_convert_truncates`** moved from
    `tests/integration/test_title_threshold.py` to
    `tests/unit/test_preview_threshold_consistency.py`. The original
    was mislabeled as live (it took `live_creds` as a fixture but
    never used it — `preview_tab_split` runs locally for the
    `docx_path=` input mode). No live coverage lost; the test
    asserts the same contract, now in CI.

  After this, `gdocs_test_manifest.named_regression_guards.missing`
  is empty — all 8 named guards present in unit suite.

- **`ci_run_url` defaulted to `""`** which conflated "no CI run
  exists yet" with "should have been set but wasn't." Per the
  v1.1.3 spec, empty must now be reserved for "broken pipeline."
  Deploys not from CI now report `ci_run_url: "local"` explicitly.

### Tests

- New unit test (5 mocks-with-batch-callback plumbing): proves the
  find-probe and trash-update return-value relationship is
  consistent. The mock setup is more involved than typical unit
  tests because the production code uses Drive's batched-HTTP
  pattern with per-request callbacks.
- Moved test stays green in its new home.
- Total: 212 unit + 5 live (was 6 — test_title_threshold.py removed).

## [1.1.3] — 2026-05-18

Closes "verify the test_suite block isn't just a number to trust"
gap. Three additions that make the suite independently verifiable.

### Added

- **`test_suite.ci_run_url`** — link to the GitHub Actions run that
  produced this artifact. Populated by `deploy.sh` via best-effort
  `gh run list --commit=<sha>`; empty string if no run found
  (deploy ran before CI completed, gh not installed, etc.).

- **`test_suite.report_digest`** — sha256 of the canonicalized
  `test-results.json` payload (excluding the `_meta` block itself,
  chicken-and-egg). Stored in `_meta.digest` in the JSON file at
  deploy time; recomputed by the server at read time and compared.

- **`test_suite.status: "tampered"`** — new status value emitted
  when the recomputed digest doesn't match the stored one. Catches
  post-build edits to the artifact's `summary` (e.g. someone hand-
  editing the passed count). The status hierarchy is now:
  - `unknown`: artifact missing or summary empty (SKIP_TESTS path)
  - `tampered`: stored digest doesn't match recomputed digest
  - `failed`: any test failed
  - `passed`: all green AND digest verifies

- **`gdocs_test_manifest()` MCP tool** — surfaces the test inventory
  + per-test outcomes from the CI artifact. Returns:
  ```
  {
    status: "ok" | "unknown" | "tampered",
    total: int,
    tests: [{nodeid, outcome}, ...],
    named_regression_guards: {present: [...], missing: [...]},
  }
  ```
  Lets any caller confirm specific named guards (e.g.
  `test_owned_by_app_agrees_with_trash_outcome`) actually exist and
  passed — instead of trusting an opaque "203". Tool count: 20 → 21.

### Fixed

- **Lazy cwd evaluation in `_find_test_results_path`** — was
  computing candidates at module-load time, freezing the working
  directory. Caught by `test_test_suite_status_tampered_when_digest_
  mismatches` which monkeypatches `chdir`. Now evaluated at each
  call.

### Tests

- `test_canonical_digest_excludes_meta_block_and_is_stable` — same
  payload in different dict-iteration orders → identical digest;
  tampering changes the digest.
- `test_test_suite_status_tampered_when_digest_mismatches` — the
  killer guard: edit summary.passed without re-signing → server
  reports status="tampered".
- `test_gdocs_test_manifest_exists_and_returns_required_shape` —
  manifest tool returns the documented shape regardless of artifact
  presence.
- All 21 tool's `test_tool_descriptions_truthful` and
  `test_tool_input_schema_non_empty` extended to the new tool
  (gdocs_test_manifest joins no_args allowlist).

Total: 210 unit + 6 live tests, all green.

### Deferred to v1.2.0

- **CI mutation testing stage** — automated proof that injected
  regressions turn their named test red. Substantial CI workflow
  changes; separate atomic commit. The manual adversarial test
  (branch + PR #8) already proved the loop works on file_id; the
  v1.2 work is automating that across all 8 named guards on every
  build.

## [1.1.2] — 2026-05-18

### Added

- **`gdocs_server_info.test_suite` block** — surfaces CI status of
  the running build over the MCP interface. Before this, the
  CI-gated test suite existed in the repo but its pass/fail state
  was invisible to anyone using the deployed server; the only way
  to confirm "the running build was actually tested" was to re-run
  behaviors by hand — the exact toil the suite was built to
  eliminate.

  Wire-up:
  - `deploy.sh` runs `pytest tests/unit --json-report --json-report-file=test-results.json`
    via `pytest-json-report`, then injects `_git_commit` into the JSON.
  - `Dockerfile` COPIes `test-results.json` into the image (uses
    the `test-results.jso[n]` glob trick so vanilla `docker build`
    without deploy.sh doesn't fail).
  - `gdocs_server_info` reads + returns:
    ```
    test_suite: {
        last_run: ISO 8601 UTC,
        commit:   git SHA the suite ran against,
        passed:   int,
        failed:   int,
        skipped:  int,
        status:   "passed" | "failed" | "unknown",
    }
    ```
  - If the file's missing or unparseable (vanilla docker build,
    SKIP_TESTS=1, malformed JSON), returns `{"status": "unknown"}`
    per the documented contract — the field is always present.
  - `test_suite.commit` should equal the top-level `git_commit`;
    divergence means the image shipped without a matching test
    run, a red flag worth surfacing.

  Test dependency added: `pytest-json-report>=1.5` (optional;
  only used at deploy time).

  Guard: `test_server_info.py::test_server_info_includes_test_suite_block`.

## [1.1.1] — 2026-05-18

Post-1.1.0 hot-fixes from the first real cloud-chat user testing.
Each was caught in production by the user noticing mid-use; v1.1.1
adds named unit-test regression guards for every one so the next
cycle catches them in CI.

### Fixed

- **Apps Script `deployments.create` rejected the `entryPoints` field.**
  Google's API returns `Invalid JSON payload received. Unknown name
  "entryPoints": Cannot find field.` Web-app entry-point configuration
  belongs in the `appsscript.json` manifest, NOT the deployment body.
  Removed `entryPoints` from `deploy_webapp`'s request body; removed
  the now-unused `execute_as` / `access` parameters from its signature.
  Guard: `test_gas_deploy.py::test_deploy_webapp_body_does_not_include_entryPoints`.

- **Apps Script Web App manifest changed `access: MYSELF` →
  `ANYONE_ANONYMOUS`.** In single-tenant v1.0 the operator was both
  deployer and runtime caller, so `MYSELF` worked via session magic.
  In v1.1 multi-tenant cloud, the USER deploys the Web App but the
  SERVER calls it — unauthenticated. `MYSELF` would 401 every call.
  `ANYONE_ANONYMOUS` is the right setting for the v1.1 architecture.
  Surface is bounded by the script's logic (only acts on doc IDs in
  the request, only on docs the deployer owns); v1.2 will add HMAC
  request validation for defense in depth.

- **OAuth callback failed on Fly with `OAuth 2 MUST utilize https`.**
  Fly terminates TLS at the edge; inside the container `request.url`
  has scheme `http://` even though the public URL is HTTPS.
  `oauthlib.Flow.fetch_token` validates the URL and rejected any
  http://. Fixed by rewriting the scheme on the URL we hand to oauthlib
  when `base_url` begins with `https://`. Did NOT set
  `OAUTHLIB_INSECURE_TRANSPORT=1` (that disables transport security
  globally).

- **PKCE handling was non-deterministic; callback failed with
  `Missing code verifier`.** Auth URLs sometimes included
  `code_challenge` and sometimes didn't, depending on which Flow code
  path generated them. v1.1.1 makes PKCE always-on: every
  `build_authorization_url` call generates a `code_verifier` via
  `secrets.token_urlsafe(48)`, persists it server-side keyed by the
  state token's nonce (see `oauth_state._pending_verifiers`), and
  retrieves it on callback so `Flow.fetch_token` can complete the
  exchange. Guard:
  `test_oauth_google.py::test_auth_pkce_consistency_every_url`.

- **Doc-string overpromise: tools claimed to work "without setup".**
  `gdocs_setup_apps_script`'s description conflated two prerequisites:
  (a) the Apps Script Web App setup itself, (b) the base Google OAuth
  grant. Other tools don't need (a) but ALL tools need (b). Saying
  "works without setup" unqualified misled the model into trying calls
  that returned `needs_authorization`. Rewrote to distinguish the two
  grant types explicitly. Guard:
  `test_tool_schemas.py::test_tool_descriptions_truthful`.

- **`gdocs_reset_authorization` was registered but undiscoverable via
  `tool_search`.** Tool was visible in `gdocs_server_info.tools` (count
  20) but search ranker couldn't surface its schema for keywords like
  "reset authorization" / "revoke grant" / "sign out". Root cause:
  bland leading description sentence. Rewrote to embed the synonym
  set ("reset / revoke / clear stored Google OAuth credentials. Force
  re-consent.") in the first 200 chars where the ranker weighs most.
  Guard: `test_tool_schemas.py::test_tool_discoverability_via_server_info`.

### Added

- **`gdocs_reset_authorization` MCP tool.** Clears the user's stored
  Google OAuth credentials and (optionally with `full=True`) Apps
  Script setup state. Forces the next tool call back into the
  `needs_authorization` flow. Required as a recovery path AND as the
  only way to re-trigger consent for testing PKCE / scope changes /
  account switches. Per-user in cloud mode (via user_store); per-
  machine in stdio mode (deletes `~/.google-docs-mcp/token.json`).

- **Version string now embeds the git commit SHA as semver build
  metadata.** `gdocs_server_info` reports `version` as
  `f"{__version__}+{GIT_COMMIT}"` (e.g. `1.1.1+abc1234`) when
  `GIT_COMMIT` env var is set. Every deploy from a distinct commit
  reports a unique version string without requiring a manual
  `pyproject.toml` bump on every hot-fix. Per semver §10 the build-
  metadata segment is informational only and doesn't affect sort.

### Tests

+ ~40 new test cases (parametrized over 20 tools):
- `test_tool_discoverability_via_server_info` — server_info.tools
  matches mcp.list_tools() exactly.
- `test_tool_descriptions_truthful` (parametrized over 19 OAuth-needing
  tools) — no description contains "without setup" / "without
  authorization" unqualified.
- `test_tool_input_schema_non_empty` (parametrized over all 20 tools)
  — every tool's schema has properties or is on the no-args allowlist.
- `test_tab_nesting_depth_cap_enforced` — 4-level nesting raises
  ValueError before any Google API call.
- `test_auth_pkce_consistency_every_url` — 5 sequential calls all
  return URLs with code_challenge + code_challenge_method=S256, all
  with unique challenges (verifier regenerated per call).
- `test_pkce_verifier_roundtrip` (+ 2 related) — sign_state with
  code_verifier → verify_state returns it on consume; single-use;
  no-PKCE returns None for backward compat.

Total: ~200 unit + 4 live tests. CI gates deploys on unit pass via
`deploy.sh`.

### Internal

- Auto version-bump-on-deploy wired via the GIT_COMMIT build arg in
  `deploy.sh`. Every push to Fly carries a unique build identifier.
- GitHub Actions runs the full unit suite across Python 3.10–3.13 on
  every push/PR.
- `deploy.sh` runs `pytest tests/unit -q` before `flyctl deploy`;
  refuses to deploy on test failure (bypassable with `SKIP_TESTS=1`
  for emergency hot-fixes).

## [1.1.0] — 2026-05-18

**Multi-tenant cloud auth.** The remote HTTP MCP (claude.ai connector
via Fly.io) is now genuinely multi-tenant — each cloud-chat user
operates on their *own* Drive with their *own* Google identity. Before
1.1, the entire Fly deployment was single-tenant: every cloud-chat
user's tool calls implicitly used the operator's cached OAuth token,
so docs were created in the operator's Drive and the retrofit Apps
Script Web App (deployed `access: MYSELF`) was unusable for anyone
but the operator.

### New: `gdocs_setup_apps_script` MCP tool

One-shot setup for the per-user Apps Script Web App needed by
`gdocs_tab_existing_doc` (the lossless content-move path). Run once
per user; idempotent on retry (resumes from the last successful
step). Stdio mode keeps calling the v1.0 local CLI; HTTP mode
deploys a Web App into the calling user's Drive.

### Cloud architecture: "Shape C"

- **FastMCP's `GoogleProvider`** handles claude.ai connector auth
  (identity-only `openid email` scopes, but `valid_scopes` advertises
  the full Workspace union so consent grants Docs/Drive/Apps Script
  at the same time)
- **Separate auth-code flow we own** (`/oauth/google/api/callback`)
  obtains tokens we can actually use for Google API calls
- **Per-user state** in SQLite on the Fly volume, keyed by Google
  `sub`
- **Per-user lock on refresh** so two concurrent tool calls don't
  rotate each other's refresh_token

Why not just rely on `GoogleProvider` for the API tokens too: the
upstream Google tokens it holds live in private attributes
(`_upstream_token_store`, no public getter, no API contract). The
MCP spec blesses two-flow designs in the "URL Mode Elicitation for
OAuth Flows" pattern; production reference is
[`taylorwilsdon/google_workspace_mcp`](https://github.com/taylorwilsdon/google_workspace_mcp).

### New env vars (HTTP deploy)

Required when running the Fly server with the new auth wiring:

- `GOOGLE_OAUTH_BASE_URL` — public HTTPS hostname of the deployment
  (e.g. `https://my-app.fly.dev`). Must exactly match the actual URL
  or claude.ai's connector OAuth discovery silently fails.
- `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` — full OAuth client JSON inline,
  or `GOOGLE_OAUTH_CLIENT_SECRETS_PATH` pointing at a file.
- `MCP_BEARER_TOKEN` (already required) — reused as the HMAC signing
  key for the OAuth state parameter.

Auto-set by `configure_auth_for_http`:
`OAUTHLIB_RELAX_TOKEN_SCOPE=1` (so partial-grant consents don't
crash the OAuth callback).

### Breaking: existing claude.ai connector users must reconnect

The OAuth scope set is changing. Existing connector connections need
to be disconnected + reconnected in claude.ai's connector settings to
pick up the new scopes.

### Internal modules added

- `user_store.py` — SQLite per-user state (WAL mode, per-path init
  guard, merge-semantics on save, typo-rejection)
- `oauth_state.py` — HMAC-signed, single-use state-param for OAuth
  callback (CSRF + replay protection)
- `oauth_google.py` — `google_auth_oauthlib.Flow` setup, callback
  code-exchange, GoogleProvider activation
- `credentials.py` — `get_credentials_for_user(sub)` with per-user
  refresh lock, `invalid_grant` → `NeedsReauthError` mapping
- `setup_apps_script_for_user` — cloud-side variant of
  `setup_apps_script_auto` using `user_store` as the per-user ledger

### Production bug fix in user_store

The concurrent-writes test surfaced a real `PRAGMA journal_mode=WAL`
race that `busy_timeout` doesn't mitigate. Fixed via per-path init
guard under `threading.Lock` — would have fired on Fly the moment
two cloud users finished OAuth simultaneously.

### Tests

+49 unit tests across the v1.1 modules:
`test_user_store.py` (13), `test_oauth_state.py` (12),
`test_oauth_google.py` (13), `test_credentials.py` (11),
`test_setup_apps_script_for_user.py` (9),
`test_phase6_consumer_branching.py` (7),
`test_configure_auth_for_http.py` (9). Total: 158 unit + 4 live.

### Dependencies

- `fastmcp>=2.13` (was `>=2.0`) — `GoogleProvider` + `valid_scopes` +
  `_default_scope_str` post-init patch all require 2.13+.

## [1.0.1] — 2026-05-18

**Fixed: orphan Apps Script projects on setup retry.**

`setup-apps-script-auto` now persists per-step state to
`~/.google-docs-mcp/setup-state.json`. If any step in the 4-step
pipeline (create project → push files → create version → deploy webapp)
fails, the next retry resumes from the first incomplete step instead
of creating a second Apps Script project in the user's Drive.

Handles three resume scenarios:
- Same content + same impersonate user → resume from first incomplete step
- Edited `restructure.gs` (different content hash) → start fresh
- User manually deleted the script in Drive (cached script_id 404s) →
  detect and start fresh

Caught preventively via the v1.0 architecture-review pass. Without the
ledger, a user retrying after a flaky network would have accumulated
"ghost scripts" requiring manual Drive cleanup.

+6 unit tests in `tests/unit/test_setup_idempotency.py` covering cold
start, mid-step crash + resume, content-change reset, and manual-delete
recovery. Total tests: 78 unit + 4 live.

## [1.0.0] — 2026-05-18

First stable release.

**Tool surface (18 tools, all `gdocs_`-prefixed):**

- Create / convert / retrofit: `gdocs_make_tabbed_doc`, `gdocs_tab_existing_doc`, `gdocs_preview_tab_split`
- Edit tabs: `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_rename_tab`, `gdocs_delete_tab`, `gdocs_set_tab_icons`, `gdocs_replace_all_text`
- Read: `gdocs_get_doc_outline`, `gdocs_read_doc`, `gdocs_get_tab_url`
- Search / manage: `gdocs_find_doc_by_title`, `gdocs_trash_file`, `gdocs_untrash_file`, `gdocs_move_to_folder`
- Cloud-chat support: `gdocs_get_signed_upload_url`
- Server identity: `gdocs_server_info`

**Notable progression since pre-1.0 alpha:**

- Native Google Docs Tabs (Oct 2024 sidebar feature) — every tool operates at the tab level, not just outline headings.
- Both stdio (Claude Desktop / Code) and remote HTTP (claude.ai via Fly.io) transports.
- Apps Script Web App setup automated via `setup-apps-script-auto` CLI — collapses the 6-step UI dance into one OAuth consent. Opt-in `--auth-mode=service-account` adds Domain-Wide Delegation for Workspace users wanting truly headless setup.
- Signed-URL upload flow (`gdocs_get_signed_upload_url`) for claude.ai's sandbox — bypasses the bytes-via-tool-args size limit AND the Drive-connector .docx corruption issue.
- Soft-failure contracts on every mutate operation: 404 / 403 / `app_not_authorized` return as data, never raised. Batch operations skip-and-continue.
- Retrofit injects synthetic Heading 1s into styled `.docx` files with no headings (table-banner sections, etc.). Unicode-normalized + whitespace-collapsed + run-fragmentation-tolerant matching.
- 76 tests (72 unit + 4 live integration). CI runs on every push/PR across Python 3.10–3.13.
- Deploy script (`deploy.sh`) gates Fly.io deploys on local unit-test pass.
- `gas_deploy/` sub-package: clean boundary around Apps Script REST plumbing — extractable as a standalone package if a second consumer ever appears.

**Known limitations** (see README):

- `setActiveTab` (persistent default-tab setting) not exposed. Use `gdocs_get_tab_url` for per-link deep linking instead — covers the common case.
- Drive's converter 500s on `.docx` files using `<w:sym w:char="00A0"/>` for NBSP. Our retrofit handles this construct correctly in-memory; the limitation is purely Drive's. Use the literal `\xa0` character inside `<w:t>` (the form Word actually produces) instead.
- Per-tab headers/footers not supported. Tabs render as continuous scroll in the Docs UI; page headers only matter for paginated PDF export.

[1.0.0]: https://github.com/Sundeepg98/google-docs-mcp/releases/tag/v1.0.0
