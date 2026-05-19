# Chaos Plan (v1.4.0b)

Operator-facing description of the resilience scenarios the chaos
harness exercises. Each scenario isolates ONE failure mode the cloud
MCP can hit in production, drives it on purpose, and asserts the
contract holds.

Run with:

```
uv run python tests/chaos/run_chaos.py --scenarios all --max-duration 60s --json-output chaos-report.json
```

Or a single scenario:

```
uv run python tests/chaos/run_chaos.py --scenarios S1 --max-duration 30s
```

The harness is intentionally NOT a pytest module — it's a
long-running, JSON-emitting CLI so the same script runs locally,
in CI nightly, and on Fly post-deploy smoke. Pytest would invert
that ergonomics.

## Scenarios

### S1 — Concurrent user_store saturation

**Hypothesis.** When N tool calls hit the SQLite-backed `user_store`
concurrently (separate users, each doing read-modify-write), no row
gets corrupted, no write is silently dropped, and the
`_initialized_paths` cache + WAL mode keeps latency bounded.

**Drive.** Spawn `--workers` threads (default 16); each picks a
distinct `user_id` and loops `save_state -> get_state -> save_state`
for `--max-duration`. Records per-op latency + any exception. At
end-of-run, verifies every worker's last write is readable, intact,
and reflects exactly the final value the worker pushed (last-write-
wins, no torn write, no value from a different worker).

**Pass.** Zero exceptions. Final-state verification passes for all
workers. p99 latency < 500ms (relaxed because Windows SQLite is
slower than Linux; real Fly numbers are much tighter).

**Why it matters.** Phase-6 introduced the per-path init cache to
fix a PRAGMA-journal-mode-WAL race; this scenario is the regression
guard for that and for any future locking change. Run it after
ANY edit to `user_store._connect` or `_ensure_initialized`.

### S2 — Validator-rejection storm (placeholder — v1.4.0c hooks)

**Hypothesis.** When `_FIELD_VALIDATORS` rejects a flood of bad
writes (e.g. an attacker enumerating malformed `apps_script_url`
values via a compromised setup tool), the rejection path is
constant-time vs the success path and does not leave partial state
in the DB. **NOT WIRED in v1.4.0b** — depends on v1.4.0a's
`_FIELD_VALIDATORS` landing first. Stubbed for v1.4.0c follow-up.

### S3 — Refresh-token rotation under load (placeholder)

**Hypothesis.** N concurrent tool calls for the SAME user trigger
exactly ONE token refresh (per-user lock in `credentials.py`), not
N parallel refreshes (which would race Google's rotation and
invalidate the refresh_token). Not in v1.4.0b — needs an upstream
mock that can simulate refresh latency without hitting Google.

## Output format

JSON written to `--json-output` path (one report per run):

```json
{
  "version": "1.4.0b",
  "started_at": "2026-05-19T10:00:00Z",
  "duration_seconds": 30.42,
  "max_duration_requested": 60.0,
  "scenarios": {
    "S1": {
      "status": "pass" | "fail",
      "ops_total": 12345,
      "ops_failed": 0,
      "latency_ms_p50": 0.8,
      "latency_ms_p95": 4.2,
      "latency_ms_p99": 9.1,
      "errors": []
    }
  }
}
```

`status: fail` exits the CLI with code 1 (CI-friendly).

## Operator notes

- Each scenario is idempotent and self-cleaning: it points
  `GOOGLE_DOCS_USER_STORE_PATH` at a tmp dir and removes the dir on
  exit. Safe to run on a developer laptop or in CI.
- The harness does NOT hit Google APIs. Don't add network-touching
  scenarios here — those belong in e2e tests under
  `tests/integration/`.
- `--max-duration` is a SOFT cap; ongoing work finishes its current
  iteration. Expect 1-2s overrun.
