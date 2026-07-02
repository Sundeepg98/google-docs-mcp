# Runbook: deploy, rollback, and the DEPLOY_ENABLED toggle

<!-- secret-scan-allow: public-container-image-digest -->

Deploy-standard hardening (2026-07-02). Source audit:
`_audit/2026-06-29-internal-engineering-audit.md` (facet 4, deploy-standard).

App: `sundeepg98-docs-mcp` on Fly.io. Canonical hostname:
`https://mcp.appscriptly.com` (the fly.dev hostname
`https://sundeepg98-docs-mcp.fly.dev` serves the same app).

Everything here works today; before this runbook it was undocumented.

---

## 1. The normal deploy path (and the only sanctioned manual one)

Push to `main` runs `.github/workflows/deploy.yml`: unit gate, mutation
gate, image build pushed to
`registry.fly.io/sundeepg98-docs-mcp:deployment-<sha7>`, then
`flyctl deploy --image ...`, then a `/health` smoke check.

Manual deploy (the ONLY sanctioned manual path; it re-runs the same
gates and preserves commit provenance):

```bash
gh workflow run deploy.yml -R Sundeepg98/google-docs-mcp
gh run watch -R Sundeepg98/google-docs-mcp \
    $(gh run list -R Sundeepg98/google-docs-mcp --workflow=deploy.yml -L 1 \
        --json databaseId -q '.[0].databaseId')
```

Notes:

- `workflow_dispatch` ALWAYS deploys; it ignores `DEPLOY_ENABLED`.
- Do NOT deploy code with a local `flyctl deploy`. That bypasses the
  unit + mutation gates and produces a ULID-tagged image with no commit
  mapping (this exact failure mode left prod on an unmapped image for
  weeks). Local flyctl is for rollback and ops only (sections 3 and 4).

## 2. Verify EVERY deploy (non-negotiable close-out)

```bash
curl -s https://mcp.appscriptly.com/health
curl -s https://sundeepg98-docs-mcp.fly.dev/health
# expect on both: {"ok":true,"service":"appscriptly","git_commit":"<sha7>"}
# git_commit must equal the short SHA you just deployed
# (images older than 2026-07-02 predate the stamp and omit the field)

curl -s https://mcp.appscriptly.com/.well-known/oauth-protected-resource \
    | jq '.scopes_supported | length'
# expect: len(IDENTITY_SCOPES) + len(WORKSPACE_SCOPES) from the deployed
# commit's source (src/appscriptly/oauth_google.py + src/appscriptly/auth.py)

flyctl status -a sundeepg98-docs-mcp
# expect: image tag deployment-<sha7> of the commit you deployed,
# checks passing
```

Or just dispatch the monitor, which runs the same checks against main:

```bash
gh workflow run prod-drift.yml -R Sundeepg98/google-docs-mcp
```

A healthy boot implies the tool-count floor passed (the server crashes
below `_MIN_EXPECTED_TOOL_COUNT` rather than serving a partial surface).

## 3. Rollback

Every CI deploy leaves an immutable, commit-addressed image tag in
Fly's registry: `deployment-<sha7>`. Rollback = deploy a previous tag.

Step 1: find the target.

```bash
flyctl releases -a sundeepg98-docs-mcp        # release history, newest first
flyctl status -a sundeepg98-docs-mcp          # what is live right now
git log --oneline -10                          # map sha7 tags to commits
```

Step 2: deploy the known-good image (no rebuild, takes ~1 min):

```bash
flyctl deploy -a sundeepg98-docs-mcp \
    --image registry.fly.io/sundeepg98-docs-mcp:deployment-<known-good-sha7>
```

Step 3: run the section 2 verification block. `git_commit` in `/health`
must now read the rolled-back SHA.

Why rollback is safe here:

- SQLite schema changes are additive, idempotent `ALTER TABLE ... ADD
  COLUMN` (`src/appscriptly/user_store.py`), so an image rollback needs
  NO data rollback.
- The `/data` volume and `FASTMCP_HOME` OAuth state survive deploys, so
  a rollback forces no re-consent.
- Disaster restore (volume loss) is a separate path: litestream/R2 per
  `docs/runbooks/backup-restore.md`.

Caveats:

- Rolling back across a SCOPE change flips the public
  `scopes_supported` surface with it. Do not run rollback drills while
  a Google OAuth verification review is in flight.
- Pre-2026-07 anchor: before CI deploys resumed, the live image was a
  manual ULID-tagged build with no commit mapping. Its digest (a public
  container-image content hash, not a secret), kept as the last-resort
  anchor from that era:
  `registry.fly.io/sundeepg98-docs-mcp@sha256:3f985a1526800bb67bca4a176cea0e07b89625d586e46ecad8ec436cc1ed5761`
  (approximately commit d766597, 13 connector scopes). Every deploy
  after CI resumed is commit-addressed; prefer `deployment-<sha7>` tags.

## 4. The DEPLOY_ENABLED toggle

State lives in a repo Actions variable (invisible in the tree; check it
when in doubt):

```bash
gh variable list -R Sundeepg98/google-docs-mcp
```

Pause auto-deploy (merge no longer deploys; dispatch still does):

```bash
gh variable set DEPLOY_ENABLED --body false -R Sundeepg98/google-docs-mcp
```

Resume (either form):

```bash
gh variable set DEPLOY_ENABLED --body true -R Sundeepg98/google-docs-mcp
gh variable delete DEPLOY_ENABLED -R Sundeepg98/google-docs-mcp
```

Resume ritual: resuming is IMMEDIATELY followed by one manual dispatch
(section 1) plus the section 2 verification block, never left for "the
next push". While paused, every merged PR is merged-but-not-live.

### Toggle policy (adopted 2026-07-02)

- The toggle is an emergency brake measured in HOURS, not weeks. The
  standing invariant is main == prod.
- For a genuine freeze window (for example an OAuth review), prefer NOT
  merging to main (queue the PRs) over merging-without-deploying. The
  whole doc/memory/tooling stack assumes merge = deploy; a 19-day
  silent freeze once hid an undeployed security fix.
- No out-of-band `flyctl deploy` for code, ever. `workflow_dispatch` is
  the only sanctioned manual deploy path (gates + provenance); local
  flyctl is for rollback (section 3) and ops only.
- While the toggle is off, expect loudness by design: each push run
  prints "DEPLOY SKIPPED - prod NOT updated" (deploy.yml
  `deploy-status` job) and the scheduled `prod-drift.yml` monitor fails
  and files/updates a `prod-drift` issue once live scope surface and
  main diverge.

## 5. Monitoring surfaces

- `.github/workflows/prod-drift.yml`: 6h cron + dispatch. Fails loudly
  when the live `scopes_supported` set/count diverges from main's
  source-derived set, or when `/health` is not 200 on either hostname;
  warns when the live `git_commit` trails main. Files/updates a
  `prod-drift`-labeled issue on failure; auto-closes it on recovery.
- deploy.yml `deploy-status` job: warning annotation + step summary on
  every withheld or failed deploy.
- Known gap (operator item): an external uptime pinger on
  `https://mcp.appscriptly.com/health` (catches a Fly billing
  suspension even when GitHub itself is the thing that is down or the
  cron is delayed). GitHub cron is best-effort.

## 6. Failure signatures seen before (fast triage)

- `Error: failed to create release (status 403): Your account has
  overdue invoices ...` at the "deploy pre-built image to Fly" step:
  Fly billing wall. Clear at https://fly.io/dashboard/sundeep-g/billing
  then re-dispatch. Nothing in the repo fixes this.
- Green push runs but prod stale: `DEPLOY_ENABLED` was left `false`.
  Section 4.
- Live image tag is a ULID (not `deployment-<sha7>`): someone deployed
  out-of-band with local flyctl. Re-dispatch deploy.yml to restore
  commit provenance; re-read the toggle policy.
