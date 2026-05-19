# Threat Model — google-docs-mcp

**Version:** v1.3.1 (post-PR #10 merge)
**Last updated:** 2026-05-19
**Audience:** security reviewers, contributors evaluating PRs that touch auth/persistence/network paths

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
| 5 | Apps Script Web App `/exec` | Anonymous POST → mutate victim docs they own | None today (`access: ANYONE_ANONYMOUS`). v2.0 ships HMAC per-request validation with ±60s timestamp window | OPEN — anyone with the user's `/exec` URL can POST any payload and mutate any doc the user owns. Mitigation: URL secrecy. Closes in v2.0 | (no test today; v2.0 ships `test_restructure_gs_rejects_bad_signature`) |
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

**Per-user keys (v2.0+):** `apps_script_hmac_key` (64-char hex) provisioned per user_state row at `gdocs_setup_apps_script` time, baked into deployed `restructure.gs` as `const _SECRET`. Authenticates POSTs from our server to the user's Web App. Not derived from the master.

**Rotation procedures** live in `docs/RUNBOOK.md` § 3.4.

## 6. Out of scope

- **Threats against Google's services.** Report via Google's Vulnerability Reward Program.
- **Threats requiring physical access to the operator's deploy machine** or Fly account compromise.
- **Threats in upstream dependencies.** Mitigated via pip-audit CI gate (post-v1.4) and the v1.3.1 CVE-clearing dep floors (`cryptography ≥ 46.0.7`, `pyjwt ≥ 2.12.0`, `urllib3 ≥ 2.7.0`, `requests ≥ 2.33.0`). Report novel CVEs upstream.
- **Misconfigurations that require operator action to exploit** (e.g., setting `OAUTHLIB_INSECURE_TRANSPORT=1` on a production Fly deploy). The startup code in `oauth_google.py` raises if it detects this; further footguns documented in `RUNBOOK.md` § 4.
- **Generic web-app threats fully handled by Starlette/FastMCP** (e.g., HTTP smuggling between the Fly proxy and uvicorn; relies on upstream's correctness).
