# Contributing to appscriptly

Thanks for your interest in contributing! This guide covers the essentials.
For the full developer map (commands, architecture, conventions) see **CLAUDE.md**
in the repo root — this file stays short and points there to avoid drift.

> Note: the GitHub repo and Python module are still named `google-docs-mcp` /
> `appscriptly` (rename to `appscriptly` is pending — see MIGRATION_READINESS.md).

## Project layout

```
src/appscriptly/
├── server.py                       # FastMCP entry; auto-discovers + registers tools
├── _decorators.py                  # @workspace_tool (auth + creds + registration)
├── google_api_client.py            # single chokepoint for Google API calls
├── auth.py / oauth_google.py       # OAuth + credential handling
└── services/                       # one package per Workspace surface
    └── {docs,sheets,slides,drive,apps_script,gas_deploy,admin}/
        ├── *.py                    # tool modules
        └── _expected_tools.py      # per-service tool manifest (a surface witness)
tests/golden/tool_surface.json      # repo-wide golden tool surface
scripts/freeze_tool_surface.py      # regenerate/verify the golden surface
```

## Adding a new tool

Tools are **auto-discovered** — there is no central tool list in `server.py`. To add one:

1. Add an `async def` in the appropriate `services/<surface>/` module.
2. Decorate it with `@workspace_tool(service=..., scopes=..., creds=...)` (from
   `_decorators.py`) — **not** raw `@mcp.tool()`. The decorator handles auth,
   credential injection, and registration.
3. Write a clear docstring (it's the tool description shown to the LLM) + type hints
   (they generate the input schema).
4. Route any Google API calls through `google_api_client` (the single chokepoint).
5. Add the tool to that service's `_expected_tools.py` manifest.
6. Re-freeze the golden surface: `python scripts/freeze_tool_surface.py`.
7. Add tests, then run the suite (see below).

A behavior-preserving change must keep the golden surface diff at **zero**
(`python scripts/freeze_tool_surface.py --check`).

## Code style

- Follow PEP 8; type hints throughout; keep functions focused and testable.
- `ruff` + `pyright` (over `src/`) define style/type green — see Testing.

## Testing

Run exactly what CI runs before submitting:

```bash
pytest tests/unit -v --cov-fail-under=55     # unit + coverage gate
uv run pytest tests/integration/ -v          # integration
uv run pyright src/                           # type check
uv run ruff check src/ tests/                # lint
python scripts/freeze_tool_surface.py --check # tool surface in sync
```

- Add tests for new functionality. **CI is the arbiter** of green — ignore
  worktree-only import-resolution noise from your editor.
- If you change dependencies, update `uv.lock` (`uv lock`) — CI installs `--frozen`.

## Questions?

File an issue.
