# OWASP ASVS Level 1 — self-attestation

**Version:** v2.3.5 (PR-Δ2)
**Standard:** [OWASP Application Security Verification Standard](https://owasp.org/www-project-application-security-verification-standard/), Level 1
**Scope:** the cloud HTTP deployment at `https://<app>.fly.dev/`. Stdio
mode is single-user local trust and most ASVS controls don't apply
(see [THREAT_MODEL.md §1](THREAT_MODEL.md#1-scope-and-trust-boundaries)).

ASVS L1 is the **minimum baseline** the standard defines — appropriate
for low-assurance applications. It is NOT a substitute for L2/L3 or
for third-party audit. See
[security-posture.md §6](security-posture.md#6-self-attestation-not-third-party-audit)
for what we explicitly do NOT claim.

## How to read this checklist

For each ASVS chapter (V1-V14) we summarize the L1 controls and
attest one of:

- **PASS** — control is implemented; link to code file:line if
  load-bearing.
- **PARTIAL** — control is implemented in part; rationale + gap noted.
- **N/A** — control does not apply to this application's surface;
  rationale required (no silent skips).
- **OPEN** — control is missing; tracked with planned closure.

Honest self-attestation means PARTIAL and OPEN markings appear where
real. A checklist with no PARTIAL/OPEN is not credible.

---

## V1 — Encoding & Sanitization

**L1 essence:** untrusted input is encoded for the appropriate output
context (HTML, SQL, command, log) to prevent injection.

| Control | Status | Notes |
|---|---|---|
| V1.2.1 — output encoding for the active interpreter | **PASS** | OAuth error HTML escapes via `html.escape()` (regression-pinned by `test_error_page_escapes_html_metachars` in [`tests/unit/test_http_server_middleware.py`](../tests/unit/test_http_server_middleware.py)). JSON responses via `JSONResponse` use Starlette's safe encoder. |
| V1.2.5 — parameterized queries / prepared statements | **PASS** | All `user_store` SQLite calls use `?` parameter binding ([`src/appscriptly/user_store.py`](../src/appscriptly/user_store.py)). No `f"SELECT ... {var}"` patterns. |
| V1.3.x — XSS-safe templating | **PASS** | OAuth callback HTML is the ONLY server-generated HTML; defense-in-depth via CSP `default-src 'none'` ([`src/appscriptly/http_server/_pages.py`](../src/appscriptly/http_server/_pages.py)); pinned by `test_oauth_error_page_includes_csp_header`. |

---

## V2 — Authentication

**L1 essence:** authentication is non-bypassable; credentials handled safely.

| Control | Status | Notes |
|---|---|---|
| V2.1.1 — password / token length floor | **PASS** | `MCP_BEARER_TOKEN` 32-char minimum enforced at startup; `keys.get_key()` raises on shorter master ([`src/appscriptly/keys.py`](../src/appscriptly/keys.py)). |
| V2.1.7 — credentials over TLS | **PASS** | Fly's edge proxy terminates TLS; all traffic to the app over HTTPS. `OAUTHLIB_INSECURE_TRANSPORT=1` rejected by `oauth_google.py` startup. |
| V2.4.x — secure credential storage | **PASS** | Tokens stored via `save_credentials_json` which strips operator `client_id`/`client_secret` before persistence; regression-pinned. |
| V2.7.x — out-of-band verification (MFA) | **N/A** | App delegates authentication to Google OAuth; MFA enforcement is Google's responsibility (user-configurable in their Google account). |
| V2.8.x — single-sign-on with established provider | **PASS** | OAuth flow uses Google as IdP via `google-auth-oauthlib`. |
| V2.10.x — service authentication | **PASS** | Bearer header constant-time compare via `hmac.compare_digest`. Per-user signed-URL HMAC pinned by tests in `test_api_convert_multitenancy.py`. |

---

## V3 — Session Management

**L1 essence:** session tokens are random, expire, and aren't leaked.

| Control | Status | Notes |
|---|---|---|
| V3.2.1 — session tokens with sufficient entropy | **PASS** | OAuth state nonces from `secrets.token_urlsafe(32)`; signed URL nonces same. |
| V3.3.1 — sessions expire | **PASS** | Signed URLs have explicit `exp` (10-min default, 1-hour max); OAuth state nonces single-use via `NonceStore`. |
| V3.3.3 — session termination on logout | **PASS** | `gdocs_reset_authorization` MCP tool clears the user's persisted creds; subsequent calls trigger fresh consent. |
| V3.4.x — cookie attributes | **N/A** | App doesn't use cookies; bearer token via Authorization header only (RFC 6750 §2.1). `bearer_methods_supported: ["header"]` pinned. |

---

## V4 — Access Control

**L1 essence:** authenticated users can only access their own resources.

| Control | Status | Notes |
|---|---|---|
| V4.1.1 — access control enforced server-side | **PASS** | `request.state.signed_url_user_id` is set by middleware after HMAC verify; downstream handler reads ONLY from request.state, never from query params directly. |
| V4.1.3 — principle of least privilege | **PASS** | OAuth requests `drive.file` (per-file) as primary scope; `drive.readonly` only for explicit-ingestion path. No `drive` (full) scope ever requested. |
| V4.1.5 — fail-safe defaults | **PASS** | Per-user dispatch in `convert.py` defaults to OPERATOR creds only on bearer-header path (single-tenant by design); cloud-chat path (signed URL) ALWAYS uses per-user creds. |
| V4.2.1 — sensitive data access controls | **PASS** | `user_store` facade returns only the calling user's row; no `get_state(other_user_id)` path from tool calls. |
| V4.3.x — admin function access controls | **PARTIAL** | `gdocs_admin_audit` (the only admin-surfaced tool) requires `admin_token` matching `MCP_ADMIN_TOKEN` env var; constant-time compare. Gap: admin token is a single shared secret, not per-operator. |

---

## V5 — Validation, Sanitization & Encoding

**L1 essence:** inputs are validated structurally; output for interpreter.

| Control | Status | Notes |
|---|---|---|
| V5.1.1 — input validation at the trust boundary | **PASS** | `apps_script_url` validated `endswith(".google.com")` AND `startswith("/macros/s/")` at save AND load. Drive query strings escape single quotes per `find_doc_by_title`'s q-DSL builder. |
| V5.1.3 — allowlist over denylist | **PASS** | TrustedHost allowlist; `split_by` enum-bound; `placeholder_behavior` enum-bound. |
| V5.2.x — sanitization for the output context | **PASS** | See V1.2.1 (HTML escape) and V1.3.x (CSP). |
| V5.3.x — SQL injection prevention | **PASS** | See V1.2.5 (parameterized queries). |
| V5.5.1 — deserialization of untrusted data | **PASS** | JSON only (Starlette `request.json()` + `json.loads`); no pickle, no YAML loader, no `eval`. |

---

## V6 — Stored Cryptography

**L1 essence:** sensitive data at rest is encrypted; keys managed.

| Control | Status | Notes |
|---|---|---|
| V6.1.1 — encryption at rest for sensitive data | **PARTIAL** | `user_state.db` is plaintext SQLite; disk encryption is platform-level (Fly volume). Application-layer encryption not implemented — correct trade-off per [THREAT_MODEL.md §9](THREAT_MODEL.md#9-what-we-currently-dont-defend-against-honest-section-pr-2) row 4. |
| V6.2.x — key derivation function | **PASS** | HKDF-SHA256 (`cryptography` library) for per-purpose key derivation post-v2.0b strict-flip; per-purpose info strings prevent cross-purpose forgery. |
| V6.4.x — key rotation | **PASS** | Per-purpose env overrides (`MCP_API_BEARER_KEY` / `OAUTH_STATE_SIGNING_KEY` / `SIGNED_URL_SIGNING_KEY`) supported today; runbook procedure at [RUNBOOK.md §3.4](RUNBOOK.md). |

---

## V7 — Error Handling & Logging

**L1 essence:** errors don't leak sensitive info; security events logged.

| Control | Status | Notes |
|---|---|---|
| V7.1.1 — no sensitive data in error messages | **PASS** | Bearer token never echoed in 401 response; `client_secret` stripped before persistence; OAuth callback error page escapes user-supplied content. Pyright + ruff CI guards against accidental token logging. |
| V7.2.1 — successful + failed auth events logged | **PASS** | Bearer compare results logged (success: `200`; failure: `401` with truncated request id); OAuth callback success/failure logged with `user_id`. |
| V7.4.x — log injection protection | **PASS** | Standard `logging` formatter; user-supplied values logged via `%s` parameter binding, not f-string interpolation into log format. |

---

## V8 — Data Protection

**L1 essence:** sensitive data protected in transit and use.

| Control | Status | Notes |
|---|---|---|
| V8.1.1 — sensitive data not cached client-side | **PASS** | OAuth callback HTML pages set `Cache-Control: no-store` (Starlette `Response` default for non-static); no sensitive data in URL fragments. |
| V8.2.x — client-side caching | **PASS** | Bearer tokens never sent to caches; signed URLs include `exp` and are single-use. |
| V8.3.x — minimization of sensitive data | **PASS** | Per-user state is minimum required: OAuth tokens, Apps Script URL, optional admin metadata. No "user profile" data stored. |

---

## V9 — Communications

**L1 essence:** secure transport, certificate handling.

| Control | Status | Notes |
|---|---|---|
| V9.1.1 — TLS 1.2+ for all connections | **PASS** | Fly's edge proxy enforces TLS 1.2+ by default; HTTP→HTTPS redirect. Outbound to Google APIs over HTTPS via `googleapiclient`. |
| V9.2.x — certificate verification | **PASS** | Default TLS verification in `requests` / `httpx` / `google-auth`; no `verify=False` patterns in source. |

---

## V10 — Malicious Code

**L1 essence:** no malicious code in supply chain.

| Control | Status | Notes |
|---|---|---|
| V10.1.x — dependency scanning | **PASS** | `pip-audit --strict` against `uv.lock` in e2e workflow; HIGH/CRITICAL fails build. CodeQL on every PR. |
| V10.2.x — pinned dependencies | **PASS** | `uv.lock` committed; `uv sync --frozen` in CI prevents lockfile drift. Base Docker image SHA-pinned. |
| V10.3.x — signed releases | **PASS** | Sigstore-attested release artifacts via `actions/attest-build-provenance@v2` (PR-Δ2); downstream consumers verify with `gh attestation verify`. |

---

## V11 — Business Logic

**L1 essence:** workflows can't be abused; rate limits where appropriate.

| Control | Status | Notes |
|---|---|---|
| V11.1.x — process flow integrity | **PASS** | OAuth state binds the user through the consent dance; signed URL nonces are single-use; replay rejected. |
| V11.1.7 — rate limiting / anti-automation | **OPEN** | No in-process rate limit on `/api/convert`, `/info`, or `/oauth/google/api/callback`. Fly's edge proxy provides per-IP limits at the platform layer. **Tracked: PR-Δ3.** See [THREAT_MODEL.md §9](THREAT_MODEL.md#9-what-we-currently-dont-defend-against-honest-section-pr-2) row 2. |

---

## V12 — File and Resources

**L1 essence:** file uploads / paths / resources don't allow injection.

| Control | Status | Notes |
|---|---|---|
| V12.1.1 — file upload allowlist | **PASS** | `/api/convert` rejects non-`.docx` extensions; MIME type validated by Drive's converter downstream. |
| V12.1.3 — file upload size limit | **PARTIAL** | `BodySizeLimitMiddleware` 10 MB declared-length cap; full decompressed-size cap deferred (v1.4 carry-forward, [THREAT_MODEL.md §9](THREAT_MODEL.md#9-what-we-currently-dont-defend-against-honest-section-pr-2) row 3). |
| V12.3.x — path traversal | **PASS** | No user-controlled paths reach the filesystem; .docx upload streams to `tempfile.NamedTemporaryFile(suffix=".docx")`. Drive file IDs are opaque tokens, not paths. |
| V12.5.x — SSRF on outbound requests | **PASS** | `apps_script_url` validated `endswith(".google.com")` AND `startswith("/macros/s/")` at save AND load. No other arbitrary-URL outbound paths from user input. |

---

## V13 — API and Web Service

**L1 essence:** API contracts enforced, common API attacks prevented.

| Control | Status | Notes |
|---|---|---|
| V13.1.1 — API auth equivalent to web | **PASS** | Bearer header + per-user signed URL; both go through `BearerTokenMiddleware`. |
| V13.1.4 — same access control rules across surfaces | **PASS** | MCP `@workspace_tool(creds=True)` tools and `/api/convert` REST endpoint both dispatch through `_get_credentials()` / `request.state.signed_url_user_id`. |
| V13.2.x — RESTful safety | **PASS** | `GET` endpoints are read-only (`/health`, `/info`, `/.well-known/*`, OAuth callback observes-only-then-side-effects); state changes require `POST`. |
| V13.3.x — SOAP / GraphQL | **N/A** | App is REST + MCP only; no SOAP, no GraphQL. |
| V13.4.x — anti-CSRF | **PASS** | OAuth callback CSRF via HMAC-signed `state`; `/api/convert` is bearer-or-signed-URL gated (CSRF irrelevant — no cookies, no ambient credentials). |

---

## V14 — Configuration

**L1 essence:** deployment hardened; secrets managed; CI/CD secure.

| Control | Status | Notes |
|---|---|---|
| V14.1.x — build artifacts reproducible | **PASS** | `uv sync --frozen` in CI + SHA-pinned base Docker image + SHA-pinned third-party GitHub Actions (PR #51). |
| V14.2.x — dependency configuration | **PASS** | See V10.x. |
| V14.3.x — secure default config | **PASS** | TrustedHost fails-closed on Fly without `FLY_APP_NAME` (refused at startup, not silent fail-open). `MCP_BEARER_TOKEN` < 32 chars refused at startup. |
| V14.4.x — HTTP security headers | **PARTIAL** | OAuth pages set `Content-Security-Policy: default-src 'none'`. App-wide HSTS / X-Frame-Options / X-Content-Type-Options not set (Fly's edge applies some by default; not explicitly enforced application-side). |
| V14.5.x — sensitive HTTP methods | **PASS** | Routes declare explicit `methods=["GET"]` or `["POST"]`; Starlette returns 405 on method mismatch. |

---

## Summary

| Chapter | Total controls assessed | PASS | PARTIAL | N/A | OPEN |
|---|---|---|---|---|---|
| V1 Encoding | 3 | 3 | 0 | 0 | 0 |
| V2 Auth | 6 | 5 | 0 | 1 | 0 |
| V3 Sessions | 4 | 3 | 0 | 1 | 0 |
| V4 Access Control | 5 | 4 | 1 | 0 | 0 |
| V5 Validation | 5 | 5 | 0 | 0 | 0 |
| V6 Crypto at rest | 3 | 2 | 1 | 0 | 0 |
| V7 Errors/logs | 3 | 3 | 0 | 0 | 0 |
| V8 Data | 3 | 3 | 0 | 0 | 0 |
| V9 Comms | 2 | 2 | 0 | 0 | 0 |
| V10 Supply chain | 3 | 3 | 0 | 0 | 0 |
| V11 Business logic | 2 | 1 | 0 | 0 | 1 |
| V12 Files | 4 | 3 | 1 | 0 | 0 |
| V13 API | 5 | 4 | 0 | 1 | 0 |
| V14 Config | 5 | 4 | 1 | 0 | 0 |
| **TOTAL** | **53** | **45** | **4** | **3** | **1** |

**Verdict:** 45/53 (85%) PASS at ASVS Level 1, with 4 PARTIALs
(disk encryption, admin token, upload size cap, HTTP headers), 3
N/A (MFA delegated to Google, cookies not used, SOAP/GraphQL not
present), and 1 OPEN (in-process rate limiting, tracked PR-Δ3).

This attestation is honest: PARTIAL and OPEN are explicit, not
disguised as PASS. Re-evaluation will happen on every PR that
touches an L1-relevant code path; major shifts will be reflected
in a CHANGELOG entry citing this file.
