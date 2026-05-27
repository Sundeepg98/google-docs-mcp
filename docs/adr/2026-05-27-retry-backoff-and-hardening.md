# ADR 2026-05-27 — RetryingGoogleApiClientAdapter + container hardening + audit logging

<!-- secret-scan-allow: docker-manifest-digest -->

**Status**: Accepted
**Date**: 2026-05-27
**PR**: PR-Δ3
**Builds on**: PR-Δ1 (#125, spec compliance), PR-Δ2 (#126, security posture artifacts)

## Context

Two audits ran during this session over the codebase as of `dfc388a` (origin/main after PR-Δ2 merged):

1. **Hex specialist** evaluating the Port + Adapters layering put in by the M1a/M2 refactors. Verdict: the seams are sound; the OPEN architectural finding is **no retry/backoff code anywhere**. `git grep` for `tenacity|backoff|@retry|num_retries` returns nothing. Google's `googleapiclient`-surfaced 429 and 5xx responses are routine (rate-limit hits during ingest bursts; transient 502s during regional rolls), and every one of them today bubbles directly to the user as a hard failure.

2. **DevOps specialist** evaluating the production runtime. Five must-fix gaps. After triage, four belong in this PR; the fifth (Litestream SQLite replication) is large enough to ship as its own PR-Δ4:
   - Container runs as root inside the container — needless attack surface.
   - Base image `python:3.13-slim` is unpinned — supply-chain risk.
   - No CODEOWNERS file — PR auto-routing surface missing.
   - In-process rate limiting (V11.1.7 from PR-Δ2's ASVS checklist) — still OPEN; tracked as the carry-over.

Two operational asks also landed this session that fit this batch without scope-creeping it:
- A key-rotation runbook for the HKDF master + OAuth client secret + Fly token + per-purpose overrides. `RUNBOOK.md` §3.4 has fragments; nothing operationally complete.
- A structured audit log line per `/api/convert` upload session, so future forensic queries against the Fly logs can answer "who uploaded what, when" without dumping content.

## Decision

Ship one PR with seven thematically-related items:

### 1. `RetryingGoogleApiClientAdapter` (headline)

Composing adapter wired into the `GoogleAPIClient` Port. Wraps an inner adapter (production: `GoogleApiClientAdapter`; tests: `InMemoryGoogleAPIClient`) and adds:

```python
def execute_with_retry(
    self,
    fn: Callable[[], _T],
    *,
    idempotent: bool,
    op_name: str = "google_api_call",
) -> _T:
```

- Retries on `HttpError` status ∈ `{429, 500, 502, 503, 504}` only. Everything else (4xx caller bugs, network errors, programmer bugs) propagates immediately.
- Idempotence is per-call-site. `idempotent=False` invokes `fn` exactly once and lets any exception propagate — this is the safety floor against duplicate side-effects from re-executing partial mutations.
- Backoff: tenacity's `wait_exponential_jitter` with `initial=1s`, `max=8s`. Custom `_RetryAfterAwareWait` strategy intercepts the retry state and honors any HTTP `Retry-After` header as the floor for the next attempt.
- `reraise=True` so callers see the underlying `HttpError`, not tenacity's `RetryError` — preserves the same exception surface as the no-retry world.
- Max attempts default 3 (first attempt + 2 retries).

### 1a. Why a separate `execute_with_retry` method instead of wrapping every `HttpRequest`

Considered: build a Resource proxy that intercepts every `.files()`/`.documents()`/`.execute()` returned from the inner Resource and wraps each `HttpRequest.execute()` in retry. Rejected because:

- The googleapiclient Resource API is huge (hundreds of dispatched methods, dynamic surface) and the proxy would couple us to internals the SDK reserves the right to rearrange.
- The proxy CANNOT see the calling tool's annotation, so idempotence would have to default to either "always retry" (unsafe — duplicate doc creation) or "never retry" (useless).
- Spec budget was 40 LOC; the proxy approach is 200+.

Explicit `execute_with_retry(fn, idempotent=...)` at each `.execute()` call site is shorter, honest about scope, portable to a future `aiogoogle` async swap, and — critically — surfaces idempotence as a per-callsite decision where the `@workspace_tool` annotation can drive it.

### 1b. Production wiring

`_active_client` (the module-level default in `google_api_client.py`) is now:

```python
_active_client = RetryingGoogleApiClientAdapter(GoogleApiClientAdapter())
```

instead of the bare `GoogleApiClientAdapter()`. The composing shape is the same `GoogleAPIClient` Protocol, so the 14 existing `get_service` call sites are unaffected — they still get a `Resource`, still construct it via the production builder. The retry behavior is opt-in per-callsite via the new `execute_with_retry` facade; no tool is using it yet, but the seam is in place.

Tests that need a non-retrying client (the existing test suite — many tests would slow down significantly if all transient stubs implicitly retried) explicitly swap in a bare `InMemoryGoogleAPIClient` via `with_google_api_client`. The facade-level `execute_with_retry` gracefully degrades to a single invocation when the active client lacks `execute_with_retry` — opt-out is honest.

### 2. Non-root container user (uid 10001)

`Dockerfile` adds a final `useradd --uid 10001 --user-group --no-create-home --shell /sbin/nologin app` + `chown -R app:app /app /data` + `USER app`. uid 10001 is high enough to dodge Debian's reserved 0-999 range and matches the Distroless/Chainguard convention.

`/data` is chown'd because Fly Volumes preserve uid ownership across deploys — after the first deploy, the volume is owned by uid 10001, and the chown step is a no-op (idempotent).

Rolling back is mechanical: delete the `useradd` + `USER app` lines, redeploy. No data migration.

### 3. SHA-pin the base image

`FROM python:3.13-slim` → `FROM python:3.13-slim@sha256:<docker-hub-manifest-digest>` (the manifest digest of the tag as of 2026-05-22; the literal value is in the Dockerfile and managed by dependabot from this point forward). Dependabot's `docker` ecosystem is added to `.github/dependabot.yml` to bump the digest weekly — without this, a SHA-pin is supply-chain-safe but rots (stale Debian base + unpatched libc/openssl CVEs).

### 4. CODEOWNERS

`.github/CODEOWNERS` with a catch-all `* @Sundeepg98`. Enables GitHub auto-review-request routing. As contributors join, per-area rules can be added above the catch-all.

### 5. Key rotation runbook

`docs/runbooks/key-rotation.md`. ~280 lines covering:
- `MCP_BEARER_TOKEN` rotation: both graceful (per-purpose-override pin → swap master → unset overrides on TTL cadence) and emergency (immediate swap, accept in-flight token invalidation).
- `GOOGLE_CLIENT_CONFIG` (OAuth client secret) rotation against Google Cloud Console's add-then-delete model.
- `FLY_API_TOKEN` rotation including the `flyctl tokens create deploy -j | jq -r .token | gh secret set` pipe so the token never appears in shell history.
- Per-purpose overrides explained as a rotation tool, NOT a steady-state config.
- Per-user OAuth tokens explained as out-of-scope (refresh on Google's clock; manual rotation is `delete-row → next-call-triggers-NeedsReauthError`).

### 6. Structured audit log per upload session

`src/google_docs_mcp/http_server/routes/convert.py` now emits one log line per upload via a new dedicated logger `google_docs_mcp.audit.upload`:

```
upload_session session_id=<uuid4> user_id=sub:<8char>… file_size_bytes=<n>
  file_sha256=<hex> split_by=<...> ts=<unix>
```

- `user_id`: signed-URL `uid` (multi-tenant) or `anonymous_sandbox` (operator/bearer-header). Sub is truncated to first 8 chars to limit correlation surface in long-retained logs.
- `file_sha256`: hash, not content. Forensic primitive for "was this exact byte sequence uploaded twice?" without storing the bytes.
- `session_id`: per-request UUID. Followup PR-Δ4 will propagate it through `docx_import` → Apps Script → response; in this PR it's scoped to the route.
- Distinct logger namespace so operators can route audit lines to a separate sink (longer retention, SIEM) without dragging in every middleware log line.

### 7. ADR + CHANGELOG

This document. `CHANGELOG.md` `[Unreleased]` extended with a PR-Δ3 block summarizing all seven items.

## Out of scope (explicitly deferred)

- **Litestream / volume backup**: ship in PR-Δ4 (DR + observability).
- **Sentry integration**: PR-Δ4.
- **Request-ID middleware that propagates `session_id`**: PR-Δ4.
- **License-key middleware**: PR-Δ5 (commercial-ready).
- **Multi-tenant hardening beyond what's already in place**: PR-Δ5.
- **Rate limiting on `/api/convert`**: deferred to PR-Δ4 or later — operator still hasn't supplied the usage-pattern data we need to set sensible per-user quotas.
- **Vercel work**: PR-Δ6.
- **New `gdocs_*` tools**: separate PRs.

## Verification

- `pytest tests/unit/test_retrying_google_api_client.py` — 22 new tests, all pass. Covers protocol conformance, pure delegation of `get_service`, parameterized 429+5xx retry-then-succeed, non-idempotent-doesn't-retry, max-retries-exhausted-reraises-HttpError-not-RetryError, 4xx-non-429-doesn't-retry, non-HttpError-doesn't-retry, Retry-After-honored, fallback-to-jittered-backoff, facade-routing, facade-degradation.
- `pytest tests/unit` — full suite green (832 + 22 = 854 expected; verified pre-merge).
- `pytest tests/unit/test_api_convert_multitenancy.py` — existing convert tests unchanged-passing despite the new audit log line.
- Local Docker build NOT exercised (no daemon in this env); the Dockerfile changes are mechanical — first cold-cache CI build will verify the non-root + SHA-pinned digest end-to-end.

## Consequences

**Positive.**

- Production gains a retry surface that's **opt-in per-callsite**. We can roll retry into tools incrementally (readonly `gdocs_read_doc` first; mutating tools never) without a flag day.
- Container surface is meaningfully smaller (no root). A future hypothetical container-escape primitive is much less useful from uid 10001.
- Base image is content-addressable; we know exactly which Debian layer set we're running.
- Audit trail exists for upload sessions — answers a question we couldn't answer before ("did THIS file get uploaded twice?") without storing content.
- Operational knowledge (key rotation) is documented in one place; a future-operator doesn't have to reconstruct the per-purpose-override trick from `keys.py` source.

**Negative.**

- The retry adapter is **wired but not yet adopted**. None of the existing `.execute()` calls actually call `execute_with_retry` yet — that's a follow-up sweep. The headline value (fewer user-visible transient errors) materializes only after call sites adopt.
- Non-root container changes the failure mode of any code that assumed root (e.g. writing outside `/data`). Audit grepped for such paths and found none, but a runtime regression on first deploy is non-zero risk; mitigation is the existing smoke check on `/health`.
- Audit log line emits per upload — at low-mid traffic this is fine; if cloud-chat usage spikes, log volume scales linearly. Followup PR-Δ4 should add log-shipping rate limits if needed.
- New dependency: `tenacity`. Well-maintained, single-purpose, MIT-licensed; no transitive concerns. Pinned `>=9.1.4` to match the current lockfile.

## Roll-forward path

If the retry adapter misbehaves in production (e.g. introduces an unexpected delay on cold paths), the rollback is surgical: change `_active_client = RetryingGoogleApiClientAdapter(GoogleApiClientAdapter())` back to `_active_client = GoogleApiClientAdapter()` and ship. Tests need a one-line revert. No data migration; no config change; no user disruption.

If the non-root container fails on Fly (volume permission edge case the local audit missed), the rollback is to delete the `useradd` + `USER app` block in the Dockerfile and redeploy. The volume's uid 10001 ownership from this PR persists, but `root` inside the container can read/write uid-10001-owned files normally.

If the SHA-pin pins an unintentionally-broken digest, dependabot's next weekly run produces a bump PR; manual override is editing the digest in one line.
