# Threat Model — google-docs-mcp

**Version:** v2.3.5 (post-PR-Δ2 — security posture artifacts)
**Last updated:** 2026-05-27
**Audience:** security reviewers, contributors evaluating PRs that touch auth/persistence/network paths

> **Document layout.** §§ 1-6 are the original v1.3.1 surface-table threat model.
> §§ 7-9 (added in PR-Δ2 / v2.3.5) supplement with: a per-component STRIDE pass,
> a bounded-blast-radius architectural callout, and an honest "what we currently
> don't defend against" section. The Apps Script HMAC verify-path (v2.0c) IS now
> folded into the §4 table (row 5, CLOSED) and §§ 7-9. Other surface-by-surface
> mitigation deltas since v1.3.1 (HKDF strict-flip in v2.0b, per-user signed-URL
> multi-tenancy in v2.1, health-exempt TrustedHost in v2.3.3, RFC 9728 metadata
> in PR-Δ1) are NOT yet folded into the §4 table — bringing the rest up to date
> is a separate hygiene PR. The §§ 7-9 supplement is independently true and
> useful as-is.

## 1. Scope and trust boundaries

google-docs-mcp runs in two modes with different trust models:

- **Stdio mode** (Claude Desktop / Code on a developer laptop): single-user, local trust. The operator IS the only user. OAuth tokens cache at `~/.google-docs-mcp/`. No network surface beyond outbound Google API calls. Threats below largely don't apply.
- **HTTP mode** (Fly.io cloud deployment): multi-tenant. Each connector user has a separate `user_state.db` row keyed by Google `sub` claim. Public HTTPS endpoint at `https://<app>.fly.dev/`. Bearer-auth on `/api/*`; FastMCP-issued JWTs on `/mcp`. The threat model below targets THIS mode.

**Trust boundaries:**
- **Outside trust:** the public internet, untrusted Google account holders, untrusted HTTP clients.
- **Half-trusted:** authenticated connector users (have a Google `sub`; can only operate on their own row).
- **Inside trust:** the operator (deploys, holds `MCP_BEARER_TOKEN`, has SSH to the Fly machine, has read access to all `user_state.db` rows).

The threat model focuses on **outside → half-trusted** and **half-trusted → other-user** boundaries. Inside-trust compromise is out of scope (an attacker with operator credentials owns the system; mitigations are limited to SSH key hygiene and Fly token rotation, both Fly-platform concerns).

## 2. Attack surfaces

| # | Surface | Reachable from |
|---|---|---|
| 1 | `/api/convert` POST upload | public internet (gated by bearer or signed URL) |
| 2 | `/oauth/google/api/callback` GET | public internet (Google's redirect) |
| 3 | `/mcp` POST | public internet (gated by FastMCP-issued JWT) |
| 4 | `apps_script_url` outbound POST | server-internal (per-user URL from `user_state`) |
| 5 | Apps Script Web App `/exec` POST | public internet (deployed `ANYONE_ANONYMOUS`) |
| 6 | Bearer token header on `/api/*` | public internet |
| 7 | TrustedHost middleware (Host header) | public internet (front of every request) |
| 8 | Container runtime / `/data` volume | inside-trust only (SSH) |

## 3. Attacker model

Three personas considered:

- **Opportunistic scanner**: scrapes GitHub for OAuth client IDs, scans Fly.dev for open MCP servers, attempts default-credential and known-CVE exploits. Skill: low. Time: minutes per target.
- **Targeted attacker**: knows the deployment exists, may have access to leaked logs / GitHub issue transcripts containing URLs, may run captured signed URLs through replay. Skill: medium. Time: hours.
- **Authenticated peer**: a legitimate connector user attempting to read/write another user's docs via tool-call abuse. Skill: medium. Time: bounded by their consent grant lifetime.

Nation-state actors with supply-chain capability are out of scope (would require Anthropic / Google / Fly compromise, all outside our control).

## 4. Threat-model table

Each row: surface → primary threat → mitigation → residual risk → test that fences it.

| # | Surface | Threat | Mitigation | Residual risk | Fencing test |
|---|---|---|---|---|---|
| 1 | `/api/convert` upload | OOM via crafted .docx (zip-bomb) | None as of v1.3.1 — relies on `BodySizeLimitMiddleware` 10 MB declared `Content-Length` cap; full decompressed-size cap deferred to v1.4 | A file under 10 MB but with quadratic-blowup XML can still degrade — bounded by 512 MB Fly VM | `test_bodysize_413_when_content_length_exceeds` |
| 2 | `/api/convert` upload | Body-size DoS via chunked encoding (no Content-Length) | None today — middleware only checks declared length. v1.4 ships `request.form(max_part_size=...)` for full coverage | Open until v1.4 ships | (no test today) |
| 3 | `/oauth/google/api/callback` | State replay / CSRF via swapped `state` param | HMAC-signed state (key from `keys.get_key("oauth_state")`) + single-use `NonceStore` + 10-min TTL | 10-min window if URL captured pre-redeem; first-redeem-wins | `test_signed_state_single_use`, `test_state_expired_rejected` |
| 4 | `apps_script_url` outbound | SSRF / data exfil to attacker URL | Host validation `endswith(".google.com")` AND path `startswith("/macros/s/")` at user_store save + load boundaries (v1.4.0a) | DB write would need a separate vuln; validation is prophylactic | `test_apps_script_url_validation_at_save` (v1.4.0a) |
| 5 | Apps Script Web App `/exec` | Anonymous POST → mutate victim docs they own | **CLOSED (v2.0c).** Every POST is authenticated by a per-user HMAC-SHA256 signature: `restructure.gs::doPost` calls `Utilities.computeHmacSha256Signature` over `"<timestamp>.<body>"`, constant-time-compares it to the `X-MCP-Signature` header (with a 5-min `X-MCP-Timestamp` skew window), and FAILS CLOSED with `stage:'auth'` on any missing/stale/mismatched signature. The server signs with the per-user `apps_script_hmac_key` in `_call_webapp`; the key is provisioned at setup and baked into the deployed script. `access` stays `ANYONE_ANONYMOUS` because the server posts with no Google sign-in, but URL secrecy is NO LONGER the access control. | LOW — forging a request needs the per-user 256-bit (64-hex-char) key, which is never logged/echoed and lives only in the user's row + their own deployed script. Residual: operator-read of `user_state.db` (inside-trust, §3). | `test_restructure_gs_has_hmac_validation`, `test_call_webapp_signs_requests` |
| 6 | Bearer token header | Brute-force on `/api/*` | 32-char minimum on master (`keys.get_key()` raises if shorter); HMAC equality via `hmac.compare_digest` | Operator chooses master entropy; if `MCP_BEARER_TOKEN` < 32 chars, server refuses to start | `test_keys_short_master_fails_loud` |
| 7 | TrustedHostMiddleware | Host-header injection / SSRF via Host | Allowlist from `derive_trusted_hosts()` reading `FLY_APP_NAME` + `localhost` + `*.fly.dev`. Fail-closed boot assertion if `FLY_REGION` set without `FLY_APP_NAME` | Empty `FLY_APP_NAME` outside Fly opens to `["*"]` with WARNING (dev fail-open) | `test_derive_trusted_hosts_fly_app_name` |
| 8 | Container runtime | Container breakout → `/data` SQLite | `app` non-root user; base image `python:3.13-slim` SHA-pinned via Dockerfile digest | `/data` is writable by `app` (required for SQLite); a breakout landing as `app` reads ALL user rows | `test_docker_user_is_not_root` (CI smoke) |

## 5. Cryptographic key inventory

As of v1.3.1, three logical keys are derived from one master:

| Purpose | Env override | Derivation | Used for |
|---|---|---|---|
| `api_bearer` | `MCP_API_BEARER_KEY` | shim returns raw `MCP_BEARER_TOKEN`; v2.0 strict-flip activates HKDF-SHA256 with info=`b"google-docs-mcp v1 api_bearer"` | Bearer header equality on `/api/*` |
| `oauth_state` | `OAUTH_STATE_SIGNING_KEY` | same shim; v2.0 strict-flip uses info=`b"google-docs-mcp v1 oauth_state"` | HMAC over OAuth callback state token (`oauth_state.py`) |
| `signed_url` | `SIGNED_URL_SIGNING_KEY` | same shim; v2.0 strict-flip uses info=`b"google-docs-mcp v1 signed_url"` | HMAC over signed upload URL query params (`crypto.py`) |

**`_BACK_COMPAT_RAW_MASTER` shim (v1.3.1 through v1.5):** all three purposes return the raw `MCP_BEARER_TOKEN` unless their dedicated env override is set. Rationale: in-flight signed URLs and OAuth states minted under v1.3.0 (which used the raw master directly) continue to verify cleanly under v1.3.1. The v2.0 strict-flip removes the shim — at that point, unset overrides switch to HKDF-derived values and ALL pre-existing in-flight tokens invalidate.

**Per-user keys (v2.0c — wired):** `apps_script_hmac_key` (64-char hex) is provisioned per user at setup (`setup_apps_script`), baked into that user's deployed `restructure.gs`, and consumed at runtime on BOTH sides: `_call_webapp` signs every `/exec` POST with it, and `restructure.gs::doPost` verifies the signature before acting. The migration script backfills legacy rows. This key now has full runtime effect — it is the authentication for the `/exec` surface (§4 row 5). The signing scheme lives in `apps_script_hmac.py` (single source of truth for both sides).

**Rotation procedures** live in `docs/RUNBOOK.md` § 3.4.

## 6. Out of scope

- **Threats against Google's services.** Report via Google's Vulnerability Reward Program.
- **Threats requiring physical access to the operator's deploy machine** or Fly account compromise.
- **Threats in upstream dependencies.** Mitigated via pip-audit CI gate (post-v1.4) and the v1.3.1 CVE-clearing dep floors (`cryptography ≥ 46.0.7`, `pyjwt ≥ 2.12.0`, `urllib3 ≥ 2.7.0`, `requests ≥ 2.33.0`). Report novel CVEs upstream.
- **Misconfigurations that require operator action to exploit** (e.g., setting `OAUTHLIB_INSECURE_TRANSPORT=1` on a production Fly deploy). The startup code in `oauth_google.py` raises if it detects this; further footguns documented in `RUNBOOK.md` § 4.
- **Generic web-app threats fully handled by Starlette/FastMCP** (e.g., HTTP smuggling between the Fly proxy and uvicorn; relies on upstream's correctness).

## 7. STRIDE per component (PR-Δ2)

The surface table in § 4 is organized by attack surface; this section
re-cuts the analysis by component so a reviewer tracing one moving
part of the system can see every STRIDE category against it. STRIDE
columns: **S**poofing, **T**ampering, **R**epudiation, **I**nformation
disclosure, **D**enial of service, **E**levation of privilege.

| Component | S | T | R | I | D | E |
|---|---|---|---|---|---|---|
| `BearerTokenMiddleware` (header) | constant-time `hmac.compare_digest` against the bytes form of `MCP_BEARER_TOKEN`; rejects on mismatch | header is request-scope only; never persisted | every 401 logged with truncated request id; full bearer never logged | bearer never echoed in error responses; constant-time compare leaks no length info | rate-limit deferred (PR-Δ3) — open issue | scope-limited to `/api/*` + `/info`; can't escalate to `/mcp`, `/oauth/*`, or `/.well-known/*` |
| `BearerTokenMiddleware` (signed URL) | HMAC over canonical `(exp, nonce, max, uid)` tuple verifies caller identity; `uid` is the cryptographic anchor | tampered `uid` → HMAC mismatch → reject; tampered `exp` → same | `signed_url_user_id` stashed on `request.state` for downstream audit logging | URL itself is the credential; URL secrecy is the residual risk (documented in § 4 row 5) | replay blocked by single-use `NonceStore`; expired URLs rejected by `exp` check | per-user `uid` bound at mint time; signed URL for user A cannot be repurposed to write into user B's Drive |
| `oauth_google_api_callback` | HMAC-signed `state` parameter pins user identity through the consent dance | tampered `state` → HMAC fail → 4xx HTML error page | every callback logged with user_id; PKCE pre-flight rejection logged separately | `client_secret` stripped from persisted creds via `save_credentials_json` (regression-pinned by `test_oauth_callback_endpoint_strips_operator_secrets_in_production`) | nonce store caps replay; rate-limit deferred (PR-Δ3) | callback can ONLY persist creds keyed by the `sub` claim Google returns; no path to write into another user's row |
| `convert_endpoint` (`/api/convert`) | signed-URL `uid` OR bearer (operator) — per-user dispatch via `request.state.signed_url_user_id` | multipart parser is Starlette's; per-part size cap (deferred to v1.4) | endpoint logs `uid`, doc id, conversion outcome; full bearer never logged | per-user creds for signed-URL path means no cross-user file access via this endpoint | declared `Content-Length` capped by `BodySizeLimitMiddleware`; full decompressed-size cap still open | bearer-header path uses operator creds (single-tenant — by design for smoke tests); signed-URL path uses per-user creds (multi-tenant gate) |
| `user_store` (SQLite at `/data/user_state.db`) | rows keyed by Google `sub` claim; no cross-row reads at the facade | `save_credentials_json` enforces stripping operator secrets; `save_state` schema validates known columns only | WAL mode preserves crash-recovery audit trail | operator has read access to all rows (inside-trust) — explicitly out of scope per § 3 | per-row write contention bounded by per-user `threading.Lock` | facade methods are the only public API; no path to read another user's row from a tool call |
| `keys.get_key(purpose)` | each purpose returns bytes via HKDF-SHA256 (post v2.0b strict-flip); short master refused at startup | shim-active hit counters surface unexpected derivation drift via `/info` | purpose-keyed access logged with first-call-age telemetry | each purpose's bytes never logged — only counters | call-counter is process-local; aggregates across replicas at read time | per-purpose isolation: leaking the `api_bearer` derived bytes does NOT enable signing fresh `signed_url` tokens |
| `TrustedHostMiddleware` (wrapped) | host allowlist from `derive_trusted_hosts()`; fails closed on Fly without `FLY_APP_NAME` | header is request-scope; never persisted | rejected requests logged with the rejected Host | rejected requests return generic 400 — no allowlist leakage | cheap O(1) header check at request entry; pre-body | `/health` bypassed (`HealthExemptTrustedHostMiddleware`, v2.3.3) to satisfy Fly internal probes; every other route still gated |
| Apps Script Web App (`/exec`) | `access: ANYONE_ANONYMOUS`, but every POST is authenticated by a per-user HMAC signature verified in `doPost` (v2.0c) — fails closed on missing/stale/mismatch | request body validation in `restructure.gs` + HMAC binds `"<timestamp>.<body>"` so a tampered body invalidates the signature | Apps Script execution log preserved by Google for 7 days | URL leak alone no longer suffices — an attacker also needs the per-user 256-bit key (never logged/echoed) to forge a request | timestamp + skew window (5 min) blocks replay of a captured (body, signature) pair; Google's per-script rate limits also apply | script always runs as `USER_DEPLOYING` (the user who deployed it); cannot escalate to another user's account |

The cells above mostly RESTATE controls already pinned by tests in §§ 4
and elsewhere; this view is for reviewers who want a STRIDE checklist
rather than a surface list.

## 8. Bounded blast radius — architectural callout (PR-Δ2)

A central security property of this codebase that's not obvious from
the surface table:

> **No single token, key, or credential held by the server grants
> cross-user access.**

The reasoning, layer by layer:

1. **Per-user OAuth tokens.** The cloud-mode `user_state` schema keys
   every Google OAuth token by Google's `sub` claim (the user's
   unique account identifier). The server holds N tokens for N users,
   each scoped to that user's Drive only. There is NO server-side
   "admin" Google token with cross-account read access.

2. **Per-user signed-URL HMAC binding (v2.1).** The signed-URL upload
   path embeds `uid` in the canonical signing tuple. A URL minted for
   user A is cryptographically pinned to user A's identity; an
   attacker who captures it cannot repurpose it to write into user
   B's Drive (HMAC fails on `uid` swap).

3. **Per-user Apps Script Web App.** Each user deploys their own
   `/exec` URL under their own Google account. The script runs as
   `USER_DEPLOYING` (i.e. as that user) and acts only on docs that
   user owns. Access is gated by a per-user HMAC signature verified in
   `doPost` (§ 4 row 5, closed in v2.0c) — and even if both the URL and
   the key leaked, the blast radius is the leaking user's docs only.

4. **Per-purpose key derivation (v2.0b strict-flip).** The three
   logical keys (`api_bearer`, `oauth_state`, `signed_url`) are
   HKDF-derived from one master with distinct info strings. Compromise
   of the bytes for one purpose does NOT enable signing under another.
   This is the inside-trust mitigation: even an operator who sees the
   bearer's derived bytes in a log can't forge OAuth state tokens.

5. **No cross-user tool call paths.** Every `@workspace_tool(creds=True)`
   tool receives the caller's per-user creds via
   `_tool_helpers._get_credentials()`. There is no tool, public or
   private, that takes a target `user_id` parameter and operates on
   that user's Drive.

The architecturally bounded blast radius is *why* an inside-trust
compromise of the operator's deploy credentials is out of scope per
§ 3 — it would expose **the operator's machine** + **everything Fly
gives them**, but the cross-user isolation above means it does NOT
hand the attacker a cross-tenant credential they can use to harvest
all users' docs in one step. The attacker would have to compromise
each user's OAuth refresh token individually, which is operator-
level access to user_state.db, which IS the inside-trust scenario
the model already calls out.

## 9. What we currently don't defend against (honest section, PR-Δ2)

A security posture document that only lists wins is theatre. This
section catalogs the open gaps the team is aware of, so a reviewer
or external user can make an informed call about whether the
posture matches their threat model.

| # | Gap | Why open | Compensating control today | Planned closure |
|---|---|---|---|---|
| ~~1~~ | ~~**Apps Script Web App `/exec` is `ANYONE_ANONYMOUS`**~~ **RESOLVED (v2.0c)** | Was open while the HMAC verify-path was schema-only. Now wired: `restructure.gs::doPost` verifies a per-user `Utilities.computeHmacSha256Signature` over `"<timestamp>.<body>"` against the `X-MCP-Signature` header (fail-closed), and `_call_webapp` signs every POST. See § 4 row 5. | n/a — closed | DONE (v2.0c) |
| 2 | **No request-rate limit on `/api/convert`, `/info`, or `/oauth/google/api/callback`** | All three endpoints rely on per-request CPU/memory bounds (body size, multipart parser caps) rather than a rate limit. A coordinated attacker can hit any of them at full network speed | `BodySizeLimitMiddleware` 10 MB declared-length cap + Fly's per-machine 512 MB ceiling bound a single request's damage. Fly's edge proxy applies its own per-IP rate limit (~ free-tier defaults; not under our control) | PR-Δ3 — adds an in-process token-bucket per `(endpoint, principal)` |
| 3 | **No full decompressed-size cap on .docx uploads** | `BodySizeLimitMiddleware` only checks `Content-Length`; a 10 MB .docx with quadratic-blowup XML can still degrade the converter | Output-size cap by Drive (50 MB conversion ceiling) + bounded by Fly VM (512 MB); python-docx parsing failure raises before the conversion call | v1.4 (carry-forward from § 4 row 2) |
| 4 | **`user_state.db` is plaintext SQLite** | Disk encryption on Fly's volumes is platform-level; we don't add an application-layer encryption (would require key management we don't have) | The Fly volume is private to the machine; operator SSH is the only path to read it. Inside-trust per § 3 | Not planned (correct trade-off for the current threat model) |
| 5 | **Operator's `MCP_BEARER_TOKEN` is the master for all derived keys** | Single-master design simplifies operator onboarding (one secret to rotate). Compromise of the master invalidates everything | 32-char minimum enforced at startup; rotation procedure documented in `RUNBOOK.md` § 3.4 | Per-purpose master rotation (independent `MCP_API_BEARER_KEY` / `OAUTH_STATE_SIGNING_KEY` / `SIGNED_URL_SIGNING_KEY`) is supported today via env overrides; operators who want stronger isolation can already opt in |
| 6 | **No formal third-party audit** | Small open-source project; no budget for paid pen-test or CASA assessment | Self-attested against OWASP ASVS Level 1 (see `docs/asvs-level-1-checklist.md`); OpenSSF Scorecard runs in CI for continuous posture monitoring | Tracked as long-term goal; will publish results if/when commissioned |
| 7 | **No nation-state / supply-chain threat coverage** | Out of scope per § 3 — would require Anthropic / Google / Fly compromise, all outside our control | SHA-pinned base image; SHA-pinned third-party GitHub Actions (PR #51); dependabot-tracked lockfile; CodeQL static analysis | Sigstore-signed release artifacts (PR-Δ2) raise the supply-chain bar for downstream consumers |
| 8 | **`drive.readonly` (the only RESTRICTED scope) has been DROPPED; base tier is sensitive-scope-only** | This row formerly listed `drive.readonly` as a retained restricted scope for the cloud-chat `gdocs_tab_existing_doc(drive_file_id=…)` ingestion path. It was subsequently removed from the base tier: the two consumers were re-plumbed onto Drive-read-free paths (signed-URL `.docx` upload; signed staging endpoint for the slides-to-video frame handoff) | No restricted scope is requested at all, so no CASA security assessment is triggered. The cloud-chat ingestion UX uses the signed-URL upload path (`gdocs_get_signed_upload_url`, POST to `/api/convert`), which stages bytes server-side with no Drive read | RESOLVED: `drive.readonly` removed; see `auth.py:WORKSPACE_SCOPES`. A future "read ANY Drive file" feature would reintroduce it on a SEPARATE restricted tier |

Honest assessment: with the Apps Script HMAC verify-path now landed
(former #1, RESOLVED in v2.0c), the most operationally-significant open
item is **#2 (rate limiting)**, since DoS via repeated `/api/convert`
POSTs is the most reachable attack today. PR-Δ3 will close #2.
