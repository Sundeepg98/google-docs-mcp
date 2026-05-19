# Contributing to google-docs-mcp

## Local dev setup

Clone, create venv, install with test extras:

```bash
git clone https://github.com/Sundeepg98/google-docs-mcp.git
cd google-docs-mcp
python -m venv .venv && source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -e ".[test]"
```

Verify install:

```bash
python -m pytest tests/unit -q
```

Should report ~240 tests passing in ~12s. If it doesn't, fix that before changing anything else.

## Running tests

```bash
python -m pytest tests/unit -q                       # fast feedback loop
python -m pytest tests/unit -v -k "your_test_name"   # iterate on one test
python scripts/mutation_check.py                     # mutation gate (~90s)
```

Live integration tests (in `tests/integration/`) require real Google creds and are not run in CI by default:

```bash
python -m pytest tests/integration --live           # opt-in via marker
```

## Adding a new tool

1. **Add the tool to `src/google_docs_mcp/server.py`** with `@mcp.tool()` decorator. Docstring uses the project's house style: `USE WHEN:` clause, `Args:` block, `Returns:` block, `Choreography:` clause.

2. **Add a unit test in `tests/unit/test_<name>.py`** — mock the Google API calls; assert the return shape.

3. **Update `_SERVER_INSTRUCTIONS` and `gdocs_guide()`** in `server.py` to mention the new tool in the relevant workflow / tool_group bucket.

4. **Add a mutation guard in `scripts/mutation_check.py`** — define a `Mutation` entry that injects a small bug and names the test that should catch it.

5. **Update `docs/TOOL_CONTRACT.md`** with the new tool's entry in §3.

6. **If security-sensitive** (touches creds, uploads, outbound URLs): add a row to `docs/THREAT_MODEL.md` §4.

7. **If operational impact** (new state, new deploy step, new failure mode): add an entry to `docs/RUNBOOK.md` outage classes.

8. **Update CHANGELOG.md** with the new tool under `### Added`.

## Code style

- Type-annotated. No `Any` in public signatures unless unavoidable.
- Soft-failure dicts include `reason: str` + `message: str` plus the success-case context fields.
- Hard-fatal errors `raise ToolError(...) from e` to preserve traceback.

## PR checklist

- [ ] Test that fails without the change and passes with it
- [ ] Input validation on every public arg
- [ ] Errors raise `ToolError` for hard-fatal, return soft-failure dict for recoverable
- [ ] If new tool: entry added to `docs/TOOL_CONTRACT.md`
- [ ] If security-sensitive: row added to `docs/THREAT_MODEL.md`
- [ ] If operational: entry added to `docs/RUNBOOK.md`
- [ ] CHANGELOG.md updated
- [ ] CI green: 240+ unit + 8 mutation guards + pip-audit

## Filing issues

Use labels: `bug`, `feature`, `security`, `incident`. For security issues, **do not file publicly** — open a private security advisory at https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new instead.
