# Migration Guide — v1.x → v2.x

**Audience:** operators running an active v1.x Fly.io deployment of google-docs-mcp who want to upgrade to v2.x cleanly.

This guide assumes you're already running v1.5.x. If you're on an older v1.x release (v1.3.1, v1.4.0a, etc.), upgrade to the latest v1.5.x first — it ships the env-var key overrides, denominator counter, and preflight script that the v2.x cutover depends on.

Stdio (Claude Desktop / Claude Code) users do not need to read this. The stdio surface is unchanged across v1 → v2; running `pipx upgrade google-docs-mcp` is the entire migration.

## 1. Why upgrade

- **Security.** v2.0b ships the HKDF strict-flip: the back-compat shim that returned the raw `MCP_BEARER_TOKEN` for `api_bearer`, `oauth_state`, and `signed_url` purposes is removed. After the flip, all three keys are derived via HKDF-SHA256 from the master with purpose-specific `info` strings. This closes the shim-derived-token attack class (THREAT_MODEL §5).
- **New tools.** v2.2b added `gdocs_help(error_message)` for LLM-callable error-recovery lookup. v2.3 (planned) adds `gdocs_admin_audit` for cross-tenant incident investigation (RUNBOOK §2.8).
- **Better observability.** Shim-hit counter, denominator (`key_call_totals`) counter, and the `preflight_strict_flip.sh` script that asserts both halves of the soak signal before the flip is safe to merge.
- **Per-user Apps Script HMAC.** v2.0a's `apps_script_hmac_key` schema lets the server sign outbound POSTs to each user's Apps Script Web App with a per-user key, closing THREAT_MODEL §4 row 5's "anyone with the user's /exec URL can mutate any doc the user owns" gap.
- **Reliability.** v2.0.2's `auto_stop_machines = "off"` flip eliminates the stuck-stopped Fly machine failure mode (see CHANGELOG; +$1.30/mo for always-on).

## 2. Breaking changes

### 2.1 HKDF strict-flip invalidates in-flight tokens

Once v2.0b activates, every signed upload URL and every OAuth callback state token minted under v1.x ceases to verify. Two impact dimensions:

- **Signed upload URLs** have a 10-min TTL by default (`gdocs_get_signed_upload_url`). In-flight URLs at cutover-moment expire within 10 minutes — wait the window out, OR notify users to request fresh URLs.
- **OAuth callback state tokens** also have a 10-min TTL (`oauth_state.py`). A user partway through the auth dance at cutover-moment will fail on the callback and need to restart the flow. Tell users in advance if this matters to them; otherwise they'll just hit "Google access required" and click through again.

To avoid both, run the preflight script (§3 step 5) and only flip when it confirms zero in-flight traffic.

### 2.2 `apps_script_hmac_key` becomes required for all users

The v2.0a migration adds the `apps_script_hmac_key` column to `user_state` and starts provisioning a fresh 64-char hex key per user at `gdocs_setup_apps_script` time. Existing users (rows that pre-date v2.0a) have `NULL` in this column — the migration script (`scripts/migrate_existing_users.py`) backfills them.

If you skip the migration step and deploy v2.0b directly, existing users will hit a `ToolError("user is missing apps_script_hmac_key; re-run gdocs_setup_apps_script")` on their next retrofit call. Inconvenient, not data-destructive.

### 2.3 Removed tools

**None.** auth-auditor confirmed: zero tools were removed across v1 → v2. The v2.0.1 cleanup (PR #37) walked back the previously-planned `gdocs_update_tabs` and `gdocs_set_trashed` superseders, so `gdocs_rename_tab`, `gdocs_set_tab_icons`, `gdocs_trash_file`, and `gdocs_untrash_file` remain first-class. See `docs/COMPATIBILITY_POLICY.md` § 3.

## 3. Migration steps

Order matters. Do not skip the preflight check.

### Step 1 — pre-upgrade to latest v1.5.x

```bash
flyctl deploy --image registry.fly.io/sundeepg98-docs-mcp:<v1.5.x-tag> -a sundeepg98-docs-mcp
flyctl status -a sundeepg98-docs-mcp     # expect: machine 'started', health 200
```

Confirm the deployed version reports `v1.5.x` via `gdocs_server_info()` from any connected client.

### Step 2 — dry-run the migration

```bash
flyctl ssh console -a sundeepg98-docs-mcp
cd /app
python scripts/migrate_existing_users.py --dry-run
```

The dry-run prints which `user_state` rows would receive a backfilled `apps_script_hmac_key` and exits without writing. Capture the output — the row count tells you how many users need the backfill.

### Step 3 — stop the server (per-user-lock is in-process)

```bash
flyctl machine list -a sundeepg98-docs-mcp        # capture machine ID
flyctl machine stop <id> -a sundeepg98-docs-mcp
```

Required: the migration script takes a per-user write lock on `user_state.db` and the running server holds long-lived connections to the same SQLite file. Concurrent writes from migration + server = SQLite lock contention. Stop first.

### Step 4 — apply the migration

```bash
flyctl ssh console -a sundeepg98-docs-mcp
cd /app
python scripts/migrate_existing_users.py --apply
```

The script provisions one 64-char hex `apps_script_hmac_key` per legacy user, writes back to `user_state.db`, and prints `migrated: <count> users`. Verify the row count matches the dry-run from Step 2.

### Step 5 — restart and run the preflight

```bash
flyctl machine start <id> -a sundeepg98-docs-mcp
./scripts/preflight_strict_flip.sh https://sundeepg98-docs-mcp.fly.dev "$MCP_BEARER_TOKEN"
```

The preflight asserts both halves of the soak signal: (1) `shim_hits == 0` (nobody actively using the back-compat shim) AND (2) `total_calls >= 100` (counter has enough signal to trust the zero). See RUNBOOK § 3.6 for the full preflight semantics and exit codes.

**If the preflight exits 4 (`shim_hits > 0`):** you have real in-flight traffic on the shim. DO NOT flip. Wait at least one full signed-URL TTL window (24h default) and re-run. Repeat until preflight passes. See RUNBOOK § 3.5 for the recovery procedure if the cutover ever IS triggered while shim_hits > 0.

**If the preflight exits 3 (`total_calls < 100`):** the counter has not collected enough signal. Generate synthetic traffic (a handful of `gdocs_server_info()` calls from any connected client) or wait for organic traffic, then re-try.

**If the preflight exits 0:** proceed to Step 6.

### Step 6 — deploy v2.0.x

```bash
flyctl deploy --image registry.fly.io/sundeepg98-docs-mcp:<v2.0.x-tag> -a sundeepg98-docs-mcp
```

Health-check via `curl https://sundeepg98-docs-mcp.fly.dev/health` — should return 200 within 30s.

Confirm `gdocs_server_info().version` reports `v2.0.x` from any connected client. The `key_back_compat_shim_active_hits` field should be present in the response and all values should be zero (or absent — the shim is gone).

## 4. Rollback

If anything goes wrong after Step 6, rollback is fast.

```bash
flyctl deploy --image registry.fly.io/sundeepg98-docs-mcp:<v1.5.x-tag> -a sundeepg98-docs-mcp
```

**Caveat on rollback after a successful flip:** any signed URLs or OAuth state tokens that were minted during the v2.0.x window are derived via HKDF and use a different key value than v1.5.x's raw-master path. Rolling back means those v2.0.x-minted tokens become invalid. Users with in-flight tokens at rollback-moment may need to re-consent or request fresh URLs.

Best-case rollback downtime: minutes (one `flyctl deploy --image` cycle).
Worst-case user impact at rollback: forced re-consent for users with in-flight tokens — same blast radius as the forward flip, just in the other direction.

The `user_state.db` schema is forward-compatible. v1.5.x ignores the `apps_script_hmac_key` column added by the v2.0a migration. No DB-level rollback needed.

## 5. Cross-references

- `docs/RUNBOOK.md` § 3.5 — strict-flip recovery procedure (what to do if the flip activates while shim_hits > 0)
- `docs/RUNBOOK.md` § 3.6 — preflight check details (exit codes, multi-replica caveat, what to do on each failure mode)
- `docs/THREAT_MODEL.md` § 5 — cryptographic key inventory; explains why the strict-flip closes a real attack class
- `docs/COMPATIBILITY_POLICY.md` § 4 — what generally breaks across major versions; § 2 — v1.x EOL window
- `scripts/migrate_existing_users.py` — the schema backfill referenced in Step 2 + Step 4
- `scripts/preflight_strict_flip.sh` — the soak-signal assertion referenced in Step 5
- `src/google_docs_mcp/keys.py` — the HKDF derivation + `_BACK_COMPAT_RAW_MASTER` shim
- CHANGELOG.md — the per-release notes that anchor "v1.5.x", "v2.0.x", etc. to specific commits
