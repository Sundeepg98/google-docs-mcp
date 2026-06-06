# ADR 2026-05-27 — Vercel pilot (parallel deployment + VercelKvBackend)

**Status**: Accepted
**Date**: 2026-05-27
**PR**: PR-Δ6
**Predecessors**: PR-Δ4 (DR + observability, brings Litestream + R2 dep), PR-Δ5 (commercial-ready), PR-Δ5.5 (rename to appscriptly)

## Context

The current production deploy is on Fly.io: a single-region machine on the `bom` (Mumbai) region, a `/data` SQLite volume for per-user state, Litestream-backed continuous replication to Cloudflare R2 for DR (PR-Δ4). The combined cost is ~$5-7/month at personal-tier traffic, split across Fly (compute) and R2 (storage + egress).

Three signals converged for the Vercel pilot:

1. **Vendor consolidation matters for operator simplicity.** The current stack pulls in Fly + Cloudflare R2 + (recently) Sentry + GitHub. Each is a separate account / billing surface / credential rotation cadence / dashboard. Reducing to fewer vendors per concern is a sustained simplicity win. Vercel KV (Upstash under the hood) replaces R2 + Litestream for the durability story; if Vercel becomes the primary deploy, the R2/Litestream dep retires.

2. **Vercel Hobby tier is verifiably free and unbillable.** Operator's Vercel account (team `sundeepg8-7006`) has no credit card on file. Vercel's Hobby tier hard-rejects any operation that would incur a charge (no overage billing — function calls just start 429ing past the daily limit). For personal-tier traffic, the Hobby limits are far above utilization (100 GB-hours compute / month; 30 MB KV / month).

3. **Apps Script runtime install (PR-α) is the differentiator, not the REST coverage.** The Vercel pilot is about distribution + deploy ergonomics, not about Vercel being a better Workspace API client. The compute platform doesn't change the moat.

**What this PR does NOT decide**: whether Vercel eventually becomes the primary deploy. That's an operator decision pending real production data (cold-start latency at the target traffic, KV size growth, Hobby tier limits in practice). This PR is the parallel-deploy plumbing.

## Decision

**Build the Vercel deploy path in parallel with Fly. Fly stays primary.** Operator can promote Vercel to primary at any time by repointing claude.ai connector URLs + updating Google OAuth Console redirect URIs; the codebase change to swap primaries is zero — both deploys read the same env vars and serve the same `appscriptly` MCP surface.

### Architecture

```
                      ┌──────────────────────────┐
                      │  Same source repo        │
                      │  Same FastMCP app        │
                      │  Same tool registrations │
                      │  Same OAuth flow         │
                      └──────────────────────────┘
                                  │
                ┌─────────────────┴─────────────────┐
                │                                   │
       ┌────────▼────────┐                ┌─────────▼─────────┐
       │ Fly (primary)   │                │ Vercel (pilot)    │
       │                 │                │                   │
       │ Dockerfile      │                │ api/index.py      │
       │ uvicorn         │                │ Vercel Python rt  │
       │ SqliteBackend   │                │ VercelKvBackend   │
       │   ↳ /data vol   │                │   ↳ Upstash KV    │
       │ Litestream → R2 │                │ (KV is durable;   │
       │ (DR backup)     │                │  no Litestream)   │
       └─────────────────┘                └───────────────────┘
        sundeepg98-docs-mcp.fly.dev       appscriptly-…vercel.app
```

### Key components added in this PR

**`src/appscriptly/storage/vercel_kv_backend.py` — VercelKvBackend.** Third implementation of the `StorageBackend` Protocol (after `SqliteBackend` and `InMemoryBackend`). Talks to Upstash via the HTTP REST API (`KV_REST_API_URL` + `KV_REST_API_TOKEN` env vars, which Vercel populates automatically when KV is bound to the project). Uses httpx (already transitive via FastMCP) — no new runtime dep.

Storage layout: each `user_id` → one Redis HSET at `user_state:<user_id>`. Fields are JSON-encoded so ints/strs/bools round-trip. Merge semantics bit-for-bit identical to SqliteBackend (HSET only touches the fields in the update; `created_at` preserved across saves; `updated_at` bumped; first save stamps `user_id` + `created_at`).

**`src/appscriptly/storage/backend_selector.py` — env-var-driven factory.** Reads `STORAGE_BACKEND`:
- unset / `sqlite` → SqliteBackend (default; preserves every existing test + every existing Fly deploy)
- `vercel_kv` → VercelKvBackend if `KV_REST_API_URL` + `KV_REST_API_TOKEN` are set; else SqliteBackend + WARNING log (fail-soft).
- unknown value → SqliteBackend + WARNING (typo protection).

Fail-soft rationale: the selector NEVER raises. A misconfigured operator deploy must not 500 on every request; the WARNING log surfaces the problem for fix-forward.

**`src/appscriptly/user_store.py::init_default_backend_from_env()` — operator entrypoint helper.** Called by `api/index.py` (Vercel) at module load. Resolves the env var + swaps the module-level `_backend`. Tests do NOT call this — they rely on `with_backend(InMemoryBackend())` for explicit per-test control, so the function exists for operator-entrypoint use only.

**`api/index.py` — Vercel Python ASGI entrypoint.** Imports the FastMCP app, calls `init_default_backend_from_env()` + `init_sentry()` + `configure_auth_for_http()` in order, builds the Starlette app, exports as module-level `app`. Vercel's Python runtime auto-detects the `app` symbol.

**`vercel.json` — runtime config.** Python 3.12, 1024 MB memory, 60s max duration (Hobby ceiling), `iad1` region (universally available; `bom1` deferred until availability is verified). Heavy on inline `_comment_*` keys documenting every choice + the operator env-var checklist.

**`.github/workflows/deploy-vercel.yml` — parallel CI.** Triggers on push to main (production deploy) AND on pull requests (preview deploy). Uses the official Vercel CLI (npm-installed, version-pinned to `^41`). Smoke-checks `/health` post-deploy. Gracefully skips with a warning if `VERCEL_TOKEN` secret isn't set — the pilot is opt-in; missing token doesn't fail CI.

### Why VercelKvBackend, not SqliteBackend, on Vercel

Vercel serverless containers have ephemeral tmpfs — SqliteBackend's file dies with each cold start. Writes would silently disappear. The selector defaults to SqliteBackend even on `STORAGE_BACKEND=vercel_kv` when KV env vars are missing (fail-soft); on Vercel this produces "user re-auth needed on every request" symptoms that the operator immediately notices, vs the alternative of 500ing the whole deploy.

### Why HTTP REST, not redis-py

Vercel KV exposes the Upstash REST endpoint (`KV_REST_API_URL`), NOT a `redis://` TCP URL. Vercel's serverless Python runtime restricts arbitrary TCP egress. The REST protocol is dead simple: `POST <URL>` with body `["COMMAND", "arg1", ...]`, auth via `Authorization: Bearer <TOKEN>`. ~80 LOC of httpx wraps the whole surface. Using redis-py would require a TCP-tunneling proxy + a new dep + no actual benefit.

### Statelessness implications

On Vercel cold start, all module-level mutable state resets:
- `_creds_cache` (stdio-mode cached operator token) — irrelevant on Vercel (HTTP mode only).
- `keys.py` key-call counters — observability data; acceptable loss.
- `user_store._initialized_paths` — irrelevant when SqliteBackend isn't used.
- `_backend` itself — re-resolved by `init_default_backend_from_env()` on each cold start.

The user-state durability story is entirely VercelKvBackend's responsibility. Everything else is opportunistic + best-effort.

## Consequences

### What gets better

- **Vendor consolidation pathway opens.** If Vercel pilot succeeds, Cloudflare R2 + Litestream retire entirely (Vercel KV is natively durable; no point in a 2nd backup layer). One vendor for compute + storage instead of two.
- **Cold-start parity test.** Vercel's serverless model surfaces latency penalties that Fly's always-on machine hides. If a tool path is slow on Vercel due to cold-start, the operator sees the cost in production logs and decides whether to optimize or stay on Fly.
- **Preview deploys per PR** (free on Hobby). Every PR gets a per-branch URL; manual testing against a real-deploy URL becomes one-click rather than requiring local `fly deploy` to a staging app.
- **Zero net dependency cost.** The VercelKvBackend uses httpx (already transitive). No new runtime deps. The `[vercel]` extras group is empty — `pip install appscriptly[vercel]` is a future-proofing seam.

### What gets worse (the honest debt)

- **Two CI workflows.** Operator now maintains both `deploy.yml` (Fly) and `deploy-vercel.yml` (Vercel). Each has its own secret rotation (`FLY_API_TOKEN` vs `VERCEL_TOKEN`). For the pilot phase, this is the cost of parallel deploy.
- **Two backends to keep in semantic parity.** VercelKvBackend MUST match SqliteBackend's contract bit-for-bit (merge semantics, NULL handling, created_at/updated_at bookkeeping). A future change to one must be reflected in the other or tests catch the drift — see the parity tests in `tests/unit/test_vercel_kv_backend.py`.
- **No cross-backend user migration.** A user authorized on Fly is NOT authorized on Vercel; the KV starts empty. During the pilot, users hitting both URLs would re-auth twice. The operator's eventual cutover means users re-auth once on the new URL, accept the friction.
- **60s max execution time on Vercel Hobby.** Fly has no hard timeout (uvicorn worker can take as long as needed). Long-tail tool calls (docx-import for large documents) approach 30-45s on Fly and might breach 60s on Vercel cold-start. Acceptable for the pilot; commercial-tier upgrade unlocks 300s.
- **Vercel KV Hobby tier is 30 MB.** Per-user state is small (~5 KB OAuth token + Apps Script metadata); ~6000 users fit. For personal-tier this is fine; commercial activation that drives growth beyond this needs a Vercel Pro KV upgrade or a different store (Postgres / FaunaDB).

### Rollback story

Vercel is purely additive. To roll back: delete the Vercel project (Vercel dashboard → Project Settings → Delete), revoke `VERCEL_TOKEN` from GitHub secrets, optionally revert this PR. Fly is unaffected throughout — no Fly secrets change, no Fly app config change, no Fly route change.

The codebase change is also benign: in the absence of the Vercel env vars + `STORAGE_BACKEND=vercel_kv`, the selector defaults to SqliteBackend (the pre-PR-Δ6 behavior). The Vercel-specific code is unreachable in a Fly-only deploy.

### Operator-action-pending checklist (post-merge)

```
[ ] Generate VERCEL_TOKEN: https://vercel.com/account/tokens
    Scope: appscriptly project only (least privilege).
[ ] gh secret set VERCEL_TOKEN < <(echo "$TOKEN")
[ ] Vercel dashboard → New Project → Import Git Repository → appscriptly
[ ] Vercel dashboard → Project → Storage → Create New → KV → Hobby (free 30 MB)
    Vercel auto-populates KV_REST_API_URL + KV_REST_API_TOKEN.
[ ] Vercel dashboard → Project → Settings → Environment Variables → add:
    - MCP_BEARER_TOKEN (same value as Fly)
    - GOOGLE_CLIENT_CONFIG (same value as Fly)
    - GOOGLE_OAUTH_BASE_URL (Vercel deploy URL or custom domain)
    - STORAGE_BACKEND=vercel_kv
    - (optional) SENTRY_DSN, GCP_PROJECT_NUMBER, etc.
[ ] Update Google OAuth Console: add Vercel URL to authorized redirect URIs
    (keep the Fly URL too — both must resolve during pilot)
[ ] First deploy: merge any PR or run `gh workflow run deploy-vercel.yml`
    Workflow smoke-checks /health automatically.
[ ] Manual verification: curl https://<vercel-deploy-url>/health
    Should return {"ok": true, "service": "appscriptly"} (or "google-docs-mcp"
    pre-PR-#135 if the rename hasn't merged yet).
[ ] (Optional) test the OAuth flow end-to-end via the preview URL.
```

None of these block PR-Δ7+ feature work.

## Verification

- `pytest tests/` — 1005 passed, 5 skipped (live-only). +33 vs the 972 PR-Δ5 baseline.
- `ruff check src/ tests/` — clean.
- `pyright src/` — 0 errors, 0 warnings.
- New tests:
  - `tests/unit/test_vercel_kv_backend.py` (24 tests): Protocol conformance, construction-time env-var validation, behavioral parity with SqliteBackend (merge semantics + NULL handling + idempotent clear + cross-user isolation), Upstash REST wire-shape correctness (HSET / HGETALL / HEXISTS / DEL commands, Bearer auth header, JSON-encoded values, key prefix), error-surface handling (429, 200-with-error-body, network failure).
  - `tests/unit/test_backend_selector.py` (9 tests): env-var matrix (unset / sqlite / vercel_kv / unknown), case-insensitivity + whitespace stripping, fail-soft for missing KV env vars, `init_default_backend_from_env` round-trip.
- Backward-compat verified:
  - Existing 972 tests still pass — no regressions from the new package, new module-level function, or new optional dep.
  - Fly deploy workflow unchanged.
  - Module-level `user_store._backend = SqliteBackend()` default unchanged → tests + Fly deploy see identical behavior.

## References

- Vercel Python runtime docs: https://vercel.com/docs/functions/runtimes/python
- Upstash Redis REST API: https://upstash.com/docs/redis/features/restapi
- Vercel KV (Upstash-backed): https://vercel.com/docs/storage/vercel-kv
- Vercel Hobby tier limits: https://vercel.com/docs/limits/overview
- StorageBackend Protocol: `src/appscriptly/user_store.py::StorageBackend`
- PR-Δ4 ADR (DR / observability — defines the Litestream + R2 + Sentry + RequestId surface this pilot's KV alternative obsoletes if/when Vercel becomes primary)
- Operator runbook: `docs/runbooks/vercel-activation.md`
