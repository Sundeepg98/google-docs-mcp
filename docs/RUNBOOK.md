# Operations Runbook — google-docs-mcp

**Audience:** the operator (you), woken at 3am by a PagerDuty alert or a GitHub issue. Read top-down; jump to the matching outage class.

## 1. Outage classes (index)

- §2.1 — Users report retrofit broken
- §2.2 — Mass outage (multiple users affected simultaneously)
- §2.3 — `/health` returning 5xx
- §2.4 — Latency spike on `/api/convert` or MCP tool calls
- §2.5 — OAuth callback failing
- §2.6 — Deploy gone wrong (machine won't start / health-check failing post-deploy)
- §2.7 — SQLite corruption suspected
- §2.8 — Cross-tenant data leak report

## 2. Diagnostic sequences

### 2.1 Users report retrofit broken

**Step 1: read `gdocs_server_info()`** from any Claude client. Check, in order:
- `version` — is the deployed version what you think?
- `test_suite.status` — `"passed"` / `"unknown"` / `"tampered"`. Anything other than `"passed"` = the deploy bypassed CI gates
- `mutation_check.status` — same
- `git_commit` — matches the latest on `main`?

**Step 2: SQL on `user_state.db`** (SSH into Fly machine, `flyctl ssh console -a sundeepg98-docs-mcp`):

```sql
SELECT user_id,
       google_creds_json IS NOT NULL AS has_creds,
       apps_script_url IS NOT NULL AS has_url,
       updated_at
FROM user_state
ORDER BY updated_at DESC LIMIT 10;
```

**Step 3: decision tree based on row state:**
- `has_creds=false` → user never authorized OR was reset → instruct user: run any tool, click auth link
- `has_creds=true, has_url=false` → user authorized but never ran `gdocs_setup_apps_script` → instruct user: run it once
- `has_creds=true, has_url=true` → fully set up. Suspect transient: Google quota / Apps Script daily runtime cap / network. Continue to §2.4.

**Step 4: fix-or-escalate.** Post recovery instruction in the GH issue. If pattern affects multiple users → escalate to §2.2.

### 2.2 Mass outage

Multiple unrelated users report failures within the same hour.

```bash
flyctl status -a sundeepg98-docs-mcp
flyctl logs -a sundeepg98-docs-mcp --since 1h | grep -E '(ERROR|CRITICAL)' | head -50
```

Check Google's status dashboard: https://status.cloud.google.com. If `Apps Script` or `Drive API` shows red, the outage is not us. Tell users "Google's API is degraded, please retry in 30 min" via the GH issue tracker.

If Google is green: suspect a recent deploy. Run `flyctl releases -a sundeepg98-docs-mcp`; if the most recent release timestamp is within the outage window, **roll back**: `flyctl deploy --image registry.fly.io/sundeepg98-docs-mcp:<previous-sha>`.

### 2.3 `/health` returning 5xx

```bash
flyctl status -a sundeepg98-docs-mcp
flyctl logs -a sundeepg98-docs-mcp --since 5m | grep -E '(ERROR|CRITICAL|Traceback)'
```

Common signatures and responses:
- `database is locked` → SQLite WAL contention. Safe to restart: `flyctl machine restart <id>`. WAL auto-recovers; in-flight OAuth callbacks have 10-min TTL and will succeed on retry. See `THREAT_MODEL.md` §5 for the consequences of in-flight token invalidation.
- `OSError: [Errno 28] No space left on device` → `/data` volume full. Run `flyctl ssh console -a sundeepg98-docs-mcp` → `df -h /data`. If >95%, delete old `*.db-wal` files (auto-recreated): `rm /data/google-docs-mcp/*.db-wal-OLD`. Long-term: §2.7 covers retention sweep.
- `RuntimeError: MCP_BEARER_TOKEN must be ≥32 chars` → operator set a short token. Update via `flyctl secrets set MCP_BEARER_TOKEN=$(openssl rand -hex 32)`; machine restarts automatically.
- `oauthlib.oauth2.InvalidClientError` → Google OAuth client_secret expired or revoked. Regenerate in Google Cloud Console; update via `flyctl secrets set GOOGLE_OAUTH_CLIENT_SECRETS_JSON='...'`. ALL active users must re-consent (no `client_id` rotation supported — see §4 footguns).

If none match, the `/health` failure is novel. Capture the traceback, file an issue, restart the machine to clear if the bug is non-deterministic.

**Safe to restart anytime** — in-flight OAuth callbacks have 10-min TTL and will succeed on retry; signed URLs same.

### 2.4 Latency spike on `/api/convert` or MCP tool calls

Baseline: p50 ~800ms, p99 ~3s for `/api/convert`; MCP tool calls vary by tool but typically <2s for read tools, <30s for `gdocs_tab_existing_doc` retrofit (Apps Script-bound).

```bash
# Check Google status first — most spikes are upstream
curl -s https://status.cloud.google.com/incidents.json | head -50

# Volume check — are we just busy?
flyctl logs --since 10m -a sundeepg98-docs-mcp | grep '/api/convert' | wc -l
```

Triage:
- p99 between 800ms and 5s, sustained → likely cold-Apps-Script. Normal.
- p99 5-30s → likely Google API transient. Wait 5 min; usually self-resolves.
- p99 > 30s, sustained > 10 min, with errors → escalate to §2.2 mass-outage flow.

**DO NOT scale machines.** `user_state.db` is SQLite on a per-machine volume (`gdmcp_data`), so any second machine would diverge. `fly.toml` does NOT currently pin a `max_machines_running` cap — the safety is operator discipline, not config. See §4.

### 2.5 OAuth callback failing

Symptom: users click the auth link, see an error page instead of "Google access granted."

```bash
flyctl logs --since 30m -a sundeepg98-docs-mcp | grep 'oauth:' | head -20
```

Common signatures:
- `oauth: callback rejected: OAuth state could not be validated` → either the user took >10 min between auth-URL generation and clicking (TTL exceeded), or the URL was shared/captured and replayed (nonce already consumed). Tell user: re-run the tool to get a fresh auth URL.
- `oauth: client_config load failed` → `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` env var missing or malformed. `flyctl secrets list`; if missing, set it.
- `oauth: MCP_BEARER_TOKEN unset; cannot verify state` → same as the `/health` case in §2.3 above.
- `Failed to exchange auth code with Google: invalid_grant` → user clicked a stale URL OR Google OAuth client was deleted/suspended.

See `THREAT_MODEL.md` §4 row 3 for the threat context behind the state-validation mitigation.

### 2.6 Deploy gone wrong

Most recent `flyctl deploy` returned non-zero, OR `flyctl status` shows machine in `failed` state.

```bash
flyctl releases -a sundeepg98-docs-mcp                  # list recent
flyctl image show -a sundeepg98-docs-mcp                # current image
flyctl logs --since 10m -a sundeepg98-docs-mcp | head   # boot logs
```

Common boot failures:
- `RuntimeError: MCP_BEARER_TOKEN env var must be set` → env var missing; see §2.3
- `RuntimeError: GOOGLE_OAUTH_BASE_URL env var is required` → set it: `flyctl secrets set GOOGLE_OAUTH_BASE_URL=https://sundeepg98-docs-mcp.fly.dev`
- `RuntimeError: FLY_REGION set without FLY_APP_NAME` (v1.3.1+) → TrustedHost fail-closed assertion. Either set `FLY_APP_NAME` or set `TRUSTED_HOSTS=*` (dev only).

Rollback: `flyctl deploy --image registry.fly.io/sundeepg98-docs-mcp:<previous-sha>` where `<previous-sha>` is from `flyctl releases`. **Never deploy via `flyctl deploy --image` from a locally-built image**; bypasses CI mutation gate. See §4.

### 2.7 SQLite corruption suspected

Rare. Symptoms: `database disk image is malformed` in logs.

```bash
flyctl ssh console -a sundeepg98-docs-mcp
sqlite3 /data/google-docs-mcp/user_state.db "PRAGMA integrity_check;"
```

If output is anything other than `ok`, there's real corruption. Recovery:
1. `cp /data/google-docs-mcp/user_state.db /data/google-docs-mcp/user_state.db.broken`
2. `sqlite3 user_state.db.broken ".dump"` → save the SQL
3. Edit the SQL to remove the broken row(s) (identified via `integrity_check` output)
4. `sqlite3 user_state.db.new < dump.sql`
5. `mv user_state.db.new user_state.db`
6. `flyctl machine restart <id>`

Affected users will need to re-authorize. Notify via GH issue.

### 2.8 Cross-tenant data leak report

User reports their doc was created in someone else's Drive, or vice versa.

Use the `gdocs_admin_audit` tool (v2.3+) for the server-side correlation step. The tool is gated by the `MCP_ADMIN_TOKEN` env var, set separately from `MCP_BEARER_TOKEN`; if it isn't set on the production server, set it via `fly secrets set MCP_ADMIN_TOKEN=...` and `fly machines restart` before running.

1. Ask the customer for: (a) timestamp of the operation (UTC, ±5 min), (b) which Google account they THOUGHT they were authenticated to, (c) which Google account the doc ACTUALLY landed in
2. Call `gdocs_admin_audit` against both candidate `user_id` values (the `sub` claim from each account's OAuth state):
   ```
   gdocs_admin_audit(admin_token=$MCP_ADMIN_TOKEN, user_id="abc123", since_hours=24)
   ```
   The response surfaces `updated_at` for that user's row plus a `user_id_prefix` you can use to grep flyctl logs. `since_hours` accepts 1-168 (1 hour to 1 week); use the tightest window that brackets the reported event.
3. Cross-reference the returned `user_id_prefix` against `flyctl logs --since 24h` for per-operation detail — the tool intentionally returns only row-level timestamps because that's the audit granularity `user_state.db` currently has (finer-grained logging tracked in #25).
4. SQL on `user_state.db` for the wider sweep (look for two rows updated within seconds of each other — possible session confusion if the user has two Google accounts and switched mid-flow):
   ```sql
   SELECT user_id, updated_at, datetime(updated_at, 'unixepoch')
   FROM user_state ORDER BY updated_at DESC LIMIT 20;
   ```
5. If no plausible benign explanation surfaces in 30 min, treat as POTENTIAL real bug; do not deny without evidence. File a P0 issue.

See `THREAT_MODEL.md` §3 (attacker model: authenticated peer) for the architectural reason cross-tenant SHOULD be impossible (per-user `sub`-keyed row isolation).

## 3. Common fixes

### 3.1 Force a fresh OAuth consent for one user

Tell user: run `gdocs_reset_authorization(full=False)` (HTTP mode) — clears their `google_creds_json` only, preserves Apps Script setup. Next tool call returns `needs_authorization` with a fresh auth URL.

### 3.2 Force a fresh Apps Script deploy for one user

Tell user: run `gdocs_reset_authorization(full=True)` then `gdocs_setup_apps_script`. The `full=True` clears `apps_script_url` and forces a brand-new Apps Script project on next setup.

### 3.3 Restart the machine cleanly

`flyctl machine restart <machine-id>` — safe anytime. WAL recovers; signed URLs and OAuth states survive (10-min TTL each).

### 3.4 Rotate keys

- **`MCP_BEARER_TOKEN` (master) — DON'T rotate without prep through v1.5.** Until v1.5 ships strict-flip, rotating the master invalidates EVERY in-flight signed URL AND every OAuth callback simultaneously. Steps if you must: (1) set all three per-purpose overrides to the current master FIRST: `flyctl secrets set MCP_API_BEARER_KEY=<current> OAUTH_STATE_SIGNING_KEY=<current> SIGNED_URL_SIGNING_KEY=<current>`; (2) then rotate master: `flyctl secrets set MCP_BEARER_TOKEN=$(openssl rand -hex 32)`. Net effect: master changes, derived keys pinned to old value.
- **Single derived key rotation** (one of `MCP_API_BEARER_KEY` / `OAUTH_STATE_SIGNING_KEY` / `SIGNED_URL_SIGNING_KEY`): rotate that one only. Invalidates only that purpose's in-flight tokens.
- **`GOOGLE_OAUTH_CLIENT_SECRETS_JSON`**: see §2.5. ALL users must re-consent (Google ties refresh_token to client_id).

### 3.5 Strict-flip recovery (post-v2.0b regression)

**Symptom:** within minutes of a v2.0b deploy, surge of:
- `NeedsReauthError` on tool calls (api_bearer keys no longer validate)
- `oauth: callback rejected: OAuth state could not be validated` (OAuth state HMAC mismatch)
- HTTP 401 on signed upload URLs that worked seconds ago

**Diagnosis:** call `gdocs_server_info()` and read `key_back_compat_shim_active_hits`. If any purpose is non-zero **after** the v2.0b flip, the preflight check (§3.6) was skipped or wrong: real users had in-flight tokens minted under the shim and the strict-flip invalidated them en-masse (R20 risk).

**Recovery:**
1. **Roll back to v1.5.x immediately** — re-enables the shim, in-flight tokens re-validate. See §2.6 for the rollback command.
2. After rollback, watch `gdocs_server_info().key_back_compat_shim_active_hits` for 24h. The rate of shim hits indicates how many users still have in-flight pre-v2.0b tokens.
3. Re-run the §3.6 preflight check. Only attempt v2.0b again once it passes (shim hits in a trailing 24h window are 0, totals show real traffic).
4. **Worst case:** if shim-hit-rate doesn't drop to 0 within a reasonable window (because users keep pinning long-lived signed URLs, for example), v2.0b stays unmerged. The shim is permanent for v1.x; the strict-flip is optional. There is no business reason to force the breaking change.

The trip-wire here is `_BACK_COMPAT_RAW_MASTER` in `keys.py`: it's the single source of truth for "which purposes still route through the shim." A partial flip (remove some purposes, leave others) is also fine — it shrinks the soak target.

### 3.6 Pre-strict-flip preflight check

**Before merging v2.0b** (or any future PR that removes entries from `_BACK_COMPAT_RAW_MASTER`), there is a two-step operator procedure:

**Step 1 — set per-purpose overrides + redeploy.** This is the critical prep. Until overrides are set, every `keys.get_key()` call routes through the back-compat shim, and the preflight gate (Step 2) returns exit 4 forever in steady state — there is no "natural soak" that makes shim_hits drop to 0 without operator action. Use the current master as the value for each override (matches §3.4's rotation pattern — net effect is "pin derived keys to today's master before removing the shim that produces the same bytes by default"):

```bash
CURRENT_MASTER=$(flyctl secrets list -j | jq -r '.[] | select(.name=="MCP_BEARER_TOKEN") | .digest')
# (If the digest path doesn't work in your flyctl version, just paste the
# value you used when you originally set MCP_BEARER_TOKEN. Don't generate a
# fresh value here — the point is "no key material changes for live users.")

flyctl secrets set \
  MCP_API_BEARER_KEY="$CURRENT_MASTER" \
  OAUTH_STATE_SIGNING_KEY="$CURRENT_MASTER" \
  SIGNED_URL_SIGNING_KEY="$CURRENT_MASTER"
# flyctl triggers a redeploy automatically after `secrets set`.
```

After the redeploy completes, `keys.get_key()` resolves each purpose via the override path, bypassing both the shim and HKDF derivation. In-flight tokens minted by the shim continue to verify cleanly because the override returns the same bytes the shim did. **No user-visible disruption.**

**Step 2 — run the preflight after a soak window.** Wait long enough for real traffic to exercise each purpose at least a few times (typically 1h30 is plenty — see `key_observability.first_call_age_seconds` for the elapsed-since-first-call telemetry). Then:

```bash
./scripts/preflight_strict_flip.sh https://sundeepg98-docs-mcp.fly.dev "$MCP_BEARER_TOKEN"
```

The script GETs the bearer-authed `/info` endpoint (v2.6+) and asserts:
- `sum(key_call_totals.values()) >= 100` — counter is sensitive enough to be trusted (no traffic = no evidence)
- `sum(key_back_compat_shim_active_hits.values()) == 0` — every call served by override (or HKDF, post-flip); nothing routes through the shim. This is achievable ONLY after Step 1 above.

The derived `override_hits = total - shim_hits` is logged for operator visibility — when it equals `total` and `shim_hits` is 0, every call landed on the override path, which means removing the shim is provably a no-op for live traffic.

**Exit codes:**
- `0` — green-light, safe to merge v2.0b
- `2` — could not reach `/info` (check URL, bearer, server >= v2.6 deployed)
- `3` — total call count < 100 (let the soak run longer or drive synthetic traffic, see below)
- `4` — shim hits > 0 (Step 1 not done, OR overrides aren't being picked up by the running server — confirm the redeploy completed and the env vars are set: `flyctl secrets list | grep -E "API_BEARER_KEY|OAUTH_STATE_SIGNING_KEY|SIGNED_URL_SIGNING_KEY"`)
- `5` — required field missing from response (server too old; upgrade to >= v2.6 first)

**Driving synthetic traffic** if `total < 100` after a wait:

```bash
# Increment api_bearer via the bearer-header check on /info (cheap, no
# side effects). 200 iterations easily clears the 100-call floor.
for i in $(seq 1 200); do
    curl -fsS -H "Authorization: Bearer $MCP_BEARER_TOKEN" \
        https://sundeepg98-docs-mcp.fly.dev/info >/dev/null
done

# Increment signed_url by minting throw-away signed URLs (TTL is 60s
# so they're harmless if leaked):
for i in $(seq 1 50); do
    fastmcp call --server-spec https://sundeepg98-docs-mcp.fly.dev/mcp \
        --target gdocs_get_signed_upload_url \
        --auth "$MCP_BEARER_TOKEN" \
        --input-json '{"ttl_seconds": 60}' >/dev/null 2>&1
done

# oauth_state is harder to drive synthetically (it requires the Google
# OAuth dance to land in /oauth/google/api/callback). Easier path: ask
# a test user to log in once via claude.ai's connector — that single
# OAuth round-trip increments oauth_state.
```

The 100-call floor is a heuristic for "telemetry is meaningful, not noise." Tune up for higher confidence; never tune down without good reason.

**Multi-replica caveat:** counters are process-local. If Fly ever lifts the single-machine constraint (see §4 footguns), the preflight must aggregate across replicas — currently the operator runs it against ONE machine and trusts the single-machine invariant. Document the rollout cadence (replica restarts reset counters) in the deploy notes for v2.0b.

**Why Step 1 is non-optional (gate-semantics rationale):** removing entries from `_BACK_COMPAT_RAW_MASTER` invalidates every key minted via the shim that's still in flight. Pre-R8, the preflight tried to gate on "wait for shim_hits to drop to 0 naturally" — but during the shim window, every call hits the shim by design, so that never happens. The override-prep step shifts traffic to the override path BEFORE the strict-flip, so by the time the shim is removed, nothing is using it. This is the only safe ordering.

## 4. What NOT to do (footguns)

- **DO NOT** rotate `MCP_BEARER_TOKEN` without first setting all three `*_KEY` overrides. See §3.4.
- **DO NOT** run `flyctl deploy --image` to push a locally-built image. Bypasses CI mutation gate; the deployed `gdocs_server_info().test_suite.status` will be `"unknown"` and you'll have no way to know if the image passed tests.
- **DO NOT** `DELETE FROM user_state` to "clean up a stuck user." Use `gdocs_reset_authorization` (see §3.1/3.2) or NULL the specific field via `UPDATE`.
- **DO NOT** raise the running-machine count above 1 (via `flyctl scale count`, `fly.toml` `max_machines_running`, or autoscaling). Per-machine SQLite split-brain. See `THREAT_MODEL.md` §1 trust boundaries.
- **DO NOT** edit `restructure.gs` in any user's Apps Script web IDE for debugging. v2.0+ content-hash drift detection will lock them out of retrofit; only safe edit path is shipping a new packaged version + having users re-run `gdocs_setup_apps_script`.
- **DO NOT** set `OAUTHLIB_INSECURE_TRANSPORT=1` on the Fly deploy. The server refuses to boot if this is detected, but if you bypass the check via direct uvicorn invocation, you've disabled all transport-security checks across all OAuth flows.

## 5. Escalation

If diagnostic sequence §2.X doesn't resolve in 30 minutes:
1. Capture: `gdocs_server_info()` output, the relevant SQL row state, affected user's GH handle, `flyctl logs` timestamp range
2. File an issue tagged `incident` with all of the above
3. Tag the maintainer (`@Sundeepg98`)
4. If the issue is potential data leak (§2.8) OR potential security exposure: tag `security` ALSO
