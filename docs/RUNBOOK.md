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

**Currently blocked on missing `gdocs_admin_audit` tool** (v2.x roadmap). For now:

1. Ask the customer for: (a) timestamp of the operation (UTC, ±5 min), (b) which Google account they THOUGHT they were authenticated to, (c) which Google account the doc ACTUALLY landed in
2. Cross-reference timestamps in `flyctl logs --since 24h | grep <approx-timestamp>` — but user_id is logged only as `user_id_prefix[:8]`, so correlation is approximate
3. SQL on `user_state.db`:
   ```sql
   SELECT user_id, updated_at, datetime(updated_at, 'unixepoch')
   FROM user_state ORDER BY updated_at DESC LIMIT 20;
   ```
   Look for two rows updated within seconds of each other — possible session confusion (user has two Google accounts; switched mid-flow)
4. If no plausible benign explanation surfaces in 30 min, treat as POTENTIAL real bug; do not deny without evidence. File a P0 issue.

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
