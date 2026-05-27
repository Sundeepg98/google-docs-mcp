# Backup + Restore — Litestream → Cloudflare R2

**Audience:** the operator deploying `google-docs-mcp` to Fly.
**Reading time:** 5 min to activate, 2 min to restore.

## What this gets you

- **RPO ~1 second.** Litestream streams SQLite WAL frames to your R2
  bucket continuously. A Fly machine crash loses at most ~1s of
  writes.
- **7-day point-in-time recovery.** Snapshots every 24h + WAL
  retention 168h means you can restore the DB to any moment in the
  last week (configurable in `litestream.yml`).
- **$0 cost.** Cloudflare R2's free tier is 10 GB storage + zero
  egress charges. The `user_state.db` lives well under 10 GB at
  personal scale (~few KB per registered user).

## Why this matters

The Fly volume mounted at `/data` holds:
- Every user's OAuth refresh token (`user_state.db`)
- Per-user Apps Script Web App URLs
- Per-user signing keys (`apps_script_hmac_key` column)

Volume loss = **every user must re-authorize Google** + re-deploy
their Apps Script. Without backup, "platform churn" is a permanent
service event for your entire user base. With this runbook done,
it's an "oh, let me restore from yesterday's snapshot" moment.

## One-time activation (~5 minutes)

### 1. Create a Cloudflare R2 bucket (free)

If you don't already have a Cloudflare account, sign up at
<https://dash.cloudflare.com/sign-up>. R2 doesn't require a credit
card for the free tier.

1. In the Cloudflare dashboard, navigate to **R2 → Overview**.
2. Click **Create bucket**.
3. Name it `gdmcp-backup` (or whatever you like — you'll set this
   value as a Fly secret in step 3 below).
4. Pick a location hint near your Fly region (or leave **Auto**).
5. Click **Create bucket**. You're done with the bucket UI.

### 2. Generate an R2 API token

R2 uses S3-compatible API tokens, distinct from Cloudflare's general
API tokens. Generate one scoped to your bucket only:

1. Still in R2's dashboard, click **Manage R2 API Tokens**.
2. Click **Create API Token**.
3. **Permissions:** "Object Read & Write".
4. **Specify bucket:** select the `gdmcp-backup` bucket from step 1.
5. **TTL:** leave as "Forever" (you can rotate later).
6. Click **Create**.
7. **Copy three values** (you only see them once — Cloudflare doesn't
   re-display the secret after this page):
   - `Access Key ID`
   - `Secret Access Key`
   - `Endpoint for S3 Clients` — looks like
     `https://<account-id>.r2.cloudflarestorage.com`

### 3. Set the four Fly secrets

```bash
fly secrets set \
  LITESTREAM_BUCKET="gdmcp-backup" \
  LITESTREAM_ENDPOINT="https://<account-id>.r2.cloudflarestorage.com" \
  LITESTREAM_ACCESS_KEY_ID="<access-key-from-step-2>" \
  LITESTREAM_SECRET_ACCESS_KEY="<secret-from-step-2>"
```

Fly automatically restarts the app after `secrets set`. The
container's entrypoint (`scripts/entrypoint.sh`) detects
`LITESTREAM_BUCKET` is set on next boot and runs the server under
`litestream replicate -exec`.

### 4. Verify backup is running

A minute after the post-secrets restart, check the Fly logs:

```bash
fly logs | grep litestream
```

You should see a line like:

```
litestream: snapshot written to s3:gdmcp-backup/user_state/...
litestream: write wal segment ...
```

If you see `entrypoint: LITESTREAM_BUCKET unset — running google-docs-mcp without backup replication`, the secrets aren't propagating; double-check `fly secrets list` shows all four.

For an active verification, run:

```bash
fly ssh console -C 'litestream restore -if-replica-exists -o /tmp/test_restore.db /data/google-docs-mcp/user_state.db'
```

If this succeeds (exit 0) and `/tmp/test_restore.db` exists, the
replica is reachable and contains a recent snapshot.

## Disaster recovery — restore from backup

**Scenario:** Fly volume is gone (corrupted, accidentally deleted, lost
to platform churn). The container is up but `/data/google-docs-mcp/user_state.db`
is missing or empty. Users start hitting "needs reauth" on every tool
call because the per-user OAuth rows are gone.

### Step 1 — SSH in and confirm the loss

```bash
fly ssh console
ls -lah /data/google-docs-mcp/
# Expect: no user_state.db, or a 0-byte file
```

### Step 2 — Restore

```bash
litestream restore -o /data/google-docs-mcp/user_state.db \
  s3://${LITESTREAM_BUCKET}/user_state
```

This pulls the most recent snapshot + WAL frames from R2 and writes
the reconstructed DB to the path. RPO is ~1 second from whatever
moment the volume was lost.

### Step 3 — Restart the app

```bash
exit              # drop the SSH session
fly apps restart sundeepg98-docs-mcp
```

The next request from any user hits the restored DB. They do not
re-authorize Google — the OAuth tokens were preserved bit-for-bit
in the snapshot.

### Step 4 — (optional) Restore to a specific point in time

If the loss was caused by a bad write (e.g. an admin tool mis-edit
clobbered rows), you can restore to a moment BEFORE the bad write:

```bash
litestream restore -timestamp 2026-05-27T14:30:00Z \
  -o /data/google-docs-mcp/user_state.db \
  s3://${LITESTREAM_BUCKET}/user_state
```

The timestamp is the cutoff: every WAL frame up to that moment is
applied; frames after it are dropped. Within the 168h retention
window configured in `litestream.yml`.

## Rotating R2 credentials

Generate a fresh R2 API token (step 2 of activation), then:

```bash
fly secrets set \
  LITESTREAM_ACCESS_KEY_ID="<new-access-key>" \
  LITESTREAM_SECRET_ACCESS_KEY="<new-secret>"
```

The container restarts; litestream picks up the new creds. No data
re-sync needed — the bucket contents survive the credential rotation.

## Costs

- **R2 free tier:** 10 GB storage, zero egress charges
- **Expected DB size:** a few KB per registered user; 10 GB covers
  ~500k+ users at personal scale
- **R2 Class A operations (writes):** 1M/month free; we use ~86k/day
  (1 WAL push per second × 86400s), so ~2.6M/month
- **Bottom line:** if your user base grows past ~20 registered users
  with active write patterns, you'll exceed the free Class A
  operations cap. R2 paid pricing is $4.50 per million writes — a
  full month of always-on litestream at 1s sync interval costs
  ~$8/mo. If/when you hit this, the fix is to widen
  `sync-interval` in `litestream.yml` from `1s` to `10s` or `30s`
  (accepts ~10-30s RPO in exchange for a 90-95% drop in write count).

## Disabling backup

Set the env vars to empty to revert to no-replication mode:

```bash
fly secrets unset LITESTREAM_BUCKET LITESTREAM_ENDPOINT \
  LITESTREAM_ACCESS_KEY_ID LITESTREAM_SECRET_ACCESS_KEY
```

The container restarts and `entrypoint.sh` falls through to plain
`google-docs-mcp`. The R2 bucket retains its snapshots until you
delete them (no auto-expiry).
