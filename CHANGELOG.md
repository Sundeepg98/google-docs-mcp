# Changelog

All notable changes to `google-docs-mcp`.

This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased] — PR-α reframe + PR-Δ1 spec compliance + scope union + PR-Δ2 security posture artifacts

Combined three related ships. PR-α surfaces the runtime install as
headline functionality; PR-Δ1 bundles its scopes into the
first-consent screen and adds the spec-mandated OAuth discovery
endpoint so claude.ai's connector reconnect flow lands precisely on
the first probe; **PR-Δ2 publishes the security posture artifacts**
(RFC 9116 `security.txt` endpoint, OWASP ASVS Level 1 self-attestation,
expanded threat model with STRIDE per component + bounded blast
radius callout + honest-gaps catalog, OpenSSF Scorecard CI, and
Sigstore-signed releases) for credibility — not as a CASA substitute,
which is no longer the operational concern given Google's Testing-mode
bypass applies to our user scope.

### Added

- **`GET /.well-known/security.txt` endpoint** (PR-Δ2) — RFC 9116 machine-readable vulnerability disclosure contact, served by `src/google_docs_mcp/http_server/routes/observability.py` next to the existing `/.well-known/oauth-protected-resource` (RFC 9728) endpoint. Contact field points at the GitHub Security Advisories form (canonical channel per `SECURITY.md`). `Expires:` field hardcoded to a conservative ~6-month window so a stale image deployment still serves a valid block; the integration test `test_security_txt_expires_is_rfc3339_and_in_future` is the canary that fires when renewal is needed. Public endpoint — the `BearerTokenMiddleware` already excludes `/.well-known/*`.
- **`docs/security-posture.md`** (PR-Δ2) — human-readable narrative companion to the structured artifacts. Covers minimal-scope OAuth posture (`drive.file` primary + `drive.readonly` for explicit ingestion), per-user token storage + per-purpose HKDF key derivation, standards-compliant OAuth discovery (RFC 8414/9728/9116), the bounded-blast-radius architectural property, continuous posture monitoring (Scorecard + CodeQL + pip-audit + Sigstore), what we self-attest (ASVS L1) and what we explicitly do NOT claim (SOC 2, CASA, paid pen-test, ASVS L2/L3).
- **`docs/asvs-level-1-checklist.md`** (PR-Δ2) — control-by-control self-attestation against [OWASP ASVS Level 1](https://owasp.org/www-project-application-security-verification-standard/), V1 through V14. 53 controls assessed: 45 PASS, 4 PARTIAL (disk encryption, admin token model, full decompressed-size cap, app-side HTTP security headers), 3 N/A (MFA delegated to Google, cookies not used, SOAP/GraphQL not present), 1 OPEN (in-process rate limiting — tracked PR-Δ3). PARTIAL and OPEN markings are explicit; honest self-attestation, not check-the-box pass theatre.
- **`docs/THREAT_MODEL.md` §§ 7-9** (PR-Δ2) — supplements the original v1.3.1 surface-table threat model with: a per-component STRIDE matrix (10 components × 6 STRIDE categories); the **bounded blast radius** architectural callout (the central security property that no single server-held token / key / credential grants cross-user access — per-user OAuth tokens, per-user signed-URL `uid` binding, per-user Apps Script deploys, per-purpose key derivation, no cross-user tool paths); and the **honest "what we currently don't defend against"** section enumerating 8 open gaps with rationale + planned closure (most operationally significant: rate limiting in PR-Δ3, Apps Script HMAC verify-path in v2.0c). The §4 surface table is not updated in this PR (separate hygiene PR); §§ 7-9 are independently true and useful as-is.
- **`.github/workflows/scorecard.yml`** (PR-Δ2) — OpenSSF Scorecard CI from the canonical [ossf/scorecard-action](https://github.com/ossf/scorecard-action) template. Runs weekly + on every push to `main`; uploads SARIF to GitHub's code-scanning surface; publishes the score to the public Scorecard API so the README badge stays live. Read-only by default with explicit narrow permissions (id-token: write for OIDC attestation of the scan run, security-events: write for SARIF upload, contents: read for checkout — nothing else).
- **`.github/workflows/release.yml`** (PR-Δ2) — Sigstore-signed release artifacts via `actions/attest-build-provenance@v2`. Triggered on `release: published` (operator publishes the existing release-drafter draft when ready); builds wheel + sdist via `python -m build`, emits an in-toto Statement signed by Sigstore's Fulcio (short-lived cert tied to this workflow's GitHub Actions OIDC identity) and logged in Rekor, then uploads the signed artifacts to the release page. Downstream consumers verify with `gh attestation verify <wheel> --repo Sundeepg98/google-docs-mcp`. Separate workflow from `release-drafter.yml` so each has minimum permission scope.
- **OpenSSF Scorecard badge in README.md** (PR-Δ2) — live badge from `api.securityscorecards.dev`; clicks through to the public Scorecard viewer. Security-posture doc links (SECURITY.md, security-posture.md, THREAT_MODEL.md, asvs-level-1-checklist.md) added as a one-line callout under the badges block.
- **`gdocs_install_automation` MCP tool** (PR-α) — canonical, user-facing name for the Workspace Automation runtime installer. One-time per-user install that enables Claude to build persistent workflows in the user's Workspace: time-driven jobs, custom menus inside docs/sheets/slides, reactive automations that fire when data changes. After install, automations live in the user's account and run on Google's infrastructure without Claude in the loop. Returns the same `{status, url, script_id, deployment_id, message}` envelope as the old name; the consent and success messages now describe the capability being unlocked rather than the deployment mechanics.
- **`GET /.well-known/oauth-protected-resource` endpoint** (PR-Δ1) — RFC 9728 OAuth Protected Resource Metadata. The MCP Authorization spec mandates this path for any MCP server that exposes OAuth-protected resources; pre-PR-Δ1 the server returned 404 (verified via live `curl`), forcing claude.ai's connector discovery into less-precise fallback heuristics on reconnect. The new endpoint advertises `resource` + `authorization_servers` + the canonical `scopes_supported` list (sourced from `oauth_google.GOOGLE_API_SCOPES` so additions/removals stay in sync without a duplicate registry) + `bearer_methods_supported: ["header"]` (we deliberately don't implement RFC 6750 §2.2/§2.3 query-string or POST-body bearer presentation). Public endpoint — `BearerTokenMiddleware` already excludes `/.well-known/*`. Companion RFC 8414 endpoint (`/.well-known/oauth-authorization-server`) was already auto-wired by FastMCP's `GoogleProvider`. See `docs/adr/2026-05-27-spec-compliance-and-scope-union.md`.
- **Apps Script scopes in baseline OAuth consent** (PR-Δ1) — `script.projects` + `script.deployments` added to both `auth.SCOPES` (stdio mode) and `oauth_google.GOOGLE_API_SCOPES` (HTTP/cloud mode). Reverses the v1.x scope reduction (Issue #17): that reduction made sense when Apps Script setup was hidden infrastructure, but the PR-α reframe made `gdocs_install_automation` headline functionality and the incremental-consent moment became a UX papercut. Now users hit a single Google consent screen that covers every scope the server may ever ask for; `gdocs_install_automation` and every subsequent tool call Just Work without re-prompting. Existing users pick up the new baseline automatically on next token refresh via Google's `include_granted_scopes=true` flow — no forced re-consent (same path that handled the earlier scope additions across prior PRs). `services/gas_deploy/tools.py`'s per-tool `required_scopes=GAS_DEPLOY_SCOPES` parameter is kept verbatim; it becomes documentary since the scopes are baseline-granted, but removing it would obscure the intent at the install site.

### Changed

- **`gdocs_setup_apps_script` is now a deprecation alias** (PR-α) for `gdocs_install_automation`. The old name remains a registered MCP tool — existing user prompts, saved automations, and external integrations that reference the old name continue to work — but calling it emits a `DeprecationWarning` instructing the caller to migrate. Both tools delegate to a single shared `_install_automation_runtime()` helper in `services/gas_deploy/tools.py`; the no-divergence invariant is pinned by a structural test (`test_alias_and_canonical_share_underlying_implementation`).
- **User-facing consent + success copy reframed** (PR-α) to lead with the capability (automation runtime install) rather than the mechanism (Apps Script Web App deploy). The `needs_authorization` message reads "Install your custom Workspace automation runtime — Google will ask you to authorize the workflow installer" instead of "Google API access required to set up your Apps Script Web App." Success messages explain what was unlocked (scheduled jobs, custom menus, reactive workflows) rather than what was deployed (a Web App URL). Copy is asserted by tests so a future "let me revert this for clarity" change can't slip in unnoticed.
- **LLM_RECOVERY entry `apps_script_modified` rewritten** (PR-α) to recommend `gdocs_install_automation` for runtime re-install + use the "Workspace automation runtime" framing in the user-facing message.
- **Retrofit error message in `docx_import.py` reframed** (PR-α) — when the runtime isn't installed yet and a user hits the retrofit path, the error now reads "Workspace automation runtime not yet installed for your account. Run the gdocs_install_automation tool first…" instead of the prior Apps-Script-Web-App phrasing.
- **`gdocs_guide()` orientation surface** (PR-α) — the `setup_and_auth` group lists `gdocs_install_automation` as the canonical entry; the deprecation alias is intentionally omitted from the user-facing group so the orientation surface stays clean.
- **README + USER_GUIDE + TOOL_CONTRACT + LLM_RECOVERY** (PR-α) updated to the new canonical name. USER_GUIDE explicitly notes that the old name still works and will be removed in v3.0 (so any cached user knowledge of `gdocs_setup_apps_script` continues to find a working tool and a clear migration message).

### Deprecated

- **`gdocs_setup_apps_script`** — use `gdocs_install_automation` instead. Planned removal in **v3.0**. The alias emits a `DeprecationWarning` on every call.

### Out of scope (deferred to follow-up PRs)

- No change to the underlying Apps Script template (`restructure.gs`) — separate PR.
- No new tools beyond the rename + alias — separate PR.
- No change to `services/gas_deploy/scopes.py` (the `GAS_DEPLOY_SCOPES` constant) — same scopes, just now baseline-granted via `auth.SCOPES` / `GOOGLE_API_SCOPES`.
- No sidebar HTML / progress UI — separate PR.
- `drive.readonly` stays in baseline (an earlier draft of PR-Δ1 removed it; reverted per operator decision — Testing-mode bypass covers the current deployment, future-CASA-if-Marketplace is hypothetical). See ADR for the rationale.
- SECURITY.md / threat model / OWASP ASVS — PR-Δ2.
- Rate limiting / key rotation / pip-audit CI / HMAC constant-time verification — PR-Δ3 (hardening).

## [2.0.6] — 2026-05-20

Eight-PR consolidation wave (#78–#85). Closes the silent e2e CI gap that had been hiding integration-test + chaos-harness + pip-audit + pyright + ruff failures since v1.4.0c (PR #26): the `e2e.yml` workflow had been broken by an invalid `runner.temp` reference in job-level `env`, rejected by GitHub's validator with HTTP 422, since the day it shipped — none of the gated tests ever actually ran in CI. PR #82 fixes that; the rest of this wave is the work that landed clean once CI was actually validating it. Also lays the `@gdocs_tool` decorator groundwork for the multi-service `@workspace_tool` rename (see `docs/ARCHITECTURE.md` §7 M4).

> **Major: e2e CI gap closed.** Integration tests, chaos harness, pip-audit, pyright, and ruff now actually run on every PR — previously silently failing since PR #26. Operators relying on the green test badge as a freshness signal should treat the v2.0.6 cut as the first build where that signal carries the full e2e suite.

### Security

- **`pip-audit` severity-aware ignore for pyjwt PYSEC-2025-183 (PR #85, C4).** The pyjwt CVE (CVSS 7.0 HIGH, DISPUTED by upstream) enters via `mcp[crypto] → pyjwt` and surfaces in `pip-audit --strict` against `uv.lock`. Verified non-applicable: `grep -rn "import jwt" src/ tests/ scripts/` returns zero hits, and `mcp[crypto]`'s only consumer is `PrivateKeyJWTOAuthProvider` + `RFC7523OAuthClientProvider` (neither instantiated in our codebase — our OAuth path is Google's standard Authorization Code flow via `google-auth-oauthlib`). The vulnerable code path is never executed in our deployment. e2e workflow now ignores the CVE with a 20-line provenance comment + re-audit trigger. Full rationale in `SECURITY.md` § Dependency CVE handling.
- **OAuth callback + signed-URL roundtrip integration tests (PR #79).** Closes the security-critical e2e gap surfaced by the THREAT_MODEL §4 review: the multi-stage flows (browser → `/oauth/google/api/callback` → token exchange; `gdocs_get_signed_upload_url` → POST `/api/convert`) had unit coverage of each leg but no end-to-end test exercising the leg boundaries. New tests in `tests/integration/` cover both flows including the failure cases (replay, expired state, tampered signature).

### Added

- **`@gdocs_tool` composite decorator (PR #83, R28 5-round deferral close).** New module `src/google_docs_mcp/decorators.py` collapses the per-tool boilerplate (the `try / except HttpError → ToolError` + `_get_credentials()` + `ToolAnnotations(...)` triad) into a single decorator. Eliminates ~83 LOC of duplication across the 15 API-touching tools. The decorator is deliberately scoped to tools that opt-in via `creds=True`; the 9 local-only tools (`gdocs_server_info`, `gdocs_help`, `gdocs_guide`, etc.) keep custom shapes. Sets the pattern for the future `@workspace_tool` rename in M4 of the Hex foundation refactor (see `docs/ARCHITECTURE.md`).
- **`output_schema=` on all 24 `@mcp.tool` decorators + per-tool runtime validation (PR #80, R33 F6).** Each tool now declares its return-shape contract via `output_schema=`; the FastMCP runtime validates every tool response against the schema before returning to the caller. Closes R33 F6 + the 21 missing contract tests that had accumulated since v1.3.0's tool-surface stabilization. New module `src/google_docs_mcp/tool_schemas.py` is the single source of truth for the schemas; `test_tool_output_schemas.py` asserts every decorator carries a schema (regression guard for "added a new tool, forgot the schema").
- **Coverage gate (PR #78, R33 floor + ratchet policy).** `pytest-cov` wired into the e2e workflow with a 55% floor (lowered from the initial 56% target after CI revealed a Py 3.11 outlier at 55.21% — see `docs/COVERAGE.md` § "Why 55% and not 56%"). Each subsequent release ratchets the floor by +1pp until coverage stabilizes. `docs/COVERAGE.md` documents the ratchet policy, the per-module exemptions, and the rationale for not chasing 100%.

### Changed

- **pyright + ruff wired into e2e workflow (PR #84).** `pyright` runs strict mode against `src/` + `tests/`; `ruff` runs `check + format --check`. Surfaced and fixed 7 real type issues + 2 ruff violations on landing. `tests/` migrated from inline `# noqa` comments to the per-file ruff `tests/` override. Pinned `astral-sh/setup-uv` from the floating `@v3` to the immutable `@v8.1.0` SHA-anchored ref (caught in post-landing fixup: no `v8` major alias exists; `v8.1.0` is the actual release that the workflow exercises).
- **Integration test fixture str → bytes (PR #81, PR #34 A.1 contract).** `test_fresh_user_flow.py` fixture was passing the signing key as `str`; the v2.0b strict-flip changes the signing key contract to `bytes` (HKDF derivation output). Fixture updated to match. 4 of the 5 integration tests now pass under the strict-flip; the 5th was resolved in the follow-up v2.0.7 fix-pack (see `ship/fix-fixture-dual-type`).

### Fixed

- **`e2e.yml` workflow `runner.temp` rejection (PR #82, broken since PR #26).** GitHub's workflow validator was rejecting the job-level `env: COVERAGE_TMP: ${{ runner.temp }}/cov` block with HTTP 422 — `runner.*` context is not available in job-level `env`, only in step-level `env`. The workflow had failed validation on every run since v1.4.0c shipped (commit `35fdb01`, 2026-05-19), but the failure mode was a silent "workflow did not run" status that didn't surface in the PR check list. **Until this fix, integration tests / chaos harness / pip-audit / pyright / ruff had never validated a single PR.** Moved the `runner.temp` references into step-level `env` blocks; e2e now runs end-to-end on every push. Root-cause comment added to the workflow header so future contributors don't re-introduce the same shape.
- **Fly internal probe TrustedHostMiddleware allowlist (PR #77).** Out-of-band v2.0.6 hotfix landed first to unblock v78+ deploys: Fly's internal health probes use hostnames that weren't covered by `derive_trusted_hosts()`. Allowlist now includes the Fly-internal `*.flycast` + `fly-local-6pn` patterns alongside the existing `*.fly.dev` + `localhost`. Per-test fence in `test_derive_trusted_hosts_fly_internal`.

### Tests

- **OAuth + signed-URL integration coverage** (PR #79, see Security above).
- **24 contract tests for `output_schema=`** (PR #80, see Added above). `test_tool_output_schemas.py` asserts schema presence + runs a smoke validation against a representative successful response for each tool.
- **Integration test fixture migration** (PR #81, see Changed above).

### Documentation

- **`docs/ARCHITECTURE.md` landed** — rationale for the Hex/Ports/Adapters foundation refactor underway (M1a in flight). Documents the 4 promoted ports (StorageBackend [proven], GoogleAPIClient, KeyProvider, CredentialStore), the 2 NOT promoted (HTTPServer, UrlSigner) with YAGNI rationale, the per-service folder pattern (inspired by `taylorwilsdon/google_workspace_mcp`), the M1a → PAUSE → M1b → M2 → M3 → M4 sequencing, and the research-agent provenance for the corrections that landed.
- **`docs/COVERAGE.md` landed** (PR #78, see Added above) — coverage floor + ratchet policy + per-module exemptions.
- **`SECURITY.md` extended** — adds the dependency-CVE handling section (motivated by the pyjwt PYSEC-2025-183 non-applicability finding in PR #85), the threat-model pointer, and the supported-versions clarification.

### Audit-trail provenance

- **R28** (5-round deferral) finally closed by PR #83's `@gdocs_tool` decorator. The deferral had been "yes, but the right shape is unclear" since v1.4.2; the v2.0.6 round nailed down the shape (creds=True opt-in + per-tool ToolAnnotations preserved) and shipped it.
- **R33 F6** closed by PR #80's `output_schema=` pass + 21 contract tests. The finding had documented the missing schemas but deferred the fix to "after we lock the tool surface"; v2.0.x's stable surface unblocked it.
- **C4** (CVE handling rigor) closed by PR #85's pyjwt provenance comment. The earlier `# TODO: figure out pyjwt CVE` comment in `e2e.yml` had been stale since v2.0.5; the comment now documents WHY the ignore is safe, plus the re-audit trigger ("if we ever wire `PrivateKeyJWTOAuthProvider` or `RFC7523OAuthClientProvider`, re-evaluate").
- **R26 + R28 nit** — the CI green status before PR #82 was a known-unknown: tests claimed to be running but the workflow validator failure was invisible in the PR check list. Documented as a runbook entry in `docs/RUNBOOK.md` so the next workflow change that fails GitHub validation gets caught earlier.

## [Unreleased] — v2.0.5

Parallel-shipping wave from a 29-round audit cycle (R1–R29 + ongoing peer review). Eight independently-reviewed PRs, each a self-contained finding with its own regression test. Bundled here as v2.0.5; the per-PR commit messages will also land in release-drafter's auto-draft, so the GitHub Release will carry both this curated summary and the per-PR detail. No user re-consent required; no tool-surface break; no schema change.

Companion in-flight items — distinct version targets, called out individually so naming-by-version-cluster doesn't bury PR #57:

- **A1 `/api/convert` multi-tenancy** (#60, targets **v2.1.0**) — signed-URL canonical string bound to user_id; per-user creds resolution at the endpoint. The version bump (v2.0.x → v2.1.0) reflects the contract-level change to the signed-URL format.
- **B1 v14 keys.get_key() wire-up** (#57, targets **v2.6** — separate version target) — closes the long-standing keys.get_key() bypass class flagged by R7→R20. Unblocks PR #34 (v2.0b HKDF strict-flip). Stranded by an earlier coding session; R20 ground-truth caught the un-pushed branch.
- **v2.0b HKDF strict-flip** (#34, targets **v2.0.0 (post-soak)** — see the named-version block below) — removes the `_BACK_COMPAT_RAW_MASTER` shim. Ships after operator preflight-soak passes.

### Security

- **Reflected XSS on `/oauth/google/api/callback` error page (PR #50).** `_error_page` now escapes the user-controlled `?error=` query param via `html.escape()` before rendering. The local `html` name was aliased to `_html` to avoid shadowing the stdlib module. Exploitable in production prior to this fix — operators on v2.0.4 or earlier should treat this as the headline reason to upgrade. Regression test: `test_oauth_error_param_escaped`.
- **README access-level lie + broken auth-recovery CLI reference (PR #52, N1+N2).** README:162 stated the Apps Script Web App deploys with `MYSELF` access; the actual `_MANIFEST` deploys `ANYONE_ANONYMOUS`. Fixed the README to match code reality so threat-model readers don't underestimate exposure. Separately, `errors.py` mapped `invalid_grant` failures to a recovery message that pointed users at a `google-docs-mcp auth` CLI subcommand that does not exist; corrected to the actual remediation. Both classes now fenced by claim-vs-code regression tests.
- **CI supply-chain hardening (PR #51, A1).** `superfly/flyctl-actions/setup-flyctl` SHA-pinned to `ed8efb3` (= the master ref past the v1.5 release, captured as an immutable SHA so a future tag-force-push can't backdoor our deploy step). `dependabot.yml` now blocks fastmcp major bumps because of the CVE-floor pin from CVE-2025-69196 + CVE-2026-27124 — auto-bumping would silently re-open the floor. The `preflight_strict_flip.sh` TTL string was also corrected from a misleading "24h default" to the actual `10min default / 1h max` values, so operators reading the script header don't oversleep the cutover window.
- **HMAC fiction hedge across 8 documentation sites (PR #53, C4).** Eight sites in `docs/THREAT_MODEL.md`, `docs/MIGRATION_v1_to_v2.md`, `docs/TOOL_CONTRACT.md`, and `scripts/migrate_existing_users.py` previously claimed v2.0a's `apps_script_hmac_key` provides HMAC per-request validation on the Apps Script Web App `/exec` surface. Code reality (verified 7+ audit rounds): `restructure.gs` has zero `Utilities.computeHmacSha256Signature`; `_call_webapp` does not sign; the column is stored-and-unused at runtime. Every site now states "schema only in v2.0a; verify-path deferred to v2.0c" and notes THREAT_MODEL §4 row 5 remains OPEN. The actual HMAC verify-path is multi-week TIER 2 work targeting v2.0c. New CI guard `test_threat_model_claims_match_code` couples the doc claims to the code reality in both directions — it flips red when HMAC actually lands, forcing the hedges to be removed at the same time.
- **CSP header on OAuth callback responses (PR #74).** Defense-in-depth alongside PR #50's reflected-XSS escape. The success / error pages rendered by `/oauth/google/api/callback` now ship a `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'` header. Even if a future regression reintroduces an unescaped sink, a strict CSP prevents `<script>` execution in any compliant browser. `'unsafe-inline'` is scoped to `style-src` only because the page uses inline `<style>` blocks; no inline scripts, no remote loads.
- **CI third-party-action floating-ref guard allowlist extended to `github/` (PR #73).** The `test_no_floating_third_party_action_refs` lint was rejecting `github/codeql-action/*@vN` (GitHub's own first-party actions) as if they were third-party. Allowlist now exempts the `github/` org. The prefix-spoof guard (lookahead) still rejects `github-fake/*`, `github-malicious/*`, etc., so the spoofing protection that motivated the original lint is preserved.

### Added

- **`ToolAnnotations` on all 24 `@mcp.tool` decorators (PR #55, F1).** Each tool now carries explicit `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, and `title` annotations. Per MCP spec, clients can use these hints to decide which tools to gate behind a user-confirmation prompt; the 10 tools marked `readOnlyHint=True` now flow through ChatGPT and similar clients without the per-call confirmation interstitial. No surface change for FastMCP / Claude clients that ignored the missing annotations.
- **ChatGPT non-support note in README (PR #55, F2).** Deep Research mode is explicitly unsupported because OpenAI's ChatGPT integration requires tools literally named `search` and `fetch`; this MCP exposes `gdocs_find_doc_by_title` and `gdocs_read_doc` instead. Documented so operators know not to wire this MCP into Deep Research and expect it to work.
- **`google_clients.py` wrapper module (PR #48, v2.6a).** Single import surface for `googleapiclient.discovery.build`. Pure passthrough at landing time (no call sites migrated in this PR); enables future wholesale client-swap (e.g. swapping in a retry-wrapper, a per-call logger, a test double, or a cache without re-editing every consumer) and centralizes the chokepoint for the TID251 lint that blocks bare `build` imports outside this file. See **Changed** below for the PR2 series that migrates the consumers.

### Changed

- **PR2-A — `docs_api.py` migrated to `google_clients.get_service` (PR #70, v2.6b).** Pure call-site refactor — `build(...)` → `get_service(...)` at 2 sites; no surface change for any consumer. First of the PR2 series that walks the wrapper-bypass class down to zero exemptions.
- **PR2-B — `drive_api.py` migrated to `google_clients.get_service` (PR #71, v2.6b).** Security-sensitive surface (Drive ACL operations: trash/untrash/move/permissions); shipped atomically per the hybrid migration strategy so the diff is easy to review against the security model. 9 call sites; same `get_service` signature; identical wire shape.
- **PR2-C — bundle migration of 4 remaining files (PR #75, v2.6b).** `docx_import.py`, `preview.py`, `retrofit.py`, and `gas_deploy/client.py` migrated in one PR (1 mechanical call site each; bundling reduces review surface without losing review value). After PR2-C, `google_clients.py` is the **sole** TID251 exemption in `pyproject.toml`; any future bare `from googleapiclient.discovery import build` outside that file is a lint failure. **Closes the wrapper-bypass class.**

### Fixed

- **`_cmd_setup_auto` swallows traceback (PR #54, F3).** The setup-apps-script-auto CLI subcommand previously printed only `str(e)` on failure, hiding the chained exception cause and making "setup failed somehow" tickets unactionable. Now uses `traceback.print_exc(file=sys.stderr)` so operators debugging setup failures see the full chain. Regression test fences the call against future stripping.
- **Conftest worktree-vs-installed-package shadow (PR #69).** Local `pytest` runs inside a worktree were silently importing the main checkout's `google_docs_mcp` via `pip install -e .` instead of the worktree's `src/`. `tests/conftest.py` now prepends the worktree `src/` to `sys.path` at collection time, so tests always exercise the code under review. Mirrors the workaround surfaced by the prior session's "16 local failures vs CI green" investigation; that pattern is now the default.
- **`gdocs_admin_audit` `title` annotation corrected (PR #72, R28 nit).** The R28 peer-review of PR #55's annotation pass noticed the `gdocs_admin_audit` tool advertised `title="List Users"`, which described a tool the MCP doesn't have. The actual tool produces a forensic timeline over `user_state` rows; `title` is now `"Audit User-State Forensic Timeline"`, matching the docstring.

### Tests

- New regression tests landing across the 8 PRs (each PR fences its own change):
  - `test_oauth_error_param_escaped` (PR #50) — asserts the OAuth error-page output contains no unescaped `<script>` after a crafted query param.
  - `test_readme_access_level_matches_manifest` + `test_error_recovery_references_real_cli` (PR #52) — claim-vs-code couplings preventing README and `errors.py` from drifting again.
  - `test_threat_model_claims_match_code` (PR #53) — pairs every aspirational HMAC claim with a status-hedge keyword in the same atomic unit (table row or prose paragraph) AND asserts `restructure.gs` still has no `computeHmacSha256Signature`. Flips red the moment v2.0c verify-path lands. Tightened from the original 500-char byte window per R28 peer-review.
  - `test_setup_auto_prints_full_traceback` (PR #54) — captures stderr and asserts a `Traceback` line is present after a synthetic failure.
  - `test_tool_annotations_populated` (PR #55) — iterates every `@mcp.tool` decorator and asserts the 5 annotation fields are set (catches "added a new tool, forgot the hints" regressions).
  - **R23 B2 async exception-handling guard (PR #58)** — fences the async error-handling robustness path so a future refactor that swallows a coroutine exception trips CI.
  - **R23 B3 `isolated_db` fixture consolidation (PR #59)** — 8 copies of the per-test SQLite-isolation fixture were collapsed into a single canonical version in `tests/conftest.py`. Pure refactor; no behavior change. Removes the drift risk where 8 copies could diverge silently.

### Documentation

- **`docs/PRIVACY.md` landed (PR #44).** End-user privacy attestation grounded in the actual `user_state.py` schema (every column documented with sensitivity tier + retention policy), GDPR/CCPA notes, breach commitment, and operator-vs-maintainer data-controller separation. The R29 hedge for §1.1 (operator-secret stripping closed in v2.0.3 via PR #47) and the HMAC-fiction hedges at §1.23 + §5.60 are included so the PR #53 CI guard runs clean post-merge. Promoted from the in-flight reference in R28's audit-trail row.

### Dependencies

- **Dependabot floor-bump batch (PRs #61–#68).** Routine floor-version bumps for app dependencies (`requests ≥ 2.34.2`, `starlette ≥ 1.0.0`, `google-api-python-client ≥ 2.196.0`, `google-auth-oauthlib ≥ 1.4.0`, `pyjwt ≥ 2.12.1`) and GitHub Actions (`actions/upload-artifact v7`, `actions/github-script v9`, `release-drafter v7`). Per-package detail lives in the GitHub Release auto-draft assembled by `release-drafter` (PR #41's config); this entry exists so a human scanning CHANGELOG sees the dependency-housekeeping wave at a glance without enumerating each bump.

### Audit-trail provenance

Backlog rationalization across R17–R29 trimmed the candidate set down to what actually shipped here:

- **R17** invalidated F10 — the proposed pattern was mis-identified as a peer of an existing finding; no real bug.
- **R18** downgraded R13 D2 — the Salesloft-Drift analogy didn't transfer to a 5-user-scale deployment.
- **R18** invalidated F1-Fernet — a single-machine SQLite deployment has no key-data separation boundary, so Fernet-at-rest would be theatre.
- **R20** confirmed B1 v14 Task 1 was stranded — branch was review-ready but never pushed; PR #57 pushes the stranded branch as-is (targets v2.6, not this bundle).
- **R21** corrected B4 — already shipped via PR #49; the audit had read a stale local checkout. Re-verified in R29 cross-check: current `Dockerfile` uses `COPY --from=ghcr.io/astral-sh/uv:0.5.0` + `uv sync --frozen --no-dev --no-editable` with an explicit "R20 attack #4 mitigation" comment.
- **R23–R26** fresh-eyes audits on `retrofit.py`, `cli.py`, `setup_state.py`, and `resources.py` confirmed no missed HIGH-severity findings in those modules.
- **R28** peer-review of PR #53 tightened the CI guard from a 500-char byte window to atomic-unit (table-row / prose-paragraph) coupling + a HMAC+AppsScript co-occurrence catch-all; also folded in `docs/PRIVACY.md` for auto-activation when PR #44 merges.
- **R29** peer-review of this CHANGELOG block surfaced 4 items: 2 valid (this commit addresses Item 1: B1 explicit naming + v2.0.6 vs v2.6 disambiguation; Item 4: header date-format is correct for `[Unreleased]` and gets the date at release-cut), 2 dismissed after orchestrator cross-check against current main (Item 2: B4 already-shipped claim is accurate per re-verification above; Item 3: SHA pin `ed8efb3` matches the post-#51-merge `deploy.yml`).
- **R30** verified PR #34 (v2.0b strict-flip) is READY pending operator soak — code change is 1 substantive line (`_BACK_COMPAT_RAW_MASTER = frozenset()`); CHANGELOG block for `[2.0.0] — TBD (post-soak)` sits below this `[Unreleased]` block as a separate named-future-version entry per Keep a Changelog ordering.

## [2.0.0] — TBD (post-soak)

### BREAKING

- Removed `_BACK_COMPAT_RAW_MASTER` shim from `keys.py`. All 3 derived
  keys (`api_bearer`, `oauth_state`, `signed_url`) now use HKDF-SHA256
  derivation by default. Operators can still pin individual purposes
  via `MCP_API_BEARER_KEY` / `OAUTH_STATE_SIGNING_KEY` /
  `SIGNED_URL_SIGNING_KEY` env vars (per v1.5.1).

  **Impact:** every in-flight signed URL + OAuth state token minted
  under v1.x simultaneously invalidates at deploy. Mitigated by:
  - Pre-flight script (`scripts/preflight_strict_flip.sh`) verifies
    zero shim hits before flip
  - In-flight tokens have hard 1-hour TTL ceiling — wait 1h30min-2h
    post-deploy of v1.5.x before running v2.0b deploy

  See `docs/RUNBOOK.md` §3.5 (recovery if symptoms surface) + §3.6
  (preflight procedure).

- **`MCP_BEARER_TOKEN` must be ≥32 chars.** The pre-flip shim path
  had no length check and silently accepted shorter values (the
  legacy 16-char form documented in v1.0–v1.3); HKDF derivation has
  a 32-char minimum and refuses shorter masters at the first
  ``keys.get_key()`` call with the existing
  ``MCP_BEARER_TOKEN must be ≥32 chars`` error message. Operators
  on a token shorter than 32 chars MUST rotate to a longer value
  BEFORE flipping (typically: `python -c "import secrets;
  print(secrets.token_hex(32))"` and set as the new
  ``MCP_BEARER_TOKEN`` + each ``*_KEY`` override). The preflight
  script does not directly check master length; the next bearer-
  authed request after deploy raises if the master is short.

- **Signing keys propagate as `bytes` end-to-end** (was `str` under
  the shim). ``keys.get_key()`` has always returned ``bytes``; the
  shim-era consumers performed a ``.decode("utf-8")`` →
  ``.encode("utf-8")`` round-trip across ``http_server.py``,
  ``server.py``, ``oauth_google.py``, ``crypto.py``, and
  ``oauth_state.py``. The decode worked because the shim returned
  the operator's UTF-8 master verbatim; HKDF returns 32 random
  bytes which fail UTF-8 decoding for ~99.96% of master values.
  This release drops the round-trip: ``signing_key`` parameters
  are typed ``bytes`` throughout the chain and flow directly into
  ``hmac.new()``. The bearer-header comparison in
  ``BearerTokenMiddleware`` now uses ``hmac.compare_digest`` on
  bytes (was f-string equality on ``str``) — semantically
  equivalent for operator-set overrides, correct for HKDF output.

  External consumers calling ``crypto.sign_upload_url``,
  ``crypto.verify_signed_params``, ``oauth_state.sign_state``,
  ``oauth_state.verify_state``, ``credentials.get_credentials_for_user``,
  ``oauth_google.build_authorization_url``, or
  ``oauth_google.exchange_code_for_credentials`` directly (rather
  than via the production wire-up) must pass ``bytes`` for
  ``signing_key`` — typically ``my_str_key.encode("utf-8")``.

- **`BearerTokenMiddleware.__init__` now takes ``bytes`` for both
  ``bearer_token`` and ``signed_url_key``** (was ``str``). Same
  rationale as above; mirrors the production callsite
  (``keys.get_key()`` returns bytes natively).

- **OPERATOR FOOT-GUN — bearer header bytes (R31).** If you SKIP the
  per-purpose overrides (RUNBOOK §3.6 step 1) and let HKDF derive
  the bearer key from your master, ``keys.get_key("api_bearer")``
  returns 32 HKDF-derived random bytes. Those bytes are intentionally
  non-printable and most HTTP clients (curl, requests, fetch) cannot
  submit them as ``Authorization: Bearer <value>`` — the bearer
  header path will reject every request, even though the server
  itself boots fine and ``/health`` keeps returning 200. Operators
  who skip overrides strand their own clients. Mitigation: set
  ``MCP_API_BEARER_KEY`` to a printable UTF-8 string (typically the
  current ``MCP_BEARER_TOKEN`` value) BEFORE flipping. The override
  path keeps the bearer header UTF-8-safe; the HKDF-direct path
  does not. Same applies to ``OAUTH_STATE_SIGNING_KEY`` /
  ``SIGNED_URL_SIGNING_KEY`` if any external client needs to mint
  state tokens or sign URLs out-of-band (rare; production usually
  has the server itself do both, so HKDF bytes are fine internally).

### Fixed

- **Latent production crash class surfaced + closed:** 5 production
  sites (`server.py:1872`, `http_server.py:239`/`:672`/`:673`,
  `oauth_google.py:194`) did ``keys.get_key(...).decode("utf-8")``.
  Under the shim this was a pointless round-trip; under HKDF it
  would have crashed every OAuth callback, signed-URL mint, and
  bearer-auth request with ``UnicodeDecodeError`` for ~99.96% of
  operator deployments. R30 audit caught this during PR #34's
  rebase + Option A.1 fixture investigation; ship state pre-A.1
  would have broken v2.0.0 on first call. See R30 row in the
  ``[Unreleased] — v2.0.5`` audit-trail provenance.

- **Latent bypass-pattern gap closed:** `http_server.py:420`
  (signed-URL convert endpoint resolving per-user creds) read
  ``os.environ.get("MCP_BEARER_TOKEN", "")`` directly — a
  comma-default form that PR #57's ``_BYPASS_PATTERNS``
  architectural guard missed. The site now routes through
  ``keys.get_key("oauth_state")`` and the guard's pattern list is
  widened to catch ``"<env>", "<default>"`` shapes so future
  refactors of this class can't reintroduce the bypass.

- **Preflight gate** (`scripts/preflight_strict_flip.sh`, R32) —
  replaced the over-strict ``TOTAL >= 100`` threshold with a
  ``TOTAL >= 3 AND each_purpose >= 1`` gate. The 100-call floor
  was set against a wrong mental model (assumed per-request
  ``get_key()`` calls); the actual wrapper architecture resolves
  each purpose ONCE at process init and caches the bytes, which
  is the correct shape for a key-derivation wrapper (HKDF on every
  request would burn CPU for zero value). The old gate was
  unsatisfiable in steady state — a healthy boot produces
  ``{api_bearer:1, oauth_state:1, signed_url:1}`` = 3 total and
  stays there indefinitely without synthetic traffic. The new
  per-purpose check directly proves wire-up of each callsite
  (what the 100-call floor was actually trying to test) AND
  catches a wire-up regression in any single purpose that would
  otherwise hide behind other purposes' counters. Smoke-tested
  against synthetic ``/info`` shapes for all 4 gate paths
  (exit-0 happy, exit-3 wire-up, exit-3 total-too-low, exit-4
  shim-active). Exit-code semantics for code 3 widened from
  "insufficient signal" to "wire-up regression"; RUNBOOK §3.6
  guidance about "drive synthetic traffic" no longer applies and
  is implicitly dropped.

## [1.5.0] — 2026-05-19

Pre-v2.0b instrumentation. Process-local counter in `keys.py` measures
the actual blast radius of the back-compat shim path before the v2.0b
strict-flip removes it. No user-facing surface change; pure additive
telemetry. Commit `beefdea` (PR #27).

### Added

- **`keys.py` shim-hit counter** — process-local
  `_BACK_COMPAT_RAW_MASTER` hit counter per purpose (`api_bearer`,
  `oauth_state`, `signed_url`). Increments every time a caller
  resolves a key via the legacy raw-master path instead of an
  explicit derived override. Zeroed at process start; not persisted
  (the v2.0b decision is on the rolling delta, not lifetime totals).
- **`gdocs_server_info().key_back_compat_shim_active_hits`** —
  surfaces the per-purpose counter over MCP so operators can verify
  zero active usage before flipping the strict default. Same shape
  contract as the rest of the `server_info` payload (always present,
  defaults to `{api_bearer: 0, oauth_state: 0, signed_url: 0}`).

### Tests

- `test_keys.py` — 9 new cases covering counter increment per
  purpose, isolation across purposes, override-path not incrementing
  the counter, fresh-process zero-state.
- `test_server_info.py` — surfaces-via-server_info assertion +
  shape contract (key always present).

### Why this is a separate release vs. bundled with v2.0b

v2.0b's strict-flip is a destructive change for anyone still on the
shim path. Shipping the observability first means we let it soak on
Fly for 3 days, watch the counter, and only flip when the rolling
delta is zero. The soak period is the whole point of separating these
two releases.

## [Dependency floor bumps] — 2026-05-19 (deps batch, dependabot)

Verified safe by the dependabot-verify agent (`uv sync --frozen` +
`pytest tests/unit/` per individual bump). No code edits required;
declared floors raised to match what `uv.lock` already resolved to.

- `fastmcp ≥ 3.3.1` (was `≥ 2.13`, MAJOR `2 → 3`) — `c9f133d`. The
  lockfile already carried `3.3.1`; this just raises the declared
  floor so v1.3.x users upgrading get the version the test matrix
  actually exercises.
- `typing-extensions ≥ 4.15.0` (was `≥ 4.6`) — `4bcb27d`. Floor lift
  only; no API used from the 4.6 → 4.15 delta.
- `pytest ≥ 9.0.3` (was `≥ 8.0`, MAJOR `8 → 9`) — `90ea96e`. CI suite
  green on 9.0.3 across all four Python versions.
- `markdown-it-py ≥ 4.2.0` (was `≥ 3.0`, MAJOR `3 → 4`) — `9b52373`.
  The 4.x renderer-api break does not touch our usage (`MarkdownIt()`
  + `parse`/`render` only).
- `google-auth ≥ 2.53.0` (was `≥ 2.0`) — `58bd3be`. Floor lift only.

## [2.2b] — 2026-05-19

LLM_RECOVERY artifacts: a dedicated recovery doc + an MCP resource +
a `gdocs_help` tool that lets an agent look up the right next action
when it sees an opaque failure response from any other tool. Tool count
goes 22 → 23.

### Why

Agents that hit a known-failure shape (e.g.
`{"error": "needs_authorization", ...}`) had no in-protocol way to
discover the canonical recovery sequence — they re-derived it from
training data or hallucinated. v2.2b makes the recovery catalogue
addressable both as a resource (`gdocs://error-recovery`) and as a
tool (`gdocs_help`) so it travels with the server.

### Added

- **`docs/LLM_RECOVERY.md`** — the recovery catalogue. One section per
  known failure key, each section names the recovery tool + minimal
  kwargs to retry. Single source of truth for the resource + tool.
- **`src/google_docs_mcp/resources.py`** — exposes
  `gdocs://error-recovery` via `@mcp.resource`. Import is load-bearing
  at server-init time (registers the decorators); commented as such
  in the module.
- **`gdocs_help` MCP tool** — zero-arg-default; pass a real failure
  response and the tool case-insensitively substring-matches it
  against the catalogue and returns the matching recovery entry.

### Changed

Per post-merge review, 4 of the 9 documented failure-shape patterns
matched no real tool output (kwargs mis-spelled, wrong enum values,
dict-vs-JSON-string mismatch). Each pattern is now pinned by a
round-trip test that feeds a real failing tool response through
`json.dumps` → `gdocs_help` and asserts the match — so future
documentation drift is caught at CI, not by an agent in production.
`apps_script_modified` is marked planned-v2.0 because its surface
hasn't shipped yet.

### Tests

`tests/unit/test_llm_recovery.py` — 9 round-trip cases (one per
catalogue key) plus tool-surface tests. `gdocs_help` joins the
no-args allowlist in `test_tool_schemas.py`. Tool count summary
line updated 22 → 23.

## [2.2a] — 2026-05-19

Pure-docs batch from the audit deliverables. No source changes; no
behavior change.

### Added

- **`docs/THREAT_MODEL.md`** — key inventory + 8-row threat table
  (asset → adversary → control → residual risk).
- **`docs/RUNBOOK.md`** — 7 outage classes with named diagnostic
  sequences (OAuth-loop, mass-401, Apps-Script-403, signed-URL-replay,
  user_state.db corruption, Fly disk-full, claude.ai-connector-disconnect).
- **`docs/TOOL_CONTRACT.md`** — versioning policy + per-tool entries
  for the 22-tool surface as of merge.
- **`CONTRIBUTING.md`** — local dev workflow (uv sync, pytest layout,
  branch naming, commit message format).

Closes #14.

## [2.0a] — 2026-05-19

Migration prerequisites for v2.0b's strict-flip. Ships the per-user
HMAC-key column + a one-shot backfill CLI. v2.0b (not yet merged) will
flip `apps_script_hmac_key` from optional to required and switch the
Apps Script Web App from anonymous to HMAC-signed requests.

### Added

- **`apps_script_hmac_key`** field in `user_store`:
  `_PERSISTENT_FIELDS`, `_FIELD_VALIDATORS` (registered as a
  validator entry — uses the v1.4.0a registry), schema column, and
  an idempotent `ALTER TABLE` for in-place upgrade of existing
  databases.
- **`scripts/migrate_existing_users.py`** — backfills legacy rows
  with a freshly minted `secrets.token_hex(32)` key. Default is
  **dry-run**; writes require explicit `--apply`. Refuses to run if
  any row has `updated_at` within the last 60s (heartbeat-as-liveness
  check) so it can't clobber refresh writes from a live server;
  `--force` skips this check for cold-DB emergencies.

### Changed

- `user_store._ensure_initialized` now uses `PRAGMA table_info` to
  detect existing columns instead of substring-matching SQLite's
  ALTER TABLE error message — error strings vary by SQLite version
  and locale.
- `_user_lock` docstring trimmed to honestly state in-process-only
  scope (no cross-process serialization claim).

### Tests

`tests/unit/test_migrate_existing_users.py` — 13 cases covering
dry-run default, `--apply` writes, heartbeat refusal, `--force`
override, partial-row tolerance, idempotency on re-run.

Closes #13. v2.0b strict-flip + Apps Script HMAC verification
remain to ship; this PR is the prerequisite, not the cutover.

## [1.4.0] — 2026-05-19

Defense-in-depth + adoption + test-infrastructure release. Bundles
four independently-reviewed PRs (v1.4.0a, v1.4.0b, v1.4.0c,
v1.x-scope-reduction). No user re-consent required — the scope
reduction is forward-compatible (existing grants still work; new
users see a smaller consent screen).

### Added

- **`user_store._FIELD_VALIDATORS`** registry — per-field validator
  dict invoked by `save_state` (raises `ValueError` before SQL touches
  disk) and `get_state` (drops invalid persisted values + logs
  WARNING). Initial entry: `_valid_gas_url` for `apps_script_url`,
  accepts only `https://script.google.com/macros/s/<deploymentId>/(exec|dev)`
  — rejects `http://`, look-alike hosts, malformed paths, and
  non-string values. `None` still clears a validated field;
  non-validated fields write through unchanged. Strict hostname match
  (post-review tightening): suffix-match `.google.com` is gone, so
  `apps.google.com` / `mail.google.com` / `attacker.script.google.com`
  are all rejected (the downstream `urlopen` carries OAuth credentials,
  so any other `google.com` subdomain is dangerous). Commits `45814ae`.
- **`tests/integration/test_fresh_user_flow.py`** + **`test_migration_upgrade_path.py`**
  — joint coverage of OAuth dance + persistence + schema-upgrade
  paths. Wires `user_store` + `oauth_google` + `credentials` end-to-end
  with Google's token endpoint mocked at the `Flow.from_client_config`
  boundary (matches existing `test_oauth_google.py` pattern). Commit `0b8f248`.
- **`tests/chaos/run_chaos.py`** — standalone argparse-driven CLI
  (`--scenarios all --max-duration 60s --json-output X.json`).
  Scenario S1 = concurrent `user_store` saturation (16 workers,
  read-modify-write loop, p99 latency budget, post-run integrity
  verification). Emits JSON for CI consumption; non-zero exit on
  failure. S2 / S3 stubbed as placeholders. `tests/chaos/chaos_plan.md`
  documents the catalogue + debug commands. Commit `0b8f248`.
- **`.github/workflows/e2e.yml`** — 4-job CI workflow sister to
  `test.yml` (which keeps the unit-only Python-version matrix).
  `e2e-test` runs `tests/integration/` with JSON artifact; `chaos-test`
  runs the harness with `continue-on-error` (transient p99 blips
  don't block merge); `security-audit` runs `pip-audit --strict`
  against the locked deps (HIGH/CRITICAL CVEs fail the build);
  `lint` runs pyright + ruff. All jobs use `uv sync --frozen`
  (R20 attack #4 mitigation) and the `security-audit` job uses
  `uv export --frozen` for the same reason. `concurrency` cancels
  in-flight runs on the same ref; `permissions: contents: read`
  honors least-privilege. Commit `35fdb01`.

### Changed

- **`GOOGLE_API_SCOPES` no longer includes `script.projects` +
  `script.deployments` by default.** Pure-runtime users (who never
  run `gdocs_setup_apps_script`) no longer see the "manage your Apps
  Script projects" checkbox on first consent — a measurable adoption
  deterrent in cloud-chat user testing. The Apps-Script-setup tool
  requests those scopes via incremental authorization (Google's
  `include_granted_scopes=true` adds the missing scope without
  resetting existing grants); regression-guard test exercises the
  cloud path and asserts the tool returns `needs_authorization` with
  an `auth_url` when stored creds lack `script.*` scopes. Commit `4eadd16`.

### Tests

- `test_user_store.py` — 13 new validator tests (canonical/dev paths,
  http/non-google/subdomain rejection, non-string rejection, save
  raises on invalid, get drops invalid with WARNING, non-validated
  fields unaffected).
- `tests/integration/test_fresh_user_flow.py` — 4 tests
  (no-creds → `NeedsReauthError`, full dance → usable creds, operator
  secrets stripped from persisted JSON, state-replay rejected).
- `tests/integration/test_migration_upgrade_path.py` — 4 tests
  (pre-setup row round-trip, enrichment via merge, narrower-schema
  legacy row reads, fresh-deploy lazy init with WAL mode).
- `tests/chaos/run_chaos.py` — S1 smoke run (2s / 4 workers) lands
  ~570 ops with p99 ~190ms, well under the 500ms budget.

### Acceptance

Saving `apps_script_url="http://bad"` raises `ValueError` before SQL
write; a row with `apps_script_url="https://mail.google.com/..."`
seeded via raw SQL is dropped on next `get_state` with a WARNING
log. New cloud-chat users see consent screens without
`script.projects` / `script.deployments` checkboxes; running
`gdocs_setup_apps_script` triggers an in-line incremental-consent
flow that adds the missing scope without invalidating Drive/Docs
grants.

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
