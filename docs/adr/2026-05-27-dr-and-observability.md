# ADR — DR + Observability (PR-Δ4)

**Status:** accepted, 2026-05-27
**Context:** PR-Δ4 — `feat(observability): litestream DR + Sentry error tracking + request-ID middleware`

## Context

The DevOps audit identified three operational gaps that together
make the Fly deployment hard to reason about under load and
unrecoverable under volume loss:

1. **No volume backup.** Fly volume holds OAuth refresh tokens + per-
   user Apps Script URLs + per-user signing keys. Volume loss =
   every user must re-authorize Google + re-deploy Apps Script.
   Pre-Δ4 the only mitigation was Fly's platform-level disk redundancy,
   which the platform doesn't guarantee for volumes on the free tier.
2. **No error tracking.** Post-deploy 5xx spikes are invisible until
   a user complains. By then Fly's log retention (~24h) may have
   evicted the trace.
3. **No request correlation IDs.** A log line saying "convert
   failed for user-A" is un-correlatable with upstream/downstream
   lines. Multi-tenant debugging is "grep + guess".

All three are $0-to-$8/mo cost (free tiers + R2's free egress
cover personal-scale traffic). Operator's framing 2026-05-27:
"Finish personal-tier first. Build commercial-ready engineering NOW
for free; defer paid commercial activities until later."

## Decision

Ship all three in a single cohesive PR (PR-Δ4):

### 1. Litestream replicates `/data/*.db` to Cloudflare R2

### 2. Sentry SDK initialized in `server.main()`, gated on `SENTRY_DSN`

### 3. `RequestIdMiddleware` + `RequestIdLogFilter` in the HTTP stack

## Alternatives considered

### DR — why litestream over the alternatives

| Option | Verdict | Why |
|---|---|---|
| **Litestream** | ✓ chosen | Near-zero RPO (1s sync interval), S3-compatible (portable across providers), no app-code changes (works at the SQLite WAL layer), MIT-licensed, single static binary (~15 MB in image). |
| Fly volume snapshots | rejected | Fly's snapshot-restore SLA is implicit ("usually within a day"), no point-in-time recovery, vendor-locked. RPO is "whenever Fly last snapshotted" — often hours. |
| Manual cron `sqlite3 .backup` + scp/upload | rejected | Higher RPO (cron-interval-bounded), more code to maintain (cron + retention + restore script), no automatic WAL streaming. |
| GCS Bucket via `gsutil rsync` | rejected | Same shape as the cron approach + GCS pricing model has egress charges that R2 doesn't. |
| App-level write-through to another store | rejected | Order-of-magnitude more complex (every write path needs the duplication), introduces consistency bugs the audit isn't asking us to fix. |

**Why Cloudflare R2 specifically over Backblaze B2 / AWS S3**:
- R2: 10 GB free forever + **zero egress charges**. Restore = $0.
- B2: 10 GB free + $0.01/GB egress beyond a small daily allowance.
  Restoring a 1 GB DB after losing the volume costs ~$0.01 — trivial,
  but R2's zero-egress story is cleaner.
- S3: no useful free tier (12-month trial only); paid pricing
  comparable to R2 for tiny workloads but adds AWS account setup
  friction the operator doesn't currently have.

### Error tracking — why Sentry over alternatives

| Option | Verdict | Why |
|---|---|---|
| **Sentry** | ✓ chosen | Most generous free tier (5k events/mo), best-in-class Python SDK (`sentry-sdk` is the upstream-blessed one), explicit `before_send` hook for scrubbing, LoggingIntegration gives breadcrumb-on-INFO + event-on-ERROR for free. |
| Bugsnag | rejected | Free tier is 7,500 events/mo (slightly better) BUT Python SDK is in maintenance mode (no contributors in 6+ months as of 2026-05); SDK-level scrubbing surface is less mature. |
| Honeybadger | rejected | Smaller free tier (5k events) + Python SDK lags Sentry's; doesn't cover us better. |
| Datadog free tier | rejected | Datadog's free tier is host-bounded (5 hosts) but the error-tracking add-on isn't free; we'd need paid APM to get parity. |
| Self-hosted GlitchTip (Sentry-compatible) | rejected for v1 | Avoids vendor lock-in BUT requires us to operate the GlitchTip server, which defeats the "free + low-op" goal. Worth revisiting if Sentry's free tier ever changes; the SDK code is the same wire protocol. |

### Request-ID — why `uuid.uuid4()` over alternatives

| Option | Verdict | Why |
|---|---|---|
| **uuid4** | ✓ chosen | Stdlib, no deps, 122 bits of entropy (collision-free at any realistic scale), best library support. |
| ULID | rejected | Time-sortable IS a nice property for log scanning, but adds a dep (`python-ulid`) for a benefit we don't actually need (logs are already timestamped, so ULID's sort-order win is redundant). |
| Snowflake (Twitter-style) | rejected | Requires a coordinated worker-id allocation — overkill for single-machine personal deployment. |
| Cloudflare's request-id header | partial accept | We HONOR an inbound `X-Request-ID` if the upstream sets one (claude.ai's proxy or CF in front of us), but we don't depend on the upstream's id format. If absent, we generate uuid4. |

## Design notes

### Litestream supervision via entrypoint script

`scripts/entrypoint.sh` is a 20-line POSIX sh script that
case-distinguishes on `LITESTREAM_BUCKET`:

- Set → `exec litestream replicate -exec "google-docs-mcp"`. The
  `-exec` flag makes litestream the parent process; on SIGTERM
  (Fly deploy rollover) it takes a final WAL checkpoint before
  letting the child exit.
- Unset → `exec google-docs-mcp` (no replication). Lets local dev
  and "operator hasn't enabled DR yet" deploys work normally.

This is the **stub-but-wired** pattern: code is in place +
config is committed + runbook is complete. The operator activates
by setting four Fly secrets — no code change.

### Sentry scrubber as defense-in-depth, not primary

The primary defense against token leakage to Sentry is **opting out
at the SDK level**:
- `include_local_variables=False` — stack frames don't ship local
  vars. This is the surface tokens MOST often leak through
  (Credentials object held in a local).
- `send_default_pii=False` — IP, cookies, default headers all opt-out.

The `_before_send` scrubber catches the surfaces the SDK couldn't:
explicit `request.headers`, query strings, breadcrumb data dicts,
and operator-supplied `extra` / `contexts`. The scrubber matches
~20 substring patterns case-insensitively against dict keys; values
of matching keys become `[REDACTED]`.

Failure tolerance: if scrubbing raises (e.g. malformed event from
a future SDK version), the entire event is **dropped**, not
transmitted. Better to lose a Sentry event than to leak a token.

### RequestIdMiddleware as pure ASGI

Not `BaseHTTPMiddleware`-derived. Reasons:
- Need to wrap the ASGI `send` callable to inject the response
  header on `http.response.start` without buffering the body.
- ASGI scope/receive/send is the lowest-overhead path; the
  middleware adds one ContextVar set + one header append per
  request.
- Lifespan / websocket scopes get forwarded unchanged — the
  ContextVar default `"-"` is fine for any logging during
  lifespan startup/shutdown.

### Middleware ordering

```
RequestIdMiddleware           # outermost (PR-Δ4)
  HealthExemptTrustedHostMiddleware
    BearerTokenMiddleware
      BodySizeLimitMiddleware # innermost
```

`RequestIdMiddleware` is outermost so the `request_id` ContextVar
is populated BEFORE any other middleware runs. Even auth-rejected
(401) or Host-rejected (400) requests still emit a log line with
the request_id stamped — critical for debugging "why is this
request 401-ing".

## Operator activation summary

| Step | Where | Command / action |
|---|---|---|
| 1. Create R2 bucket + API token | Cloudflare dashboard | (~3 min, no credit card) |
| 2. Set 4 R2 Fly secrets | terminal | `fly secrets set LITESTREAM_BUCKET=… LITESTREAM_ENDPOINT=… LITESTREAM_ACCESS_KEY_ID=… LITESTREAM_SECRET_ACCESS_KEY=…` |
| 3. Create free Sentry account + project | sentry.io | (~2 min, no credit card) |
| 4. Set Sentry DSN Fly secret | terminal | `fly secrets set SENTRY_DSN=…` |
| 5. Verify both activated | terminal | `fly logs \| grep -E 'Sentry initialized\|litestream'` |

Total operator time: ~10 min, $0/mo for personal-scale traffic.

## Consequences

### Positive

- **DR established.** Volume loss is now a 5-minute restore, not a
  user-base reauth event. RPO ~1s.
- **Visibility into 5xx.** Operator sees every unhandled exception
  the moment it fires, with stack + log breadcrumbs.
- **Multi-tenant debug is grep-able.** Every log line within an
  HTTP request carries `request_id=<uuid>`; one grep reconstructs
  the full request lifecycle.

### Negative / costs

- **Container image is ~15 MB larger** (litestream binary). Acceptable
  vs the operational win; multi-stage build keeps the bloat in the
  binary only (not Go toolchain).
- **Operator activation required** for items 1 + 2 (stub-but-wired).
  Documented in two new runbooks; the code works as a no-op
  otherwise so the deployment doesn't break on un-activated state.
- **+1 dep** (`sentry-sdk>=2.60.0`) in the runtime image. ~2 MB
  installed; sentry-sdk has a clean dep tree (urllib3 only).

### Deferred

- **License-key middleware** — PR-Δ5 (commercial-ready).
- **GCP project linking** — PR-Δ5.
- **Multi-tenant hardening beyond logging context** — PR-Δ5.
- **Rate limiting on `/api/convert`** — operator hasn't answered the
  usage-pattern question; defer (open ASVS L1 finding from PR-Δ2).

## Verification

- `pytest tests/unit/test_request_id_middleware.py` → 15/15 passed
- `pytest tests/unit/test_observability_sentry.py` → 43/43 passed
- `pytest tests/unit tests/integration` → no regressions
- `ruff check src/ tests/` → clean
- `docker build .` validates the multi-stage litestream copy +
  entrypoint script wiring (covered by the existing e2e workflow's
  build step)
