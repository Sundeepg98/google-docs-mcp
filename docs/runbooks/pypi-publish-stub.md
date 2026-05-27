# Runbook: Publish the `appscriptly` PyPI stub (squat-protection)

**Status**: Operator action. PR-Δ5.5 renames the package on the metadata surface; this runbook walks through the actual PyPI upload that reserves the name.
**Why now**: claim `appscriptly==0.0.1` on PyPI before someone else publishes a confusingly-named package under the same name. Squat-protection only; real distribution still happens under the prior `google-docs-mcp` PyPI name until the operator decides to flip publishing fully (separate later runbook).
**Cost**: $0 (PyPI is free for open-source projects).
**Time**: ~10 minutes one-shot; ~2 minutes for re-publishes if you ever bump.

## Prerequisites

- PyPI account (https://pypi.org/account/register/) — free, takes 2 minutes.
- `uv` installed locally (already a dev dependency of this repo).
- A clean checkout of this repo at the commit you want to publish from.

## One-time setup: PyPI account + API token

### 1. Create the PyPI account

Visit https://pypi.org/account/register/ — pick a username, verify your email, enable 2FA (PyPI requires it for new uploads). The username doesn't matter for the package name (we're publishing as `appscriptly`, which is the package name, not the publisher username).

### 2. Generate a project-scoped API token

After your first successful upload (step 5 below), you can scope a token to just the `appscriptly` project. For the FIRST upload, you need an account-scoped token:

1. https://pypi.org/manage/account/token/
2. **Token name**: `appscriptly-first-upload`
3. **Scope**: "Entire account (all projects)" — required for the first upload because the project doesn't exist yet.
4. Click "Add token", **copy the `pypi-...` value immediately** (it's only shown once).

After the first upload succeeds, generate a project-scoped token and revoke the account-scoped one:

1. https://pypi.org/manage/account/token/
2. **Token name**: `appscriptly-ci` or `appscriptly-publisher`
3. **Scope**: select "Project: appscriptly" from the dropdown.
4. Replace the account-scoped token in any CI / env vars.
5. Revoke the account-scoped token.

### 3. Save the token

For local manual uploads, save the token in a place `uv publish` can find it. The two supported mechanisms:

```bash
# Option A: env var (preferred for one-shot uploads)
export UV_PUBLISH_TOKEN="pypi-..."

# Option B: keyring (preferred for long-term local use)
# uv reads from any keyring-compatible store via the standard
# Python `keyring` package. Set up once:
pip install --user keyring
keyring set https://upload.pypi.org/legacy/ __token__
# (Paste the pypi-... token at the prompt; uv picks it up.)
```

## Publish flow (run this from a clean checkout)

### 1. Build the distribution

```bash
cd /path/to/the/clean/checkout
# Make sure the working tree is clean — the sdist captures everything
# tracked by git plus everything in src/, so uncommitted edits would
# leak into the published artifact.
git status --porcelain   # should be empty

# Build wheel + sdist
uv build
# Outputs:
#   dist/appscriptly-1.5.1-py3-none-any.whl
#   dist/appscriptly-1.5.1.tar.gz
```

### 2. Verify the artifacts before upload

```bash
# Inspect the wheel's metadata to confirm the rename took effect
unzip -p dist/appscriptly-1.5.1-py3-none-any.whl appscriptly-1.5.1.dist-info/METADATA | head -20
# Expected:
#   Name: appscriptly
#   Version: 1.5.1
#   Summary: MCP server for Apps Script automation across Google Workspace...
# If you see "Name: google-docs-mcp" the rename in pyproject.toml didn't
# land — abort and investigate before publishing.

# Sanity-check the wheel installs locally into a throwaway venv
uv venv /tmp/appscriptly-publish-check
/tmp/appscriptly-publish-check/bin/python -m pip install dist/appscriptly-1.5.1-py3-none-any.whl
/tmp/appscriptly-publish-check/bin/appscriptly --help 2>/dev/null || echo "CLI bin found"
rm -rf /tmp/appscriptly-publish-check
```

### 3. Test-publish to TestPyPI first (optional but recommended for the FIRST upload)

TestPyPI is a separate instance that lets you smoke-test the upload + install round-trip without committing to a permanent claim. Useful for the very first publish to confirm the metadata + project name look right.

```bash
# Get a TestPyPI token first: https://test.pypi.org/manage/account/token/
# (Separate account from production PyPI — register at https://test.pypi.org/account/register/)
export UV_PUBLISH_TOKEN="pypi-TEST-..."
uv publish --publish-url https://test.pypi.org/legacy/ dist/*

# Verify the test install works
uv venv /tmp/testpypi-check
/tmp/testpypi-check/bin/pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  appscriptly==1.5.1
/tmp/testpypi-check/bin/appscriptly --help 2>/dev/null && echo "OK"
rm -rf /tmp/testpypi-check
```

### 4. Publish to production PyPI

```bash
export UV_PUBLISH_TOKEN="pypi-..."   # the real PyPI token from step 2
uv publish dist/*
```

`uv publish` uploads both `.whl` and `.tar.gz` from `dist/`. On success it prints:

```
Publishing to https://upload.pypi.org/legacy/
Uploading appscriptly-1.5.1.tar.gz (... KiB)
Uploading appscriptly-1.5.1-py3-none-any.whl (... KiB)
```

### 5. Verify the publish landed

```bash
# Wait ~30s for PyPI's CDN to propagate
sleep 30

# Open the project page in your browser:
open https://pypi.org/project/appscriptly/  # macOS
# OR: xdg-open https://pypi.org/project/appscriptly/  # Linux
# OR: explorer https://pypi.org/project/appscriptly/  # Windows

# Smoke-test the install from PyPI proper:
uv venv /tmp/pypi-prod-check
/tmp/pypi-prod-check/bin/pip install appscriptly==1.5.1
/tmp/pypi-prod-check/bin/appscriptly --help 2>/dev/null && echo "PUBLISHED OK"
rm -rf /tmp/pypi-prod-check
```

### 6. Tighten the token scope

After step 5 confirms the upload landed:

1. https://pypi.org/manage/account/token/
2. Generate a NEW token scoped to "Project: appscriptly".
3. Update your local env / keyring / CI to use the new token.
4. **Revoke** the account-scoped token from step 2 of the one-time setup.

This blast-radius reduction is per PR-Δ2's security posture — every long-lived credential should be scoped to the narrowest possible surface.

## Subsequent re-publishes (version bumps)

```bash
# 1. Bump version in pyproject.toml + src/google_docs_mcp/__init__.py (must match)
# 2. uv lock (regenerates uv.lock with the new version)
# 3. Commit + push + merge to main as usual
# 4. From a clean checkout at the merged commit:
git checkout main && git pull
uv build
uv publish dist/*
# 5. Tag the release in git for traceability:
git tag v1.5.2
git push --tags
```

When the operator decides to retire the legacy `google-docs-mcp` PyPI name (separate decision, separate runbook), that involves publishing a final `google-docs-mcp` release with a deprecation notice + a hard pin on `appscriptly` to forward existing users. Out of scope for this stub-publish runbook.

## Naming considerations

PyPI normalizes project names per [PEP 503](https://peps.python.org/pep-0503/#normalized-names): all comparisons are case-insensitive and treat `-`, `_`, and `.` as equivalent. So `appscriptly`, `Appscriptly`, `APPSCRIPTLY` all resolve to the same project. The pyproject `name = "appscriptly"` is the canonical lowercase spelling.

## Related

- ADR: `docs/adr/2026-05-27-rename-to-appscriptly.md`
- `pyproject.toml` — the `[project]` block + `[project.scripts]` aliases
- Companion runbooks: `docs/runbooks/backup-restore.md`, `docs/runbooks/gcp-project-linking.md`, `docs/runbooks/key-rotation.md`, `docs/runbooks/sentry-setup.md`
