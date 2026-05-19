# Compatibility Policy — google-docs-mcp

**Audience:** consumers integrating with the MCP, operators running self-hosted deployments, contributors evaluating PRs that could affect existing users.

This document is the authoritative source for what we promise about backward-compatibility, deprecation, and end-of-life. Other docs (CHANGELOG, TOOL_CONTRACT, RUNBOOK) defer to this one for policy questions.

## 1. Versioning

We follow [Semantic Versioning 2.0.0](https://semver.org/) strictly.

| Change kind | Triggers a... | Examples |
|---|---|---|
| Removing or renaming a tool | MAJOR (`X.0.0`) | drop `gdocs_trash_file` from the surface; rename `gdocs_make_tabbed_doc` to `gdocs_create_tabs` |
| Removing or renaming a tool argument | MAJOR | drop `tab_ids` from `gdocs_replace_all_text`; rename `docx_path` to `source_path` |
| Tightening an existing argument's accepted shape | MAJOR | reject titles >256 chars where 1024 used to work; require `tabs: list` where `tabs: dict` used to be accepted |
| Changing the success-return shape | MAJOR | rename a field; change `tabs: list[dict]` → `tabs: dict[str, dict]`; remove a previously-returned field |
| Switching auth model on an endpoint | MAJOR | require Google OAuth where bearer used to suffice; rotate the HKDF context strings (v2.0b strict-flip) |
| Adding a new tool | MINOR (`X.Y.0`) | ship `gdocs_help`; add `gdocs_admin_audit` |
| Adding an optional argument with a backwards-compatible default | MINOR | add `full: bool = False` to `gdocs_reset_authorization` |
| Adding a new optional success-return field | MINOR | add `latency_ms` to `gdocs_server_info()` |
| Loosening an argument's accepted shape | MINOR | now accept `file_id: str | list[str]` where previously only `str` worked |
| Bug fix with no surface change | PATCH (`X.Y.Z`) | fix a wrong-error-message; correct a soft-failure that should've been hard-fatal |
| Internal refactor with no surface change | PATCH | extract a helper; tighten a type annotation |
| Doc-only change | PATCH | this file; CHANGELOG entries; README updates |
| Dependency floor bump (non-breaking transitive) | PATCH | `cryptography ≥ 46.0.7`; `pyjwt ≥ 2.12.0` |
| Security patch backporting to a supported branch | PATCH on the patched branch | v1.3.1 hotfix on top of v1.3.0 |

Pre-1.0 releases (`0.x.y`) gave no compatibility promise. Everything from v1.0.0 forward is bound by this policy.

## 2. v1.x end-of-life

When v2.0.0 ships, the v1.x line enters a 6-month security-only window. Calculation: take the v2.0.0 tag's commit date, add 6 months, that's the v1.x EOL date. Document it in the v2.0.0 CHANGELOG entry.

**During the window (6 months):**
- Security-relevant fixes are backported to a `v1.x` maintenance line on demand. "Security-relevant" = anything that warrants a CVE, anything that exposes user data, anything that lets an unauthenticated request reach a privileged code path.
- Non-security bug fixes are NOT backported. If your deploy hits a non-security bug on v1.x during the window, the answer is "upgrade to v2.x."
- No new features. No new tools. No tool-argument additions. The surface is frozen.
- Backports ship as new `v1.x.y` PATCH releases. They tag, they CHANGELOG, they push to PyPI / Docker Hub like any other release.

**After the window:**
- No further updates of any kind. The `v1.x` branch is archived (made read-only in GitHub Settings).
- The README for the v1.x branch gets a top-of-file banner: "v1.x reached end-of-life on YYYY-MM-DD; please migrate to v2.x. See `docs/MIGRATION_v1_to_v2.md`."
- The `v1.x-eol` git tag is placed on the last v1.x.y commit.

We do not commit to specific calendar dates ahead of v2.0.0 shipping — the date is "6 months after v2.0.0's tag date" and gets pinned in the v2.0.0 CHANGELOG.

## 3. Deprecated tool policy

A tool can be MARKED deprecated in a MINOR release and REMOVED in a later MAJOR release. The minimum gap is **≥2 minor releases between the DEPRECATED marker and the removal release.**

Worked example:
- v2.3.0 — `gdocs_foo` description gains a leading `DEPRECATED: use gdocs_bar instead.` Call sites still work; soft-failures unchanged; no new error path.
- v2.4.0 — same; tool still callable.
- v2.5.0 — same; tool still callable.
- v3.0.0 — tool is REMOVED. Calls return `ToolError("tool 'gdocs_foo' was removed in v3.0.0; use 'gdocs_bar' instead")`.

That's the minimum cycle. Longer is fine; shorter is not.

**Currently deprecated tools (as of v2.0.2):** none. The v2.0.1-cleanup PR (#37) walked back the previously-planned `gdocs_update_tabs` and `gdocs_set_trashed` superseders, so `gdocs_rename_tab`, `gdocs_set_tab_icons`, `gdocs_trash_file`, and `gdocs_untrash_file` are first-class with no successor and not deprecating.

**The DEPRECATED marker convention:** the tool's docstring (and therefore the MCP tool-description payload) starts with `DEPRECATED: <reason>. Use <replacement> instead.` exactly. LLM tool-routers should treat the literal `DEPRECATED:` prefix as a signal to prefer the replacement when both are available.

## 4. Backward-compat for tool callers

This section is about what consumer code MUST keep working across compatible version bumps.

### v1.x stdio clients against a v2.x server

A v1.x stdio config in Claude Desktop / Claude Code that points at the `google-docs-mcp` entry-point continues to work against a v2.x install IF AND ONLY IF the consumer pinned tool names match what v2.x exposes. Since v2.x to date has only ADDED tools (gdocs_help in v2.2b; gdocs_admin_audit planned for v2.3) and has not removed any, every v1.x consumer continues to work without changes. The JSON-RPC envelope, the tool-name surface, the soft-failure dict shape, the hard-fatal `ToolError` shape — all stable across v1 → v2.

### What MIGHT break across major versions

- **Auth model changes.** v2.0b ships HKDF strict-flip: in-flight signed URLs and OAuth state tokens minted under v1.x become invalid the moment the new master derivation activates. Affected operators must time the cutover (see `docs/MIGRATION_v1_to_v2.md` § 2 + § 3, and `docs/RUNBOOK.md` § 3.5 / § 3.6).
- **Removed tools.** If a tool is deprecated then removed per § 3 above, consumers calling it get the `ToolError` shape. v1 → v2: no tools removed (auth-auditor confirmed).
- **Tightened argument validation.** A MAJOR bump might reject inputs that previously sneaked through. If your consumer relied on lax validation (e.g., titles with unusual control chars), check the v2 CHANGELOG under `### Changed`.
- **Soft-failure → hard-fatal promotion.** If a case that used to return a soft-failure dict now raises `ToolError`, that's a MAJOR change — consumers that branched on `result.get("reason")` need to also handle the exception.

### What never breaks within a MAJOR line

- Adding a new optional argument (with backwards-compatible default) does not affect existing callers.
- Adding a new return field does not affect existing callers (the consumer code's JSON parser ignores unknown keys).
- A PATCH release will never change the tool surface — the only valid PATCH-level changes are bug fixes, internal refactors, doc updates, and dependency-floor bumps.

### Operators control update timing

Nothing in this project auto-updates an operator's deploy. Operators control when to upgrade. The PyPI / Docker Hub / GitHub Releases artifacts are all explicit pulls. If an operator is on v1.3.1 and never deploys v1.3.2, they stay on v1.3.1 — and during the v1.x security-only window, we will still backport CVE fixes for them.

## 5. Branching strategy

Trunk-based. **`main` IS the release line.** Every commit on `main` is a candidate for a tagged release.

- No long-lived `release/*` branches. No `develop` branch. No `next` branch.
- Feature work happens on short-lived PR branches (`<version>-<slug>` for releases-in-progress, e.g. `v2.0b-hkdf-strict`; `<topic>-<slug>` for cross-cutting work like `docs-readme-freshness`).
- Tag on substantive change. A PR that ships a new tool, fixes a CVE, or completes a planned milestone gets a tag at merge. A PR that does cleanup, doc polish, or batched dependency bumps does not need its own tag — the next substantive release picks them up.
- The `v1.x` maintenance line (post-v2.0.0) is the ONE exception to "no release branches." It exists for security backports during the EOL window and is archived afterward.

PR cleanups (worktree removal, branch deletion) happen at merge time. We do not let merged branches accumulate. See CONTRIBUTING.md for the PR-flow steps.

## 6. Cadence

**There is no release cadence.** We ship when something is ready to ship.

- No weekly / monthly / quarterly release cycle.
- No "release train" where features get held back to land on the next bus.
- A bug fix can land within an hour of being noticed. A multi-PR feature can take weeks. Neither is unusual.
- Operators who want predictability subscribe to GitHub Releases notifications and pin their deploy to a specific tag.
- We do not pre-announce dates. The v2.0.0 ship date is "when the v2.0 milestone is closed and the preflight passes" — not "Q3 2026."

This is deliberate. We're an MIT-licensed open-source project; the people doing the work are the same people deciding when it's done. Calendar-bound cadence is a commercial-support concern, and we don't offer commercial support.

If an operator NEEDS a specific calendar commitment for a specific change, the answer is: vendor the relevant commit yourself, OR fork the project, OR sponsor the work on GitHub Sponsors so it gets prioritized. The repo is MIT — all three are explicitly fine.
