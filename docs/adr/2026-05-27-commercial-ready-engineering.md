# ADR 2026-05-27 — Commercial-ready engineering (PR-Δ5)

**Status**: Accepted
**Date**: 2026-05-27
**PR**: PR-Δ5
**Builds on**: PR-α (reframe), PR-Δ1 (spec compliance), PR-Δ2 (security
disclosure), PR-Δ3 (retry/hardening), PR-Δ4 (DR + observability)

## Context

Operator's strategic framing (2026-05-27): **build commercial-ready
engineering NOW for free; defer paid commercial activities until
later.** Engineering investments are cheap (dev hours via agents);
commercial activities (third-party audits, SSO subscriptions, lawyer
drafting) carry recurring per-year cost.

The personal-tier deployment continues to be the primary use case.
Anything that activates commercial-only behavior MUST default off so
the personal experience stays unchanged. Tomorrow's commercial
activation must be a config flip (env var + Stripe wiring), not a
code-and-deploy operation.

Three thematically related items surfaced as the right
commercial-ready engineering investment for this window:

1. **License-key middleware** — a wire-but-stub gate so commercial
   activation later is a Stripe (or similar) integration plus an env
   var flip, not an architectural change.
2. **GCP project linking for Apps Script** — opt-in Cloud Logging
   audit trail for SOC 2 / enterprise customers.
3. **Multi-tenant hardening** — defensive per-call tenant-binding
   assertion + structured audit log of credential dispatch events.

## Decision

### (1) License-key middleware

New module `src/appscriptly/license.py` with `check_license(token)
→ LicenseStatus`. Three states: `DISABLED` (env var off — personal
default), `VALID` (env var on + token accepted), `INVALID` (env var
on + token missing or rejected).

The verifier is a STUB — `_verify_token` returns True unconditionally
and logs the check for visibility. Commercial activation swaps the
function body for real verification (Stripe license keys, self-hosted
JWT validation, internal license server). The signature is the
contract; everything else in the module + middleware + HTTP 402
response shape stays unchanged across the swap.

New `LicenseKeyMiddleware` in `http_server/middleware.py`. Wired into
the existing middleware stack AFTER `BearerTokenMiddleware` (same
protected surface: `/api/*` and `/info`). Returns **HTTP 402 Payment
Required** (RFC 9110 §15.5.2) for missing / rejected keys when
enforcement is on — distinct from 401 (auth missing) so monitoring
can disambiguate "user forgot bearer" from "user lacks license."

Key resolution order: `X-License-Key` HTTP header (caller-supplied,
commercial customer hits) beats `MCP_LICENSE_KEY` env var
(operator-configured, self-hosted customer). Lets an operator
override the env-var default per-request without restarting.

Activation: `LICENSE_KEY_ENFORCEMENT=true` env var flips the gate
on. Default (unset / `false` / `0` / `no` / `off`) is no-op for
personal users.

### (2) GCP project linking for Apps Script

New `_build_manifest(gcp_project_number)` in
`src/appscriptly/setup_apps_script.py`. When the project number
is supplied (via the `GCP_PROJECT_NUMBER` env var), the appsscript.json
manifest gets an additional `cloudPlatform.projectId` block per
Google's documented schema. When unset (the default), the manifest is
bit-for-bit identical to v2.3.x — zero behavior change.

`_current_manifest()` resolves the env var at CALL time (not module
import) so:
- Tests can monkeypatch the env var per-test without reloading.
- Operators flipping the env var without restarting (e.g. Fly secret
  update + soft-reload) see the change on the next pipeline run.

Both manifest-using sites in `_execute_setup_with_ledger` (push_files
+ compute_content_hash) use the SAME manifest within a single run
via a bound local — the content_hash MUST match what gets pushed for
the setup-state ledger's "manifest changed → re-deploy" reset logic
to work correctly for GCP-linking flips.

Effect when enabled: every Apps Script execution surfaces logs in the
named GCP project's Cloud Logging. Searchable + retainable per
GCP's standard policies + exportable to BigQuery / SIEM. SOC 2 +
HIPAA + GDPR audit-trail compliant. See
`docs/runbooks/gcp-project-linking.md` for the operator setup
workflow.

### (3) Multi-tenant hardening

Two complementary additions:

**3a. Tenant-stamp on credentials.** `_stamp_tenant(creds, user_id)`
in `credentials.py` writes the user_id to a known attribute
(`_google_docs_mcp_user_id`) on every Credentials object that flows
out of `get_credentials_for_user`. Process-local (never serialized
to `user_store`).

**3b. Defensive `assert_tenant_match`.** New helper in
`_tool_helpers.py`. Called automatically by `_get_credentials` (the
3-consumer-extraction-trigger function from M3 Phase C). On
mismatch — credentials stamped for a different user than the caller
expects — raises `TenantIsolationError` (subclass of `AssertionError`)
immediately, BEFORE any user data flows downstream.

Today this is genuinely belt-and-suspenders: the storage layer is
the source of truth and the storage layer is correct. The assertion
catches a future caching bug (stale entry returned for the wrong
user), a future SQL bug (WHERE clause typo), a future race condition
(two requests interleaving cache updates). Without the assertion,
those bugs would surface as silent cross-tenant data access; with
it, they surface as a hard loud failure before any user data is
touched.

**3c. Per-call audit log.** New logger
`appscriptly.audit.tenant` emits a structured record on every
credential dispatch outcome (`dispatched`, `needs_reauth`,
`revoked`). Carries `audit_user_id`, `audit_required_scopes`,
`audit_granted_scopes`, `audit_event`, `audit_outcome` as extra
fields. The PR-Δ4 `request_id` ContextVar is auto-injected by the
existing `RequestIdLogFilter` on the root logger — no explicit
threading.

A separate logger
`appscriptly.audit.tenant_isolation` carries the assertion-fire
records (WARNING for stamp-absent, ERROR for actual mismatch). The
split lets operators route normal dispatch audit trail (high volume,
compliance retention) separately from isolation-violation alerts
(low volume, page-the-on-call).

The human-readable log message truncates `user_id` to 8 chars to
avoid leaking the full Google `sub` claim into shoulder-surfable
terminal output; the structured `audit_user_id` field carries the
full untruncated value for downstream correlation.

## Consequences

### What gets better

- **Commercial activation cost ≈ 0 dev-days.** When the operator is
  ready to charge for the server, the work is: write a Stripe (or
  similar) license-key verifier, swap the `_verify_token` stub for
  it, flip `LICENSE_KEY_ENFORCEMENT=true`. The middleware + 402
  response shape + env-var plumbing + caller-vs-operator key
  resolution order all stay unchanged. ~1-2 hours of work, not 1-2
  days of architectural redesign.
- **SOC 2 audit trail ready to opt into.** Enterprise customers
  asking for "where do the Apps Script logs go" have a one-line
  answer: set `GCP_PROJECT_NUMBER`. The runbook walks through
  setup + verification + compliance notes.
- **Cross-tenant safety hardened.** Two layers of defense now: the
  storage layer (was: only layer) + the assertion at every
  credential-dispatch consumer. A future bug at the storage layer
  fails LOUDLY instead of silently leaking.
- **Audit trail anchor for compliance.** The
  `appscriptly.audit.tenant` logger gives SOC 2 / GDPR / HIPAA
  reviewers a single grep target for "who got which credentials
  when" — paired with the PR-Δ4 request-ID correlation, complete
  request lifecycle reconstruction is one `flyctl logs | grep` away.

### What changes for personal users

**Nothing.** All three items default off:

- `LICENSE_KEY_ENFORCEMENT` unset → middleware passes every request
  through unchanged.
- `GCP_PROJECT_NUMBER` unset → Apps Script manifest identical to
  v2.3.x.
- The tenant-stamp + audit log are background plumbing — visible
  in the structured logs (which most personal users don't read)
  but invisible at the tool surface.

A personal-mode deployment that never touches any of the three env
vars sees zero behavior change. The assertion fires only on
mismatch, which doesn't happen under any current code path.

### What changes for commercial activation

Three env-var flips (zero code redeploys needed beyond the
verifier swap):

```bash
# Commercial activation checklist
fly secrets set LICENSE_KEY_ENFORCEMENT=true         # gate the protected surface
fly secrets set MCP_LICENSE_KEY="<operator-key>"      # OR rely on per-customer headers
fly secrets set GCP_PROJECT_NUMBER="<project-num>"   # opt into Cloud Logging audit trail

# Then swap the _verify_token stub for the real verifier — separate PR
# Pattern: stripe.licenses.retrieve(token).active, or jwt.decode(...)
```

The customer-facing surface change: callers send `X-License-Key:
<their-key>` on every `/api/*` request; invalid keys get 402; the
documentation URL in the 402 body points at the operator's
customer-onboarding page.

### What this PR does NOT do (explicit non-decisions)

- **No Stripe integration.** The verifier swap is a follow-up PR
  when commercial activation is the priority. This PR pins the
  shape only.
- **No actual GCP project creation.** Operator action; the runbook
  walks through it.
- **No SSO / SAML.** Enterprise-tier feature; deferred indefinitely
  per operator's "personal-first" framing.
- **No marketplace listing artifacts.** Deferred to PR-Δ8.
- **No per-tool license tier gating** (e.g. "Tool X requires Pro
  license"). The `@workspace_tool` decorator is the natural
  attachment point, but no commercial-only tool exists yet to
  motivate the gating surface. Deferred until first commercial-tier
  tool ships.
- **No license-key telemetry / usage metering.** The verifier
  invocations are logged (so operators flipping enforcement on for
  the first time can see the middleware run) but there's no
  aggregate metric / billing-event emission. Deferred to the
  commercial-activation PR alongside Stripe wiring.

## Verification

- `pytest tests/` — 972 passed, 5 skipped (live-only). +45 vs the
  927 baseline.
- `ruff check src/ tests/` — clean.
- `pyright src/` — 0 errors, 0 warnings.
- New test files:
  - `tests/unit/test_license.py` — 14 tests covering DISABLED /
    VALID / INVALID paths + middleware integration + header-beats-
    env resolution order.
  - `tests/unit/test_gcp_project_linking.py` — 9 tests covering
    pure-helper + env-var-read + at-call-time semantics + content-
    hash-change-triggers-redeploy invariant.
  - `tests/unit/test_tenant_isolation.py` — 10 tests covering
    stamp round-trip + assert match/mismatch/absent + audit-log
    structured-field emission + end-to-end via
    `get_credentials_for_user`.
- One existing test required a stamp-on-mock fixup
  (`test_get_credentials_http_mode_uses_per_user_resolver`); the
  mock now mirrors the production contract that `_stamp_tenant`
  imposes on returned credentials.

## References

- RFC 9110 §15.5.2 — HTTP 402 Payment Required semantics
- Apps Script manifest schema:
  <https://developers.google.com/apps-script/manifest#cloudplatform>
- Google Cloud Logging free tier:
  <https://cloud.google.com/logging/quotas#free-tier>
- M3 Phase C extraction rule (justifies adding
  `assert_tenant_match` to `_tool_helpers` rather than each
  consumer): `docs/ARCHITECTURE.md` §5.1
- PR-Δ4 request-ID middleware (the structured-log correlation
  spine the audit-log records ride on):
  `docs/adr/2026-05-27-dr-and-observability.md`
- Runbook: `docs/runbooks/gcp-project-linking.md`
