# Runbook: Activate the Vercel pilot deploy

**Status**: Operator action. PR-Δ6 ships the codebase + CI plumbing; this runbook walks through the manual Vercel-side setup.
**When to use**: when you want a parallel Vercel deploy alongside the existing Fly deploy. Fly stays primary throughout; Vercel is purely additive.
**Cost**: $0/month at personal-tier traffic (Vercel Hobby + Upstash Hobby KV).
**Time**: ~15 minutes one-shot for the full setup; ~30 seconds for subsequent deploys (just push to main).

## Why this is opt-in

The `.github/workflows/deploy-vercel.yml` workflow has a guard: if the `VERCEL_TOKEN` secret isn't set, the workflow logs a warning and exits 0 (doesn't fail the run). So merging PR-Δ6 to main doesn't accidentally trigger a Vercel deploy you haven't set up yet. You complete the steps below at your leisure.

## One-time setup

### 1. Create the Vercel project

Browser steps (Vercel doesn't have a usable CLI flow for project creation):

1. Open https://vercel.com/new
2. Click "Import" next to the `Sundeepg98/google-docs-mcp` repo (or whatever the repo is named post-PR-#135 rename if it transferred).
3. Project name: `appscriptly` (matches the canonical post-rename name).
4. Framework preset: **Other** (Vercel auto-detects Python from `api/index.py` + `vercel.json`).
5. Root directory: `.` (repo root).
6. **DO NOT click "Deploy" yet** — env vars need to be set first.

### 2. Bind Vercel KV to the project

Same dashboard, but switch to the new project's view:

1. Project → Storage → Create New → KV.
2. Database name: `appscriptly-kv` (any name; Vercel doesn't enforce uniqueness across projects).
3. Region: `iad1` (matches `vercel.json`). If you want `bom1`, check the Hobby tier KV availability — it may be Pro-only.
4. Plan: Hobby (free 30 MB; ~6000 user-state rows at our ~5 KB/user footprint).
5. Click "Create".
6. After provisioning, click "Connect Project" and link to `appscriptly`. Vercel will automatically populate four env vars in your project:
   - `KV_REST_API_URL`
   - `KV_REST_API_TOKEN`
   - `KV_REST_API_READ_ONLY_TOKEN` (we don't use; KV operations write)
   - `KV_URL` (we don't use; that's the TCP redis URL)

### 3. Set the rest of the env vars

Project → Settings → Environment Variables → add each (use "Production" + "Preview" scopes unless noted):

**Required for any deploy:**

| Variable | Value | Notes |
|---|---|---|
| `MCP_BEARER_TOKEN` | Same value as your Fly secret | The HKDF master. `fly secrets list -a sundeepg98-docs-mcp` to copy. |
| `GOOGLE_CLIENT_CONFIG` | Same value as your Fly secret | OAuth client secrets JSON. |
| `GOOGLE_OAUTH_BASE_URL` | `https://appscriptly-<hash>.vercel.app` | Vercel's auto-generated preview URL; or your custom domain if bound. |
| `STORAGE_BACKEND` | `vercel_kv` | **Mandatory** for Vercel — SqliteBackend on Vercel's ephemeral tmpfs silently loses every write. |

**Optional (mirror what you have on Fly):**

| Variable | Value | Purpose |
|---|---|---|
| `SENTRY_DSN` | Same DSN as Fly | PR-Δ4 error tracking; same Sentry project receives events from both deploys. |
| `LICENSE_KEY_ENFORCEMENT` | `false` (default) | PR-Δ5 commercial gate; leave off for personal use. |
| `MCP_LICENSE_KEY` | (unset for personal) | PR-Δ5 commercial gate. |
| `GCP_PROJECT_NUMBER` | (unset for personal) | PR-Δ5 Apps Script GCP linking. |
| `TRUSTED_HOSTS` | `*.vercel.app,appscriptly-*.vercel.app,<custom-domain>` | Host allowlist; needed only if Vercel's auto-detection fails. |

### 4. Generate the `VERCEL_TOKEN` for CI

1. https://vercel.com/account/tokens
2. Click "Create Token".
3. Name: `appscriptly-ci`.
4. **Scope: the appscriptly project only** (least privilege — don't grant account-wide).
5. Expiration: 1 year (rotate annually; the rotation runbook is below).
6. Copy the token value immediately (shown once).
7. Save to GitHub secrets:
   ```bash
   gh secret set VERCEL_TOKEN -b "$YOUR_TOKEN_VALUE"
   ```

### 5. Update Google OAuth Console

Add the Vercel URL to the authorized OAuth redirect URIs:

1. https://console.cloud.google.com/apis/credentials
2. Click your OAuth Client ID.
3. Authorized redirect URIs → Add URI:
   `https://appscriptly-<hash>.vercel.app/oauth/google/api/callback`
   (replace `<hash>` with your actual Vercel deploy hash; the URL is visible at the top of the Vercel project dashboard after the first deploy).
4. **Keep the Fly URL too** — both must resolve during the pilot.
5. Save.

### 6. First deploy

Two options:

**Option A (preferred): trigger via GitHub Actions.** Merge any PR or push to main. The `deploy-vercel.yml` workflow runs automatically. Watch it in the Actions tab; the smoke-check step verifies `/health` returns 200.

**Option B: deploy from local CLI.**
```bash
npm install --global vercel@^41
vercel link  # interactive: link to your project
vercel --prod
# Watch the deploy URL, then:
curl https://<deploy-url>/health
# Expect: {"ok":true,"service":"appscriptly"}
```

## Verification

After the first deploy, verify the pilot is healthy:

```bash
# Both deploys should return 200 on /health
curl https://sundeepg98-docs-mcp.fly.dev/health         # Fly
curl https://appscriptly-<hash>.vercel.app/health        # Vercel

# Both should expose RFC 9728 OAuth metadata
curl https://sundeepg98-docs-mcp.fly.dev/.well-known/oauth-protected-resource
curl https://appscriptly-<hash>.vercel.app/.well-known/oauth-protected-resource

# Both should accept the FastMCP transport
# (manual: claude.ai Custom Connector pointed at the Vercel URL)
```

In Vercel function logs (Vercel dashboard → Project → Logs), look for:

- `storage: STORAGE_BACKEND=vercel_kv` resolution log (the selector's debug line). If you see `storage: STORAGE_BACKEND=vercel_kv requested but VercelKvBackend construction failed (...); falling back to SqliteBackend.`, the KV env vars aren't bound — step 2 didn't complete or the project wasn't redeployed after binding.
- `Sentry SDK initialized` if you set `SENTRY_DSN` (step 3 optional row).
- Per-request `[req=<uuid>]` correlation IDs (PR-Δ4 RequestIdMiddleware).

## Test the OAuth + tool-call flow end-to-end

1. Open claude.ai → Settings → Custom Connectors → Add Connector.
2. Server URL: `https://appscriptly-<hash>.vercel.app/mcp` (note the `/mcp` suffix).
3. Click Connect. Claude.ai's connector discovery probes `/.well-known/oauth-protected-resource` (must return 200 — step 6 already verified).
4. OAuth consent screen appears. Authorize.
5. In a new chat: "list my recent Google Docs." Claude calls `gdocs_find_doc_by_title` (or similar). If you see "needs_authorization" with a re-auth URL, the KV write of `google_creds_json` didn't persist — check Vercel function logs for VercelKvBackend errors.

If steps 1-5 all succeed against the Vercel URL, the pilot is live.

## Operating the parallel deploy

During the pilot phase, both Fly and Vercel are running. Some considerations:

- **Users on Fly stay on Fly.** Their OAuth tokens are in Fly's SQLite. Hitting the Vercel URL triggers a re-auth (KV is empty for them).
- **No automatic state migration.** Migrating users between backends is out of scope for the pilot; if you want to cut over Fly users to Vercel atomically, the manual procedure is: dump Fly's SQLite (`fly ssh sftp shell -a sundeepg98-docs-mcp; get /data/user_state.db local.db`), write a one-off script to read each `user_state` row and POST it to Upstash via the REST API, swap the OAuth redirect URI to point at Vercel only.
- **Both deploys share the same `MCP_BEARER_TOKEN`.** That's deliberate — signed URLs minted on one deploy are validatable by the other (good for cross-deploy testing). If you ever want hard separation, generate distinct tokens.
- **Costs to watch**:
  - Vercel Hobby: 100 GB-hours compute / month, 100 GB bandwidth / month. Personal tier is far below.
  - Upstash KV Hobby: 30 MB storage, 10,000 daily requests. ~6000 users fit in 30 MB; per-request KV ops are 2-3 (HEXISTS + HSET + HGETALL).
  - Daily-request cap is the more likely first-hit. Monitor in Vercel dashboard → Storage → KV usage.

## Eventual cutover to Vercel-primary (when ready)

This is the deferred decision. When operator decides Vercel should be the primary deploy:

1. Verify Vercel pilot has been stable for a soak period (~1 month suggested).
2. Update claude.ai connector URL in user-facing docs (README, USER_GUIDE) to the Vercel URL.
3. Update Google OAuth Console redirect URIs: keep both for ~90 days, then remove Fly.
4. Migrate Fly's SQLite state to Vercel KV (one-off script per the parallel-operation note above).
5. Set Fly to serve HTTP 308 → Vercel URL for ~90 days (one OAuth-token-refresh cycle).
6. After grace window: `fly apps destroy sundeepg98-docs-mcp`. Litestream + R2 dep retire.

**Until step 6**, don't remove Fly code paths — the rollback story relies on Fly being live.

## Rotation: VERCEL_TOKEN

Annual rotation cadence (Vercel tokens default to 1-year expiry):

1. https://vercel.com/account/tokens
2. Generate new token (same scope: appscriptly project only).
3. Save to GitHub: `gh secret set VERCEL_TOKEN -b "$NEW_TOKEN_VALUE"`.
4. Trigger a manual deploy (`gh workflow run deploy-vercel.yml`) to verify the new token works.
5. Revoke the old token in the Vercel dashboard.

## Rotation: Upstash KV credentials

Vercel KV regenerates its REST tokens on operator request:

1. Vercel dashboard → Project → Storage → appscriptly-kv → Settings.
2. "Reset API tokens" (or similar phrasing — Vercel UI evolves).
3. Vercel automatically updates the `KV_REST_API_*` env vars in the project.
4. Trigger a redeploy so the function picks up the new env: `gh workflow run deploy-vercel.yml`.

The window between old-token-revoke and new-deploy-live is ~30 seconds. During it, KV operations 401 — users see "please re-auth" responses transiently. Plan rotations during low-traffic windows.

## Disable the Vercel pilot

If you decide the pilot isn't working out and want to roll back:

1. Vercel dashboard → Project → Settings → Delete Project. Confirms removal of the deploy + the KV instance (free up the Hobby slot).
2. `gh secret delete VERCEL_TOKEN` (revoke the GitHub secret; CI workflow goes back to the "secret unset → skip" branch).
3. Revoke the OAuth redirect URI in Google Cloud Console (remove the Vercel URL; keep Fly).
4. Optionally: revert PR-Δ6 itself. NOT REQUIRED — the codebase change is benign without the Vercel env vars (selector falls back to SqliteBackend).

Fly is unaffected throughout — no Fly secret change, no Fly route change, no Fly downtime.

## Related

- ADR: `docs/adr/2026-05-27-vercel-pilot.md`
- Code: `src/google_docs_mcp/storage/vercel_kv_backend.py`, `src/google_docs_mcp/storage/backend_selector.py`, `api/index.py`, `vercel.json`, `.github/workflows/deploy-vercel.yml`
- Tests: `tests/unit/test_vercel_kv_backend.py`, `tests/unit/test_backend_selector.py`
- Companion runbooks: `docs/runbooks/backup-restore.md`, `docs/runbooks/gcp-project-linking.md`, `docs/runbooks/key-rotation.md`, `docs/runbooks/sentry-setup.md`, `docs/runbooks/pypi-publish-stub.md`
- Vercel docs: https://vercel.com/docs/functions/runtimes/python
- Upstash REST: https://upstash.com/docs/redis/features/restapi
