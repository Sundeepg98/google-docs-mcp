# Coverage Policy

## Current floor

CI enforces minimum **line coverage** via `--cov-fail-under=55` on the
**unit-only** test invocation in `.github/workflows/test.yml`. The flag
deliberately does NOT live in `pytest.ini` default `addopts` (see
[Scope](#scope) below).

- **Floor: 55%** (set 2026-05-20 per R33 baseline measurement).
- **Measured baseline per Python version on CI:**

  | Platform | Coverage |
  |---|---|
  | Linux Py 3.10 | 56.52% |
  | Linux Py 3.11 | **55.21%** ← outlier; sets the floor |
  | Linux Py 3.12 | 56.56% |
  | Linux Py 3.13 | 56.56% |
  | Windows Py 3.13 | 56.74% |

  Py 3.11 measures noticeably lower than every other version — likely a
  version-conditional import branch (some `if sys.version_info` gate). To be
  investigated; either covered with a test or explicitly accepted as
  version-specific dead code (then `# pragma: no cover` it).

- **Floor at 55**: one integer below the worst-case version (55.21%), giving
  ~0.2pp headroom.

If your PR drops total coverage below the floor, CI fails with:

```
FAIL Required test coverage of 55% not reached. Total coverage: 54.xx%
```

To see what changed locally:

```bash
pytest tests/unit --cov=src/google_docs_mcp --cov-report=term-missing
```

## Scope

Coverage **measurement** (`--cov`, `--cov-report=*`) is enabled globally via
`pytest.ini` default `addopts` so every invocation produces a `coverage.xml`
the CI artifact step can pick up.

Coverage **enforcement** (`--cov-fail-under=55`) is added explicitly on the
unit-only CLI invocation in `.github/workflows/test.yml`. It is NOT in default
`addopts` for a reason:

| Test layer | Natural coverage | Should enforce 55%? |
|---|---|---|
| `tests/unit/*` | ~56.5% | yes — that's the calibrated layer |
| `tests/integration/*` | ~34% (high-level flows only) | no — would always fail |
| Ad-hoc subset (`pytest tests/unit/test_foo.py`) | varies wildly | no — investigation tool |
| Combined `pytest tests/unit tests/integration` | dominated by unit | no — opt-in if you want it |

The original v2.0.6 ship put `--cov-fail-under=55` in default `addopts`,
which made the integration job in `e2e.yml` fail on every run because
integration tests measure ~34% of the package by design. v2.0.7 (PR #84)
moved the flag to the unit-only CLI; surfaced via ship-doc's investigation.

If you need to enforce a different floor on a different layer in the future
(e.g. 30% on integration), add `--cov-fail-under=30` to that job's CLI; the
two layers are independent.

## Ratchet policy

- **Bump `+1pp` per release** (or every 2 weeks, whichever comes first)
  until **80%** is reached.
- First scheduled ratchet: floor goes `55 → 56` once the **Py 3.11 outlier**
  is resolved (either covered by a test or `# pragma: no cover`'d), bringing
  the worst-case version to ≥ 56.5%.
- After 80% is hit, switch to branch coverage (`--cov-branch`) and
  re-baseline.

## How to ratchet

1. Run `pytest tests/unit` locally; note the `Total coverage: XX.YY%`
   line.
2. If `XX >= floor + 1`, bump `--cov-fail-under=<N>` in
   `.github/workflows/test.yml` (the unit-test step CLI) by 1.
3. Update both the comment block in `pytest.ini` and this file's
   "Current floor" section.
4. Open a small `chore(ci):` PR.

## Modules below 50% — prioritize tests here

Per 2026-05-20 baseline (`pytest tests/unit --cov`):

| Module | Lines | Coverage | Notes |
|---|---|---|---|
| `docs_api.py` | 503 | **14%** | Largest by LOC, lowest coverage — highest ROI |
| `docx_import.py` | 182 | 19% | .docx parsing path; mock-heavy |
| `cli.py` | 123 | 30% | argparse glue; integration-test it |
| `errors.py` | 13 | 31% | Tiny module; easy win |
| `auth.py` | 55 | 49% | Local OAuth flow; partial mocks possible |
| `server.py` | 467 | 49% | MCP tool handlers; large; chip away tool-by-tool |
| `drive_api.py` | 162 | 51% | After PR2-B migration; soft-failure paths covered |
| `preview.py` | 68 | 56% | Tab-split logic |

`__main__.py` shows 0% (3 lines) — entry-point glue, not test-relevant.

## Bypassing the gate locally

For a quick targeted iteration without coverage overhead:

```bash
pytest tests/unit/test_foo.py --no-cov
```

This skips both measurement and the gate. Never push a change that
disables the gate in CI without a `chore(ci): temporarily lower
coverage floor` PR + RFC.

## CI artifact

The workflow uploads `coverage.xml` as a per-Python-version artifact
(`coverage-3.10`, `-3.11`, `-3.12`, `-3.13`) with a 7-day retention.
Use it to spot platform-conditional coverage drift.
