# ADR 2026-05-27 — Spec compliance (RFC 9728) + scope union (Apps Script in baseline)

**Status**: Accepted
**Date**: 2026-05-27
**PR**: PR-Δ1
**Builds on**: PR-α reframe (`gdocs_install_automation` rename + alias)

## Context

Two related papercuts surfaced in the claude.ai connector UX after PR-α
shipped the reframed `gdocs_install_automation` tool:

1. **`/.well-known/oauth-protected-resource` returned 404.** The MCP
   Authorization spec MANDATES this endpoint (RFC 9728 — *OAuth 2.0
   Protected Resource Metadata*) for any MCP server that exposes
   OAuth-protected resources. We had the companion RFC 8414 endpoint
   (`/.well-known/oauth-authorization-server`) wired automatically by
   FastMCP's `GoogleProvider`, but the 9728 half was missing entirely.
   Live-verified via `curl` against the production deployment.

   Symptom: spec-conformant claude.ai connector discovery probes the
   9728 path on reconnect; a 404 forced the discovery path to fall
   back to less-precise heuristics. Not a complete break — claude.ai
   tolerates the absence — but the reconnect UX was noticeably
   slower and less precise than it should be.

2. **Apps Script scopes triggered a second consent moment.**
   Pre-PR-Δ1 the baseline OAuth consent (`auth.SCOPES`) covered
   docs + drive + sheets + slides + presentations. The Apps Script
   management scopes (`script.projects`, `script.deployments`) were
   in a separate per-tool `GAS_DEPLOY_SCOPES` list, requested
   incrementally by `gdocs_install_automation` (née
   `gdocs_setup_apps_script`) via Google's
   `include_granted_scopes=true` flow.

   This was a v1.x scope-reduction decision (Issue #17), which made
   sense when Apps Script setup was hidden infrastructure that most
   users never touched. After the PR-α reframe surfaced
   `gdocs_install_automation` as headline functionality, the
   incremental-consent moment became a UX papercut: users finished
   the first consent flow, started building, and immediately got
   prompted again the moment any workflow needed the automation
   runtime. The reframe paragraph in the consent screen tried to
   contextualize this, but the second-consent-screen surface is
   identical to the first one, so users couldn't tell it was a
   continuation versus a fresh ask.

## Decision

**(1) Add the RFC 9728 endpoint.** New
`GET /.well-known/oauth-protected-resource` handler in
`http_server/routes/observability.py`, registered in
`http_server/app.py` BEFORE the catch-all `Mount("/", ...)` so
Starlette resolves it before falling through to the FastMCP sub-app's
404. Response shape per RFC 9728 §3:

```json
{
  "resource": "<base-url>",
  "authorization_servers": ["<base-url>"],
  "scopes_supported": [...sorted GOOGLE_API_SCOPES...],
  "bearer_methods_supported": ["header"],
  "resource_documentation": "<base-url>"
}
```

Sources `scopes_supported` from `oauth_google.GOOGLE_API_SCOPES`
directly — no duplicate registry. Scope additions / removals in that
one list propagate to the metadata endpoint without a second edit.
`bearer_methods_supported` is pinned to `["header"]` only: we
deliberately don't implement RFC 6750 §2.2/§2.3 (query-string or
POST-body bearer), and a regression test guards the constraint so a
future "let me add query-string bearer for convenience" change
forces a security review first.

Public endpoint by spec — claude.ai's discovery probes it before
consent is even possible, so no bearer is required.
`BearerTokenMiddleware`'s dispatch matcher already excludes
`/.well-known/*` from the bearer-required set.

**(2) Promote Apps Script scopes to baseline.** Added
`script.projects` + `script.deployments` to both `auth.SCOPES`
(stdio mode) and `oauth_google.GOOGLE_API_SCOPES` (HTTP/cloud mode).
Reverses the v1.x scope-reduction decision (Issue #17), which the
PR-α reframe made obsolete: the reduction was the right call when
Apps Script was hidden infrastructure, but it's the wrong call once
the install is headline functionality.

`services/gas_deploy/tools.py`'s per-tool
`required_scopes=GAS_DEPLOY_SCOPES` parameter is kept verbatim. It
becomes documentary post-PR-Δ1 since the scopes are now baseline-
granted (so `_check_scopes_or_raise` passes on first call), but
removing it would obscure the intent at the install site. Leave it
for the next reader.

## Consequences

### What gets better

- **One consent moment for the entire install.** New users hit a
  single Google consent screen that lists every scope the server
  may ever ask for; subsequent tool calls — including the headline
  `gdocs_install_automation` — Just Work without re-prompting. The
  reframe paragraph from PR-α now describes the actual UX (one
  install, persistent automation runtime in your Workspace),
  matching the consent flow.

- **Spec-conformant claude.ai connector reconnect.** RFC 9728
  discovery succeeds on first probe rather than fallback. Faster,
  more precise reconnect UX. Future claude.ai client versions that
  hard-require the endpoint won't regress us.

- **Existing users**: pick up the new baseline scopes automatically
  on next token refresh via Google's `include_granted_scopes=true`
  incremental-consent flow. No forced re-consent — the same path
  that handled the earlier `drive.readonly` + Apps Script +
  `spreadsheets` + `presentations` scope additions across prior PRs.

### What gets traded

- **Baseline consent screen lists 2 more scopes** (`script.projects`,
  `script.deployments`). The PR-α reframe paragraph now lives on the
  install tool itself rather than in the consent screen, so the
  consent screen needs to stand on its own merit. Acceptable:
  Google's standard consent UI shows scopes by friendly description
  ("View, edit, create, and delete your Apps Script projects"),
  and the operator's product positioning ("Workspace Automation
  MCP") already primes users to expect the runtime.

### What's NOT in this ADR (explicitly)

- **`drive.readonly` stays.** The CASA-audit-driven removal was
  considered (an earlier draft of PR-Δ1 included it) and DROPPED
  per operator decision: Testing-mode bypass covers the current
  deployment, and the future-CASA-if-Marketplace concern is
  hypothetical rather than a present blocker. Re-evaluate if/when
  Marketplace listing becomes a near-term goal.

- **`docx_drive_file_id` path stays.** Removing it was bundled with
  the `drive.readonly` removal in earlier drafts; both decisions
  reverse for the same reason. The existing workflows (2 + 3 in
  the MCP server instructions) keep working unchanged for files
  this app created AND for files uploaded by other apps.

- **SECURITY.md / threat model / OWASP ASVS** — deferred to PR-Δ2.

- **Rate limiting / key rotation runbook / pip-audit CI / HMAC
  constant-time verification** — deferred to PR-Δ3 (hardening).

## Verification

- Live `curl` against the deployment after PR merge:
  `https://sundeepg98-docs-mcp.fly.dev/.well-known/oauth-protected-resource`
  returns 200 with the documented JSON shape.
- claude.ai connector reconnect flow: first-probe discovery succeeds
  on the 9728 path (operator manual verification post-deploy).
- Existing user with a v2.3.3 token: next tool call refreshes the
  token with the expanded scope set, no re-consent prompt.

## References

- RFC 9728 — OAuth 2.0 Protected Resource Metadata
- RFC 8414 — OAuth 2.0 Authorization Server Metadata (companion endpoint, auto-wired)
- RFC 6750 — OAuth 2.0 Bearer Token Usage (§2.2 / §2.3 we deliberately don't implement)
- MCP Authorization spec — mandates RFC 9728 for protected-resource MCP servers
- PR-α (reframe) — surfaced `gdocs_install_automation` as headline functionality, which motivated the baseline-scope unification
- Issue #17 (v1.x) — original Apps Script scope reduction (the decision PR-Δ1 reverses)
