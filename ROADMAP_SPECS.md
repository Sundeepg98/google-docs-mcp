# appscriptly — ROADMAP Build Specs (DRAFT)

> **STATUS: DRAFT for operator review — untracked, NOT committed.** Generated
> 2026-05-30 by a read-only analysis pass. Turns the top remaining `ROADMAP.md`
> items into ready-to-build specs so a fresh session can pick one and start
> without re-deriving scope. Every file path below was read on disk in THIS
> checkout; precedents are cited by file + function. Where the implementation
> shape can't be settled read-only, it says **needs design decision** instead of
> inventing.
>
> **Checkout note:** this repo's `origin` is still
> `github.com/Sundeepg98/google-docs-mcp` (the pre-rename name; the
> module/dist already renamed to `appscriptly` per CLAUDE.md — the GitHub/Fly
> identity moves are tracked in `MIGRATION_READINESS.md`). `src/appscriptly/` is
> tracked and committed. Current branch when this was written:
> **`feat/google-client-timeout`** (i.e. the in-flight timeout work is on this
> branch — see "In-flight items").

## Ground rules baked into every spec (verified against CLAUDE.md + the code)

- **Adding a tool** = (1) add the impl fn to the service's `api.py`; (2) add the
  `@workspace_tool(service="<svc>", ...)`-decorated tool in the service's
  `tools.py` (or, for apps_script, its own feature module); (3) add the tool
  name to that service's **`_expected_tools.py::EXPECTED`** frozenset; (4)
  add the tool's output schema to `src/appscriptly/tool_schemas.py` (every tool
  passes `output_schema=...` — see the `GSHEETS_*_OUTPUT_SCHEMA` constants); (5)
  **re-freeze** the golden surface: `python scripts/freeze_tool_surface.py` (run
  AS A FILE, never `python -c` — src-layout import constraint) and commit the
  `tests/golden/tool_surface.json` diff in the same PR; (6) bump
  `_MIN_EXPECTED_TOOL_COUNT` in **`src/appscriptly/server.py:340`** (currently
  **39**) when growing the floor.
- **3 witnesses must agree** (enforced by
  `tests/unit/services/test_tool_registration.py`): live `mcp.list_tools()` ==
  union of every `_expected_tools.py::EXPECTED` == `tests/golden/tool_surface.json`.
  A 4th hand-set lives in `tests/unit/test_tool_schemas.py` (`EXPECTED_TOOLS`)
  guarding schema/description contracts — update it too when adding a tool.
- **Behavior-preserving / no-new-tool changes → golden zero-diff.** A surprise
  golden diff is a real surface change to explain, not noise to re-freeze.
- **Unit-test pattern (verified in `tests/unit/services/sheets/test_api.py`):**
  NOT `@patch`. Use the injected chokepoint —
  ```python
  from appscriptly.google_api_client import InMemoryGoogleAPIClient, with_google_api_client
  stub = MagicMock(name="sheets-v4")
  stub.spreadsheets().values().append().execute.return_value = {...}
  with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): stub})):
      result = append_rows(MagicMock(), "SHEET", "A1", [["x"]])
  # then assert on stub.spreadsheets().values().append.call_args.kwargs
  ```
  Tests are co-located per service: `tests/unit/services/<svc>/test_api.py` and
  `.../test_tools.py`.
- **All Google calls** go through the chokepoint:
  `get_service("<svc>", "<ver>", credentials=creds)` (from
  `appscriptly.google_clients`) + `execute_with_retry(lambda: ....execute(),
  idempotent=<bool>, op_name="...")` (from `appscriptly.google_api_client`). A
  ruff TID251 rule bans bare `googleapiclient.discovery.build` elsewhere. Note
  the api functions take **`creds` as the first positional arg**.
- **CI gates:** `pytest tests/unit -v --cov-fail-under=55`, `uv run pytest
  tests/integration/ -v`, `uv run pyright src/`, `uv run ruff check src/ tests/`.

## In-flight items — DO NOT touch (avoid collision)

The two items the task named as in-flight are **NOT visible as uncommitted work
in this checkout** — `git status` is clean of Sheets/`google_api_client` changes
(the only working-tree noise is stray `.r_*.txt` / `START_HERE.md` dotfiles, and
this draft). So they're presumably on **separate branches / open PRs**, not in
this working tree. They are still off-limits per the task; the file-overlap notes
below are about where those PRs land, so you can avoid double-editing the same
files when they merge.

1. **Sheets `batchUpdate` request-builder** (Architecture-P1/L; ROADMAP "Top 5"
   #1) — IN-FLIGHT (other branch/PR). Confirmed not-yet-present in THIS tree:
   `services/sheets/api.py` ships only `read_range`/`write_range`/
   `create_spreadsheet`, its docstring still says the batchUpdate tagged-union is
   "DELIBERATELY DEFERRED", and there is **no `_batch.py`** and **no
   `gsheets_format_range`/`gsheets_add_sheet`** in `_expected_tools.py` here.
   Everything that "rides the batchUpdate plumbing" in **Sheets** depends on that
   builder and is **ordering-gated** behind it — see "Blocked on the in-flight
   Sheets batch-builder" at the bottom. Don't start those yet; expect the
   in-flight PR to add a `services/sheets/_batch.py`-style module + touch
   `sheets/api.py`/`tools.py`/`_expected_tools.py` + the golden, so rebase the
   Sheets items below onto it after it merges.
2. **google-client socket/transport timeout** (Hardening-P1/S; ROADMAP "Top 5"
   #3) — IN-FLIGHT (the current branch name is `feat/google-client-timeout`, but
   the timeout change is NOT in the working tree — `google_api_client.py` is
   unmodified here, so the actual edit is presumably committed on the branch /
   in its PR, or pending). It lands in `src/appscriptly/google_api_client.py`
   (the `GoogleApiClientAdapter.get_service` / `build(...)` path, ~line 133-140)
   and `tests/unit/test_google_api_client.py` /
   `test_retrying_google_api_client.py`. **Avoid editing those files** to prevent
   merge conflicts.

> All ten specs below are independent of BOTH in-flight items by API surface.
> The only overlap is *file-level*: the Sheets specs (#1/#2/#6) edit the same
> `services/sheets/` files the batch-builder PR will touch, so once that PR is
> open, rebase onto it rather than racing it. Drive (#3/#4/#5), Slides (#9),
> scopes (#7), apps_script (#8), and pyproject (#10) touch entirely separate
> files.

---

# Build-ready specs (ordered: grab #1 first)

Ordering = build-readiness for a fresh session (clear precedent + no in-flight
dependency + bounded blast radius), not raw ROADMAP priority. The two
highest-ranked ROADMAP items are in-flight, so the best *startable* work begins
here. Each item is independent of both in-flight PRs unless noted.

---

## 1. Sheets: append rows — `gsheets_append_rows`  ⭐ BEST FIRST ITEM
**ROADMAP:** Features-P1 *(P1/S/high)*. **Effort: S.**

> Note: shares the `services/sheets/` files with the in-flight batch-builder PR
> (file-level overlap, not API dependency). If that PR is already open, rebase
> onto it; if not, this is clean to start. Either way it's the smallest, nearest-
> precedent add.

- **What + why:** Add `gsheets_append_rows` calling `values().append`. Today
  appending requires a race-prone read-then-find-empty-row-then-write.
- **Files to touch:**
  - `src/appscriptly/services/sheets/api.py` — add `append_rows(creds,
    spreadsheet_id, range_str, values, value_input_option="USER_ENTERED")`.
    (Clean ADD — there is no existing stub; the module currently has exactly
    `read_range`, `write_range`, `create_spreadsheet`.)
  - `src/appscriptly/services/sheets/tools.py` — add the `@workspace_tool`.
  - `src/appscriptly/tool_schemas.py` — add `GSHEETS_APPEND_ROWS_OUTPUT_SCHEMA`.
  - `src/appscriptly/services/sheets/_expected_tools.py` — add
    `"gsheets_append_rows"` to `EXPECTED`.
  - `tests/unit/services/sheets/test_api.py` (+ `test_tools.py`).
- **Approach:** Mirror `write_range` in the same file (it's the nearest twin),
  but call `.values().append(...)`:
  ```python
  resp = execute_with_retry(
      lambda: sheets.spreadsheets().values().append(
          spreadsheetId=spreadsheet_id, range=range_str,
          valueInputOption=value_input_option,
          insertDataOption="INSERT_ROWS",
          body={"values": values}).execute(),
      idempotent=False, op_name="sheets.values.append")
  ```
  Reuse `write_range`'s same `ValueError` guards (empty / non-2D). **idempotent
  must be False** — re-running append duplicates rows (contrast: `write_range`
  is `idempotent=True`). Tool annotations: `readonly=False, destructive=False,
  idempotent=False`.
- **Tool-surface impact:** **Adds a tool** → `_expected_tools.py` + `EXPECTED_TOOLS`
  in `test_tool_schemas.py` + re-freeze golden + bump `_MIN_EXPECTED_TOOL_COUNT`
  39→40.
- **Test plan:** in `tests/unit/services/sheets/test_api.py`, add a
  `stub_sheets_for_append` fixture (mirror `stub_sheets_for_write`) and assert:
  body shape `{"values": ...}`, `insertDataOption="INSERT_ROWS"`, and that the
  `value_input_option` kwarg threads through (default USER_ENTERED; RAW when
  passed). Mirror `test_write_range_*`.
- **Risk/gotchas:** Minimal. No auth/volume/deploy surface. `spreadsheets` scope
  already in BOTH consent lists — no scope change. The one correctness note:
  `idempotent=False` (don't copy write_range's `True`).
- **Dependencies/ordering:** **API-independent of both in-flight items**
  (`values.append` is a values-API call, not `spreadsheets.batchUpdate`). The
  only caveat is file-level overlap with the Sheets batch-builder PR (same
  `sheets/api.py`/`tools.py`/`_expected_tools.py`/test/golden) — rebase onto it
  if that PR is open. **Best first item:** smallest diff, nearest precedent
  (`write_range`, one function above it), and it exercises the full add-a-tool
  ritual end to end.

---

## 2. Sheets: clear a range — `gsheets_clear_range`
**ROADMAP:** Features-P2 *(P2/S/high)*. **Effort: S.**

> Note: same file-level overlap with the Sheets batch-builder PR as #1 (rebase
> if that PR is open). API-independent.

- **What + why:** Add `gsheets_clear_range` via `values().clear`. `write_range`
  deliberately leaves unwritten cells alone, so today there's no way to blank a
  region.
- **Files to touch:** `sheets/api.py` (+`clear_range`), `sheets/tools.py`,
  `tool_schemas.py` (+`GSHEETS_CLEAR_RANGE_OUTPUT_SCHEMA`),
  `sheets/_expected_tools.py`, `tests/unit/services/sheets/test_api.py`.
- **Approach:** `sheets.spreadsheets().values().clear(spreadsheetId=...,
  range=range_str, body={})` wrapped in `execute_with_retry(idempotent=True)`
  (clearing a fixed range twice is a no-op). Return `{"cleared_range": resp.get(
  "clearedRange", range_str)}`.
- **Tool-surface impact:** **Adds a tool** → `_expected_tools.py` +
  `test_tool_schemas.py` + re-freeze + bump floor.
- **Test plan:** assert `values().clear` gets the right `spreadsheetId`/`range`
  and the envelope maps `clearedRange`. Mirror the sheets test fixtures.
- **Risk/gotchas:** Minimal; values-API call, not batchUpdate. No scope change.
- **Dependencies/ordering:** None; independent of in-flight items. Natural pair
  with item #1 (same file, same PR size) — could ship together.

---

## 3. Drive: folder creation — `gdocs_create_folder`
**ROADMAP:** Features-P1 *(P1/S/high)*. **Effort: S.**

- **What + why:** Add `files().create` with
  `mimeType:'application/vnd.google-apps.folder'`. Today only move-into-existing
  exists; the `gdocs_move_to_folder` docstring already admits the gap.
- **Files to touch:** `src/appscriptly/services/drive/api.py` (+`create_folder`),
  `drive/tools.py`, `tool_schemas.py` (+ schema), `drive/_expected_tools.py`,
  `tests/unit/services/drive/test_api.py`.
- **Approach:** Mirror the `files().create` call already in `api.py`'s
  `upload_and_convert_docx` (the create-with-body shape) but no media:
  ```python
  body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
  if parent_id: body["parents"] = [parent_id]
  file = execute_with_retry(
      lambda: drive.files().create(body=body, fields="id,name,webViewLink").execute(),
      idempotent=False, op_name="drive.files.create.folder")
  ```
  App-created folders are writable under `drive.file`. `drive.file` already in
  both consent lists. Annotations: `readonly=False, destructive=False,
  idempotent=False`.
- **Tool-surface impact:** **Adds a tool** → `_expected_tools.py` +
  `test_tool_schemas.py` + re-freeze + bump floor.
- **Test plan:** `tests/unit/services/drive/test_api.py` — stub
  `drive.files().create().execute`, assert the body carries the folder mimeType
  and `parents` when given. Same `with_google_api_client(InMemoryGoogleAPIClient
  ({("drive","v3"): stub}))` pattern the file already uses for `find_doc_by_title`.
- **Risk/gotchas:** Minimal. No auth/volume/deploy surface.
- **Dependencies/ordering:** None. Touches only the `drive` service folder — zero
  file overlap with the in-flight Sheets or timeout work. A good alternative
  first pick if you'd rather avoid even the file-level Sheets overlap of #1.

---

## 4. Drive: sharing revoke — `gdocs_revoke_permission`
**ROADMAP:** Features-P1 *(P1/S/high)*. **Effort: S.**

- **What + why:** Add `gdocs_revoke_permission(file_id, permission_id)` via
  `permissions().delete()`. Grant + list exist; the inverse is referenced in the
  `gdocs_share_file` / `gdocs_list_permissions` docstrings ("a future
  `gdocs_revoke_permission` tool will accept it") but unbuilt. `permission_id` is
  already surfaced by `gdocs_list_permissions`.
- **Files to touch:** `src/appscriptly/services/drive/sharing.py` (+`revoke_permission`
  — the grant/list helpers live HERE, not in `drive/api.py`), `drive/tools.py`
  (wrap it, alongside the existing share/list tool wrappers), `tool_schemas.py`,
  `drive/_expected_tools.py`, `tests/unit/services/drive/test_sharing.py`.
- **Approach:** Mirror `grant_permission`/`list_permissions` in `sharing.py`:
  `drive.permissions().delete(fileId=file_id, permissionId=permission_id)` +
  `execute_with_retry(idempotent=True)` (delete-by-id is idempotent). `delete`
  returns empty body → return `{"revoked": True, "permission_id": permission_id,
  "file_id": file_id}`. Consider mirroring the drive tools' soft-failure contract
  for 403 `appNotAuthorizedToFile` / 404 (see `trash_drive_file` for the exact
  pattern) so batch teardown can skip-and-continue. Annotations: `readonly=False,
  destructive=True, idempotent=True`.
- **Tool-surface impact:** **Adds a tool** → `_expected_tools.py` +
  `test_tool_schemas.py` + re-freeze + bump floor.
- **Test plan:** `test_sharing.py` — assert `permissions().delete` gets the right
  `fileId`/`permissionId`; if you add soft-failure handling, test the 403/404
  branches like `test_soft_failure_contracts.py` does.
- **Risk/gotchas:** Minimal. Mutates sharing but no auth-config/volume/deploy
  surface. No scope change.
- **Dependencies/ordering:** None. Pairs naturally with item #3 (same service).

---

## 5. Drive: generalized search — generalize `gdocs_find_doc_by_title`
**ROADMAP:** Features-P1 *(P1/M/high)*. **Effort: M.**

- **What + why:** Today `find_doc_by_title` hardcodes a Docs/.docx mimeType
  OR-filter (`api.py` ~line 345), silently excluding Sheets/Slides/PDF/folders.
  Add optional `mime_type`, `full_text`, and `parent_folder_id` params so other
  verticals are reachable.
- **Files to touch:** `drive/api.py` (`find_doc_by_title` — generalize the `q`
  builder; the function already lives here), `drive/tools.py`
  (`gdocs_find_doc_by_title` — add optional params + update docstring),
  `tests/unit/services/drive/test_api.py`.
  - **This item does NOT add a tool** — `gdocs_find_doc_by_title` already exists
    in `_expected_tools.py`. It changes the *signature*, not the surface. (The
    ROADMAP calls it `gdocs_find_file`; the real existing tool is
    `gdocs_find_doc_by_title` — generalize that rather than add a new one, OR
    decide to add a distinct `gdocs_find_file` — see design note.)
- **Approach:** In `find_doc_by_title`, make the hardcoded
  `(mimeType = 'application/vnd.google-apps.document' OR ...docx)` clause
  conditional: when `mime_type` is supplied, emit `and mimeType='<mime_type>'`
  instead; when `full_text` is supplied, add `and fullText contains '<...>'`;
  when `parent_folder_id`, add `and '<id>' in parents`. **Quote-escaping is
  ALREADY done** (`safe_query = query.replace("'", "\\'")` at ~line 342) — reuse
  the same escape on any new user-supplied string params.
  - **needs design decision:** keep ONE generalized tool (add optional params to
    `gdocs_find_doc_by_title`, default behavior unchanged) vs add a SEPARATE
    `gdocs_find_file` (matches ROADMAP wording but means a new tool + golden bump
    + a near-duplicate). Recommend generalizing the existing tool (golden
    zero-diff, no new surface) unless the operator wants the distinct name for
    discoverability.
- **Tool-surface impact (if generalizing existing tool):** **No new tool →
  golden zero-diff.** (If you instead add `gdocs_find_file`: adds a tool → all
  the add-a-tool steps.)
- **Test plan:** extend `test_api.py`: (a) default call still emits the
  Docs/.docx clause; (b) `mime_type` overrides it; (c) `full_text` adds the
  `fullText contains` clause; (d) `parent_folder_id` adds `in parents`. Assert on
  the `q` string passed to `files().list` (the file already has helpers that
  capture `list` kwargs).
- **Risk/gotchas:** Backward-compat — all new params optional with defaults so
  existing callers and the golden are unaffected. (Quote-escaping is NOT a gotcha
  here — already handled.)
- **Dependencies/ordering:** None; independent of in-flight items.

---

## 6. Sheets: value-input mode safety (RAW option)
**ROADMAP:** Features-P2 *(P2/S/high)*. **Effort: S.**

> Note: same file-level overlap with the Sheets batch-builder PR as #1/#2 (and
> best folded into #1). API-independent.

- **What + why:** `write_range` (and `gsheets_write_range`) **hardcode**
  `valueInputOption="USER_ENTERED"` (api.py line 155; the test
  `test_write_range_uses_USER_ENTERED_value_input_option` pins it). Add an
  optional `value_input_option` (`USER_ENTERED`|`RAW`) param so values starting
  with `=` aren't silently interpreted as formulas. (Correcting an earlier
  assumption: this is NOT already done — the ROADMAP is right that it's hardcoded.)
- **Files to touch:** `sheets/api.py` (`write_range`, and `append_rows` if item
  #1 landed) + `sheets/tools.py` (expose the param). No new tool.
- **Approach:** Add `value_input_option: str = "USER_ENTERED"` to `write_range`
  (validate it's one of the two, else `ValueError`); pass through to the API
  call. Thread the same kwarg through the `gsheets_write_range` tool signature +
  docstring.
- **Tool-surface impact:** **No new tool → golden zero-diff** (adding an optional
  kwarg doesn't change the tool-NAME set; the input JSON schema gains a field, so
  re-run `freeze_tool_surface.py --check` to confirm the golden — which is a
  name-list — is unchanged, and update any input-schema assertions in
  `test_tool_schemas.py`/`test_tool_output_schemas.py` if they pin inputs).
- **Test plan:** **UPDATE** `test_write_range_uses_USER_ENTERED_value_input_option`
  (it currently asserts the hardcoded value) to assert the *default* is
  USER_ENTERED AND that `value_input_option="RAW"` forwards `RAW`. This is a
  deliberate change to a pinned-invariant test — call it out in the PR.
- **Risk/gotchas:** Low. The one gotcha is that an existing test pins the
  hardcoded value — expect to edit it (don't "re-freeze away" — it's an
  intentional behavior addition). Best folded into item #1.
- **Dependencies/ordering:** Best done **with or right after item #1**.
  Independent of the batch-builder.

---

## 7. Hardening: derive the OAuth consent union from per-service scopes
**ROADMAP:** Hardening-P1 *(P1/M/high)* — also ROADMAP "Top 5" #2 / "the one
genuine multi-service seam". **Effort: M. ⚠️ needs-care (touches OAuth/consent).**

- **What + why:** Two central scope lists are hand-synced today —
  `auth.SCOPES` (stdio) and `oauth_google.GOOGLE_API_SCOPES` (HTTP) — a classic
  drift bug ("stdio works, HTTP 403s"). Replace both with one computed union
  harvested from per-service scope declarations.
- **Files to touch:**
  - `src/appscriptly/scopes.py` — implement the harvester (the module exists; the
    ROADMAP says it's "a consent no-op" today). (Could not read it directly this
    pass — confirm its current contents first.)
  - Per-service scope decls: **today only `services/apps_script/scopes.py`
    (`GAS_BOUND_SCOPES`) and `services/gas_deploy/scopes.py` (`GAS_DEPLOY_SCOPES`)
    exist** — exactly the ROADMAP's "2 of 7". You must ADD a scope list to
    `docs`, `sheets`, `slides`, `drive` (and `admin` if it needs any). **Design
    decision below: pick a uniform constant name.**
  - `src/appscriptly/auth.py` — `SCOPES = <computed union>` (stdio variant).
  - `src/appscriptly/oauth_google.py` — `GOOGLE_API_SCOPES = <computed union>`
    (HTTP variant = stdio + `IDENTITY_SCOPES` `openid`/`userinfo.email`).
  - Tests: `tests/unit/test_base_tier_scopes.py` is the AUTHORITATIVE pin — it
    hardcodes `_TARGET_CONNECTOR` and `_TARGET_STDIO` sets and asserts the two
    lists equal them (its own comment even says "Future: a per-service scopes.py
    + computed union removes this dual-maintenance"). Also `tests/unit/test_scopes.py`.
- **Approach:** In `scopes.py`, mirror the `#144` pkgutil discovery (working
  template: `test_tool_registration.py::_declared_by_service` walks
  `appscriptly.services` importing each `<svc>.<module>`). Walk the service
  packages, import each `<svc>.scopes`, union their per-service scope constant +
  the fixed identity scopes; expose `compute_stdio_union()` and
  `compute_connector_union()`. Keep both **sorted+deduped** for a stable consent
  string. Both `auth.SCOPES` and `oauth_google.GOOGLE_API_SCOPES` then read them.
  - **needs design decision (naming):** the two existing files use bespoke names
    (`GAS_BOUND_SCOPES`, `GAS_DEPLOY_SCOPES`), not a uniform `SCOPES`. Decide the
    single harvested symbol per service (e.g. standardize on `SCOPES`, or have
    the harvester read a documented attribute). Recommend a uniform
    `SCOPES: list[str]` per `services/<svc>/scopes.py`, with the two existing
    constants kept as aliases for their tool decorators.
  - **needs design decision (restricted-tier seam):** the base tier must stay
    RESTRICTED-scope-free (`test_base_tier_scopes._RESTRICTED`). The union must
    NOT pull in a restricted scope. Decide how a future restricted tier opts in
    (this dovetails with the Architecture-P1 scope-tier-groups item, item not in
    this batch).
- **Tool-surface impact:** **No new tool → golden zero-diff.** The *set* of
  scopes in each union MUST equal today's literal lists exactly (this is a pure
  refactor) — verify against `test_base_tier_scopes`'s pinned sets.
- **Test plan:** Make `compute_*_union()` return values equal the existing
  `_TARGET_STDIO` / `_TARGET_CONNECTOR` sets (reuse those pins). Add: (a) each
  service's scope decl ⊆ its union; (b) `auth.SCOPES == compute_stdio_union()`
  and `oauth_google.GOOGLE_API_SCOPES == compute_connector_union()`; (c) a
  fixture service with a new scope proves the union auto-picks it up.
- **Risk/gotchas:** **needs-care.** This is the LIVE consent surface — a dropped
  scope = silent runtime 403s on tool calls. Mitigations: (1) the union must be
  byte-identical to today's sets on this change — pin it; (2) do NOT bundle any
  actual scope addition in the same PR; (3) `test_base_tier_scopes` already
  guards against accidentally adding a restricted scope — keep it green.
- **Dependencies/ordering:** Independent of both in-flight items. **Prerequisite
  enabler** for the Architecture-P1 Gmail/Calendar scope-tier work (makes adding
  a service a one-file edit). Higher leverage than the small Sheets/Drive items
  but higher care; a confident session could treat it as the #1 *strategic* pick.
  Build-readiness ranks it here because of the consent-surface care required.

---

## 8. Apps Script: reactive/event triggers — `as_install_edit_trigger` / `as_install_form_handler`
**ROADMAP:** Features-P1 *(P1/M/high)*. **Effort: M. ⚠️ partial needs-design.**

- **What + why:** The manifest/plan layer is event-aware but no shipped tool
  synthesizes a working installable **event** trigger (only `onOpen` simple
  triggers + time-based ship via `as_install_sheet_dashboard`; **Forms are
  explicitly rejected** — see `services/apps_script/api.py` ~line 109: "Forms are
  technically bindable but their automation [is rejected]"). The server banner
  advertises "reactive automations".
- **Files to touch:**
  - `src/appscriptly/services/apps_script/sheet_dashboard.py` is the precedent
    for `.gs` trigger-body generation (`_trigger_builder_expr`,
    `build_dashboard_script_body`, which emit
    `ScriptApp.newTrigger(name).timeBased()...create()`). Add analogous pure
    builders for `.forSpreadsheet(id).onEdit().create()` /
    `.forForm(id).onFormSubmit().create()` — either in a NEW feature module
    (e.g. `services/apps_script/event_trigger.py`) or extend an existing one.
    (Note: there is **no `gas_source.py`** — correcting an earlier assumption.)
  - `src/appscriptly/services/apps_script/api.py` — reuse `create_bound_project` /
    `set_project_content` / `create_deployment` (the same primitives
    `as_generate_bound_script` uses). The actual install can also just be a
    curated `script_body` fed to the existing generic
    `as_generate_bound_script` — see design note.
  - The new tools' `@workspace_tool` decorations (their own feature module).
  - `src/appscriptly/services/apps_script/_expected_tools.py` — add both names.
  - **`tests/unit/services/test_tool_registration.py::_APPS_SCRIPT_TOOL_MODULE`**
    — apps_script tools require an entry in THIS map too (the test asserts the
    map and `_expected_tools.py` cover exactly the same set). Easy-to-miss extra
    step unique to apps_script.
  - `tool_schemas.py` (+ schemas); `services/apps_script/scopes.py` already has
    `script.projects`/`script.deployments`; trigger creation needs
    `script.scriptapp` at the generated-manifest level (the manifest builder
    derives scopes — verify it adds `script.scriptapp` for a trigger).
  - Tests under `tests/unit/services/apps_script/`.
- **Approach:** `.gs` body uses
  `ScriptApp.newTrigger(fn).forSpreadsheet(id).onEdit().create()` and
  `.forForm(id).onFormSubmit().create()`. Lift the Forms rejection **only on the
  form-submit path**.
  - **needs design decision (two-part):** (1) **build a dedicated tool** with its
    own feature module + `_APPS_SCRIPT_TOOL_MODULE` entry, OR **expose a thin
    recipe** that calls the existing `as_generate_bound_script` with a generated
    body (fewer moving parts, but then it may not need a new tool at all). (2)
    **Where exactly the Forms rejection lives** and how to lift it for the
    form-submit path only — it's in `apps_script/api.py` (~line 109) and possibly
    the manifest/`build_manifest` path; read both `api.py` and the manifest
    builder before implementing. Couldn't be fully pinned read-only.
- **Tool-surface impact:** **Adds tool(s)** (likely 2) → `_expected_tools.py` +
  `_APPS_SCRIPT_TOOL_MODULE` + `test_tool_schemas.py` + re-freeze + bump floor by
  the number added.
- **Test plan:** unit-test the **pure `.gs` string builders** (assert the
  generated source contains `newTrigger`, `forSpreadsheet`/`forForm`,
  `.onEdit()`/`.onFormSubmit()`) — cheap and high-signal, mirroring how
  `sheet_dashboard.py` builders are tested. Mock the create/deploy path like
  `tests/unit/services/apps_script/test_*`.
- **Risk/gotchas:** The Forms-rejection lift is the design risk (it was rejected
  deliberately). `script.scriptapp` must end up in the generated manifest's
  `oauthScopes` (the bound script requests its own scopes at run time, separate
  from the connector consent). Not a /data or deploy-path change.
- **Dependencies/ordering:** Independent of in-flight items. Blocked only by the
  two design decisions above.

---

## 9. Slides: `createSlide` + `insertText` — `gslides_create_slide` / `gslides_insert_text`
**ROADMAP:** Features-P1 *(both P1/M/high)*. **Effort: M (pair).**

- **What + why:** Decks are stuck at one default slide and `replace_all_text`
  can't author new text (a "workflow dead-end" — nothing in-service creates the
  placeholder deck it targets). These two tools close the authoring loop.
- **Files to touch:** `src/appscriptly/services/slides/api.py` (+`create_slide`,
  `insert_text`), `slides/tools.py`, `tool_schemas.py` (+ schemas),
  `slides/_expected_tools.py`, `tests/unit/services/slides/test_api.py`.
- **Approach:** Direct batchUpdate — **precedent is `replace_all_text` in the
  SAME file** (`slides.presentations().batchUpdate(presentationId=...,
  body={"requests":[...]})` + `execute_with_retry`). Requests:
  - createSlide: `{"createSlide": {"slideLayoutReference":
    {"predefinedLayout": layout}, "insertionIndex": index}}`; return the new
    objectId from `resp["replies"][0]["createSlide"]["objectId"]`.
  - insertText: `{"insertText": {"objectId": object_id, "text": text,
    "insertionIndex": index}}`.
  Thread a `layout` param (e.g. `BLANK`, `TITLE_AND_BODY`). createSlide is
  `idempotent=False` (each call adds a slide); insertText `idempotent=False`.
  - **IMPORTANT:** Slides has its OWN batchUpdate already — this does NOT depend
    on the in-flight **Sheets** batch-builder (that builder is Sheets-only).
    Safe to build now.
- **Tool-surface impact:** **Adds 2 tools** → `_expected_tools.py` +
  `test_tool_schemas.py` + re-freeze + bump floor by 2.
- **Test plan:** mirror `tests/unit/services/slides/test_api.py` (stub
  `presentations().batchUpdate().execute`): assert createSlide returns the new
  objectId from the mocked reply, and insertText forwards the right
  objectId/text/index.
- **Risk/gotchas:** Low. `presentations` scope already present. Build createSlide
  before insertText is useful (insertText needs a target objectId) — ship as a
  pair. No auth/volume/deploy surface.
- **Dependencies/ordering:** Independent of in-flight items. Unblocks several
  downstream Slides P2 items (speaker notes, element-management, non-empty
  `create_presentation`) which reuse insertText/createSlide.

---

## 10. Hardening: coverage `fail_under` backstop in pyproject
**ROADMAP:** Hardening-P3 *(S/high)* (flagged "P2→here"). **Effort: S.**

- **What + why:** The `--cov-fail-under=55` gate lives ONLY in
  `.github/workflows/test.yml` (verified: `pytest.ini` *intentionally omits*
  `--cov-fail-under`, and `pyproject.toml` has **NO `[tool.coverage]` section at
  all** — confirmed 0 matches). Add `fail_under = 55` as a workflow-independent
  backstop so the gate can't be lost by editing CI.
- **Files to touch:** `pyproject.toml` only — add a new
  `[tool.coverage.report]` table with `fail_under = 55`. (There is currently no
  coverage config in pyproject; the `--cov=appscriptly` + omit list live in
  `pytest.ini`.)
- **Approach:** Add:
  ```toml
  [tool.coverage.report]
  fail_under = 55
  ```
  Do NOT bundle the documented 55→56 ratchet (a separate task gated on the
  Py3.11 coverage outlier — see `docs/COVERAGE.md`).
- **Tool-surface impact:** **None → golden zero-diff** (no code, no tools).
- **Test plan:** No new test. Before committing, confirm the unit run is
  comfortably ≥55 (`pytest tests/unit --cov-fail-under=55` already passes in CI,
  so this just mirrors it). CI is the regression guard.
- **Risk/gotchas:** One: `[tool.coverage.report] fail_under` applies to any
  `coverage report` invocation (incl. local `pytest` via pytest-cov's default),
  whereas today only the unit CLI enforces 55. If a *local* run measures <55 it
  would now fail locally too. Confirm local coverage ≥55 first. No
  auth/volume/deploy surface.
- **Dependencies/ordering:** Fully independent. A safe, tiny "warm-up" item.

---

# Blocked on the in-flight Sheets batch-builder (do NOT start yet)

All of these "ride the proven batchUpdate plumbing" per ROADMAP and **depend on
the in-flight Sheets `batchUpdate` request-builder** (`services/sheets/_batch.py`,
Architecture-P1). They become ready specs the moment that builder merges — each
is then a thin wrapper emitting one request type through `_batch.execute_batch_update`
(`api.py` gains a builder fn + a tool; unit tests stub
`spreadsheets().batchUpdate().execute` and assert the request type). Listed
roughly readiest-first:

> Likely to land WITH the in-flight builder PR (so confirm before starting):
> `gsheets_format_range` (`repeatCell`, Features-P1/M) is the ROADMAP's named
> first wrapper and the obvious thing the builder PR demonstrates itself with.
> Check whether the in-flight PR already includes it before speccing it.

- **Sheets: cell & text formatting** `gsheets_format_range` (`repeatCell`) —
  Features-P1/M. The canonical first wrapper on the new builder (verify it isn't
  already part of the builder PR).
- **Sheets: sheet/tab lifecycle** `gsheets_add_sheet`/`delete_sheet`/
  `rename_sheet`/`freeze` (`addSheet`/`deleteSheet`/`updateSheetProperties`) —
  Features-P1/L.
- **Sheets: conditional formatting** `gsheets_add_conditional_format`
  (`addConditionalFormatRule`) — Features-P2/M.
- **Sheets: charts** `gsheets_add_chart` (`addChart`) — Features-P2/M.
- **Sheets: structural ops** insert/delete/resize dimensions, merge cells, data
  validation (`gsheets_insert/delete_dimension`, `merge_cells`,
  `set_data_validation`) — Features-P2/L.
- **Sheets: batch values write/read** `gsheets_batch_write`/`batch_read`
  (`values.batchUpdate`/`batchGet`) — Features-P2/M. (Borderline: these are
  *values*-API batch calls, NOT `spreadsheets.batchUpdate`, so technically
  buildable independently like items #1/#2 — but they conceptually belong with
  the batch work; **confirm with the in-flight author** before touching
  `sheets/api.py` to avoid a collision.)

Each, when unblocked: **adds a tool** → `_expected_tools.py` +
`test_tool_schemas.py` + re-freeze + bump `_MIN_EXPECTED_TOOL_COUNT`.

---

# Larger / lower-readiness items deliberately NOT fully spec'd here

Real ROADMAP items, but L-effort and/or needing a design decision that can't be
made read-only. A fresh session should `superpowers:brainstorming` or
`feature-dev` these rather than treat them as turn-key:

- **Docs: programmatic table creation** (`gdocs_insert_table`) — Features-P1/L.
  Add a pure table-request builder in `services/docs/markdown_render.py` + enable
  a markdown-it table plugin. The Docs service is the ROADMAP's "well-factored"
  exemplar (pure builders in `markdown_render.py`, REST call sites in `api.py`,
  registrations in `tools.py` + `_expected_tools.py`), so the seam is clean — but
  the request-builder + index math is non-trivial. **needs design decision:
  table-cell index math under the Docs UTF-16 indexing bug** (below).
- **Docs: ranged editing** `gdocs_edit_range` (`deleteContentRange` +
  location-indexed `insertText`) — Features-P1/M. **needs-care:** depends on
  correct UTF-16 index handling. **Consider fixing the UTF-16 index bug FIRST**
  (Hardening-P3/S): `markdown_render.py` does `current_index += len(text)`
  (code points) where it should be `+= len(text.encode('utf-16-le')) // 2`; add
  an emoji regression test. It's a small, isolated, high-value precursor that
  unblocks reliable ranged/table editing — arguably a better pick than the L
  items it gates.
- **Docs: richer text/paragraph formatting** `gdocs_format_range`
  (`updateTextStyle`/`updateParagraphStyle`) — Features-P1/M. Same UTF-16 caveat.
- **Apps Script: web-app author-and-deploy** `as_deploy_web_app` (`doGet`/`doPost`
  + webapp entryPoint) — Features-P2/M. The deploy machinery EXISTS
  (`services/gas_deploy/api.py` has `create_version` + `deploy_webapp`, and
  already extracts the live `/exec` URL from `entryPoints`), so it's more ready
  than its peers — but **needs-care (deploy path)** and a design decision on the
  generated `doGet`/`doPost` body + manifest webapp entryPoint. Good *second-tier*
  pick for a session comfortable with the deploy surface.

---

# Architecture/serverless items — NOT in this batch (strategic, multi-PR, needs-care)

The Architecture-P1 serverless/affinity items are high-value but multi-PR and all
touch OAuth / the `/data` volume / the deploy path — every one is **needs-care**
and several **need a design decision**. Out of scope for "grab one and build in a
session"; plan with `superpowers:writing-plans` against `MIGRATION_READINESS.md`.
Flagged so they're not mistaken for turn-key:

- **PKCE `code_verifier` + NonceStore → shared `StorageBackend`** (Arch-P1/M) —
  needs-care (live OAuth path; the verifier is generated in
  `oauth_google.build_authorization_url` and consumed in
  `exchange_code_for_credentials`).
- **FastMCP OAuth-proxy state → KV** (Arch-P1/L) — needs-care; **the top
  remaining engineering item for production-grade serverless**, but a design
  project (wrapping FastMCP's own `OAuthProxy` store; today it's pinned to
  `/data` via `FASTMCP_HOME` with a fail-loud guard in
  `oauth_google._assert_oauth_state_is_persistent`).
- **Scope-tier groups (`BASE_SCOPES` vs `RESTRICTED_SCOPES`)** for opt-in
  Gmail/Calendar (Arch-P1/L) — needs-care (consent/CASA; `test_base_tier_scopes`
  is the guardrail). **Depends on item #7** (the scope-union derivation) landing
  first.

---

## Quick-pick summary for a fresh session

| # | Item | Effort | Adds tool? | Care flags |
|---|------|--------|-----------|------------|
| 1 | Sheets `append_rows` ⭐ | S | yes (+golden, +floor) | idempotent=False (don't copy write_range); rebase if batch PR is open |
| 2 | Sheets `clear_range` | S | yes (+golden, +floor) | file-overlap w/ batch PR only |
| 3 | Drive `create_folder` | S | yes (+golden, +floor) | none (zero overlap) |
| 4 | Drive `revoke_permission` | S | yes (+golden, +floor) | lives in sharing.py, not api.py |
| 5 | Drive generalized search | M | no* (golden zero-diff) | *or new tool by choice; params optional |
| 6 | Sheets RAW value-input | S | no (golden zero-diff) | edits a pinned invariant test; fold into #1 |
| 7 | Scope-union derivation | M | no (golden zero-diff) | **needs-care (consent)**; needs-design (naming + restricted tier) |
| 8 | Apps Script event triggers | M | yes ×2 (+golden, +floor) | needs-design (Forms lift + tool-vs-recipe); +_APPS_SCRIPT_TOOL_MODULE |
| 9 | Slides createSlide+insertText | M | yes ×2 (+golden, +floor) | own batchUpdate (NOT the sheets one) |
| 10 | Coverage `fail_under` backstop | S | no (golden zero-diff) | confirm local cov ≥55 first |

**Start with #1 (`gsheets_append_rows`).** Smallest blast radius; the nearest
precedent (`write_range`) sits one function above it in the same file; it adds a
clean tool so it walks the full add-a-tool ritual (`_expected_tools.py` +
`test_tool_schemas.py` + re-freeze + floor bump); and it's API-independent of
BOTH in-flight items because it's a `values.append` call, not a
`spreadsheets.batchUpdate` one. The only correctness nuance is `idempotent=False`,
and the only coordination is rebasing onto the Sheets batch-builder PR if that's
already open (file overlap only). **If you'd rather avoid even that file overlap,
#3 (`gdocs_create_folder`) is the cleanest fully-isolated alternative** (Drive
folder; zero overlap with either in-flight effort). Then chain #2/#3/#4 for fast
S wins, and take #7 (leverage, with care) or #9 (clean M) next.
