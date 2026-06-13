# CLAUDE.md — appscriptly

Orientation for a fresh Claude session. Keep this a stable map; **live status lives in the planning docs** (see bottom), not here.

## What this is

**appscriptly** is a Workspace-automation MCP server. Its differentiator is generating **persistent Apps Script automations** that live in the user's Google Workspace and run on Google's infrastructure — bound scripts, custom menus, custom `=FUNCTION()`s, scheduled sheet dashboards, slides-to-video decks — plus create/edit/manage of Google **Docs (native Tabs), Sheets, Slides, and Drive**.

- **Tool prefixes:** `gdocs_*` (legacy, docs-first era — kept indefinitely) and `as_*` (newer, appscriptly-native). Both are first-class; don't mass-rename.
- **Module path:** the implementation package is **`src/appscriptly/`** — the module path and distribution name now match. Remaining identity moves (GitHub repo transfer, Fly app cutover) are tracked in `MIGRATION_READINESS.md`.
- **Distribution:** `pyproject.toml` `name = "appscriptly"`, build backend **hatchling**, packages = `src/appscriptly`. Console scripts: **`appscriptly`** and legacy alias **`google-docs-mcp`**, both → `appscriptly.server:main`.
- **Python:** `>=3.10`. Dep/lock manager: **uv** (`uv.lock`, `uv sync --frozen`).

## Commands

Dev install (pip, editable):
```bash
pip install -e ".[test]"           # runtime + test extras
# or, matching CI exactly:
uv sync --frozen --all-extras
```

Run the server:
```bash
appscriptly                         # stdio (Claude Desktop / Code) — default
MCP_TRANSPORT=http appscriptly      # HTTP transport (Fly / claude.ai connector); also: appscriptly --http
```

Tests + lint — **these are the exact CI commands** (`.github/workflows/test.yml`, `e2e.yml`):
```bash
pytest tests/unit -v --cov-fail-under=55        # unit suite + coverage gate (matrix 3.10–3.13)
uv run pytest tests/integration/ -v             # integration (mocked Google endpoints)
uv run pyright src/                             # type check — src/ scope only
uv run ruff check src/ tests/                   # lint (ruff config: TID rule family)
```
(`pytest.ini` adds coverage *measurement* by default; the `--cov-fail-under=55` *gate* is applied only on the unit CLI invocation.)

Golden tool-surface (regenerate ONLY for a deliberate tool change; commit the diff in the same PR):
```bash
python scripts/freeze_tool_surface.py           # rewrite tests/golden/tool_surface.json
python scripts/freeze_tool_surface.py --check   # CI mode: fail on drift, no write
```
Must run **as a file**, never `python -c` (src-layout import-resolution constraint).

Deploy (Fly) — normally automatic: push to `main` runs `.github/workflows/deploy.yml` (gated on unit + mutation jobs → builds image → `flyctl deploy --image …` → `/health` smoke check). App: **`sundeepg98-docs-mcp`**.

## Architecture (brief)

- **FastMCP server**, constructed in `src/appscriptly/server.py` as `FastMCP("appscriptly", …, on_duplicate="error")`.
- **Auto-discovery registration (#144):** a `pkgutil.walk_packages` loop imports every leaf module under `services/` (skipping `_`-prefixed modules + the `{api, scopes}` denylist). Tools register as a side effect of import; **no central registry edit** to add one.
- **Boot guards (fail loud before serving):** (1) any discovery import error → `RuntimeError` at module load; (2) `on_duplicate="error"` turns a duplicate tool name into a boot crash; (3) `_MIN_EXPECTED_TOOL_COUNT` floor (currently 57) catches a silent surface drop.
- **Services:** `services/{docs,sheets,slides,drive,apps_script,gas_deploy,admin}/`. Each defines tools via the **`@workspace_tool(service=…, scopes=[…], creds=…)`** decorator (in `_tool_helpers.py`) and declares its surface in **`_expected_tools.py::EXPECTED`**.
- **Tool-surface witnesses (3, must agree):** live `mcp.list_tools()` == union of every `_expected_tools.py::EXPECTED` == `tests/golden/tool_surface.json`. Enforced by `tests/unit/services/test_tool_registration.py`.
- **OAuth:** FastMCP `GoogleProvider`/`OAuthProxy` handles the connector auth; a **second Google-API grant** (`oauth_google.py`, routes under `/oauth/google/api/*`) obtains the usable Workspace tokens for tool calls. HTTP startup calls `configure_auth_for_http(mcp)`.
- **State + runtime:** per-user state DB on the Fly **`/data` volume**; `FASTMCP_HOME=/data/fastmcp` keeps OAuth-proxy state on the volume (survives deploys). Container runs **non-root** (entrypoint reconciles `/data` ownership then drops privileges). `assert_state_db_writable()` runs at HTTP startup.

## Conventions / guardrails (persistent)

- **Verify via the REAL entry path** (console script / pytest / in-container), **not `python -c`** — under the src layout, `-c` under-registers the `services` namespace and gives false tool-count readings.
- **CI is the arbiter.** The type/lint gate is `pyright src/` + `ruff check src/ tests/` (note the **`src/` scope** for pyright). Ignore worktree-only import-resolution noise that CI doesn't reproduce.
- **Adding a service or tool** = define it under `services/X/` with `@workspace_tool` + update that service's **`_expected_tools.py`** + **re-freeze** the golden surface (and bump `_MIN_EXPECTED_TOOL_COUNT` only when deliberately growing the floor).
- **Behavior-preserving changes keep the golden-surface diff at zero** — if `tool_surface.json` changes unexpectedly, that's a real surface change to explain, not noise to re-freeze away.
- **Keep ROADMAP self-current:** when you finish a ROADMAP.md item, flip its line to DONE (with the PR #) IN THE SAME PR as the code. ROADMAP.md is the source of truth for what's shipped; memory only *points* to it, never restates status — so the done-marker travels with the code and can't drift.
- Google API calls go through the `google_api_client` chokepoint; a ruff `TID251` rule bans bare `googleapiclient.discovery.build` imports elsewhere.
- Deps are CVE-floor-pinned; after editing dependencies, regenerate `uv.lock` (`uv sync`) — CI uses `--frozen` and fails on drift.

## Current status & live context

Do **not** rely on prose here for volatile state — read the planning docs in the repo root:

- **`ROADMAP.md`** — feature / hardening / architecture roadmap (synthesized; pending a verified code audit).
- **`PHASE1_VERIFICATION_KIT.md`** — Google OAuth verification + dedicated-client plan and operator punch-list.
- **`MIGRATION_READINESS.md`** — the rename/migration surface (remaining identity moves: GitHub repo transfer + Fly app cutover) and sequencing.

Project ADRs live in `docs/adr/`. Repo URLs / GitHub org and the Fly app name may move per `MIGRATION_READINESS.md`.
