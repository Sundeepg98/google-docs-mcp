# Security Policy

## Reporting a vulnerability

If you find a security issue in this project — OAuth flow, signed-URL handling, Apps Script deployment, or anything else with user-data implications — **please do not file a public issue.**

Instead, open a private security advisory at:
https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new

We'll acknowledge within 7 days. Fixes will be coordinated and released alongside disclosure.

## Scope

In scope:
- Authentication and authorization (OAuth token handling, signed-URL HMAC, service-account impersonation)
- Data handling (anything that touches the user's Google Drive contents)
- Server-side issues in the Fly.io HTTP transport (e.g., the `/api/convert` endpoint)
- Apps Script deployment flow (the `setup-apps-script-auto` CLI and `gas_deploy` sub-package)

Out of scope:
- Vulnerabilities in upstream dependencies (report those to the dependency directly)
- Vulnerabilities in Google's services themselves (report via Google's VRP)
- Misconfigurations that require user action to exploit (e.g., setting `access: ANYONE` on a Web App deployment)

## Supported versions

The latest tagged release on `main` is supported. Earlier versions are not patched.

## Threat model

The full threat model — trust boundaries, attack surfaces, attacker personas, and the surface-by-surface mitigation table — lives in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). New reviewers and contributors evaluating PRs that touch auth, persistence, or network paths should read it before commenting on the security posture.

Key takeaways for fast reference:
- **Stdio mode** = single-user, local trust. The operator IS the only user. Most network-mode threats don't apply.
- **HTTP mode (Fly.io cloud)** = multi-tenant. Bearer auth on `/api/*`; FastMCP JWTs on `/mcp`; per-user `user_state.db` rows keyed by Google `sub`. This is where the threat model bites.
- **Cryptographic keys** are derived from one master via HKDF-SHA256 (v2.0b+); three logical keys (`api_bearer`, `oauth_state`, `signed_url`) with separate env overrides. See THREAT_MODEL §5.
- **OPEN risk** (v2.0.6): Apps Script Web App `/exec` surface is `ANYONE_ANONYMOUS` — anyone with the per-user URL can POST. HMAC verify-path deferred to v2.0c. Mitigation today: URL secrecy. See THREAT_MODEL §4 row 5.

## Dependency CVE handling

We run `pip-audit --strict` against `uv.lock` in the e2e workflow on every push. CVEs surface as failing CI; each must be either fixed, pinned to a non-vulnerable version, or explicitly ignored with a documented non-applicability finding.

### Ignored CVEs

#### PYSEC-2025-183 / CVE-2025-45768 — pyjwt (CVSS 7.0 HIGH, DISPUTED by upstream)

- **Where it enters:** `mcp[crypto] → pyjwt` (transitive dependency; we do not declare pyjwt directly).
- **Our usage:** ZERO. `grep -rn "import jwt" src/ tests/ scripts/` returns no hits. Verified per release.
- **`mcp[crypto]`'s only consumer of pyjwt:** `mcp/client/auth/extensions/client_credentials.py`, where it underpins `PrivateKeyJWTOAuthProvider` and `RFC7523OAuthClientProvider`. **We do not instantiate either provider.** Our OAuth path is Google's standard Authorization Code flow via `google-auth-oauthlib` (see `src/google_docs_mcp/oauth_google.py`).
- **Mitigation status:** Non-applicable — the vulnerable code path is never executed in our deployment. This is neither a "real" mitigation (no fix applied) nor a "theatrical" mitigation (no defense-in-depth that doesn't actually defend); the CVE simply cannot fire on a code path we never run.
- **Action:** `.github/workflows/e2e.yml` runs `pip-audit --strict --ignore-vuln PYSEC-2025-183` with a 20-line provenance comment above the line. The comment names the upstream non-consumer, our actual OAuth path, and the re-audit trigger below.
- **Re-audit trigger:** If we ever wire `PrivateKeyJWTOAuthProvider` or `RFC7523OAuthClientProvider` anywhere in this codebase, **this ignore MUST be re-evaluated before the PR lands.** The grep above is the bright line; a CI lint enforcing it is on the v2.1 backlog.

### What gets ignored vs fixed

| Severity (CVSS) | Default action | Override required |
|---|---|---|
| CRITICAL (9.0–10.0) | FIX (pin floor, no exceptions) | Security advisory + maintainer approval |
| HIGH (7.0–8.9) | FIX unless non-applicability is documented | Provenance comment in `e2e.yml` (see pyjwt example above) |
| MEDIUM (4.0–6.9) | FIX in the next release cycle | Issue-tracker note, no advisory required |
| LOW (0.1–3.9) | Track in dependency-housekeeping batch | Captured in next dependabot batch |

The pyjwt entry above is the canonical template for documenting HIGH-severity non-applicability. Future ignored CVEs should match its structure (where it enters / our usage / why non-applicable / re-audit trigger).
