FROM python:3.13-slim

# Minimal runtime image. Build the package, then install it. No dev
# tooling (uv etc.) shipped to the final image.
WORKDIR /app

# v2.0.4: install deps from the LOCKED uv.lock — not a fresh pip
# resolve from pyproject.toml. CI runs `uv sync --frozen` (see
# .github/workflows/e2e.yml); the Dockerfile must use the same dep
# graph or production deploys differ from what CI tested. R20 attack
# #4 mitigation — lockfile drift between PR/CI and prod is a supply-
# chain signal, NOT a build-system convenience to paper over.
#
# uv is copied from the official astral-sh image at a pinned tag.
# Once `uv sync --frozen --no-dev --no-editable` populates
# /app/.venv, the uv binary is no longer needed at runtime, but
# leaving it in /usr/local/bin/ adds negligible image size and lets
# operators `docker exec` for diagnostics (`uv pip list` etc.).
COPY --from=ghcr.io/astral-sh/uv:0.5.0 /uv /uvx /usr/local/bin/

# Order matters for layer caching: lockfile + manifest first (rarely
# change beyond dep-bump PRs), then dep install, THEN source. The two
# `uv sync` invocations split the work so a code-only edit reuses the
# heavy deps layer:
#
#   1. COPY pyproject.toml + uv.lock + README.md       (changes on
#      dep bumps only — buildx GHA cache keeps this layer warm)
#   2. uv sync --no-install-project --frozen --no-dev  (installs ONLY
#      third-party deps; CACHED unless step 1 invalidates)
#   3. COPY src                                         (changes every
#      app-code edit — busts everything below, but NOT step 2)
#   4. uv sync --no-editable --frozen --no-dev          (now installs
#      just our package into the existing venv; ~1-2s)
#
# v2.3.3: this split is what makes the new GitHub-Actions free
# builder + buildx GHA cache hit ~3-5 min on warm cache vs ~10 min
# cold. The pre-v2.3.3 version did a single `uv sync` AFTER
# `COPY src`, which forced the full dep resolve on every code edit.
COPY pyproject.toml uv.lock README.md ./

# --frozen: error if uv.lock and pyproject.toml drift (we want that).
# --no-dev: skip [project.optional-dependencies] test extras (pytest
#   etc. don't belong in the runtime image).
# --no-install-project: install dependencies into /app/.venv but do
#   NOT install the project itself yet (we don't have src/ on disk).
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src

# Same flags as above, with --no-editable so the project is built as
# a wheel and installed non-editably. The dep layer is reused; only
# the project install runs (fast, no network).
RUN uv sync --frozen --no-dev --no-editable

# Put the venv's bin dir on PATH so `google-docs-mcp` (the
# [project.scripts] entry point) resolves at CMD time without needing
# `uv run` wrapping. Matches the pre-v2.0.4 `pip install .` behavior.
ENV PATH="/app/.venv/bin:$PATH"

# Bake in deploy provenance. Both default to "unknown" so a vanilla
# `docker build` works for local testing without git context. The
# deploy.sh wrapper passes the real values via --build-arg.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}

# Bake the pytest results from this build into the image so
# gdocs_server_info's test_suite block reflects what was tested
# at build time. Written by deploy.sh; if absent (vanilla
# `docker build`), the runtime reports status="unknown".
# Glob trick: `test-results.jso[n]` matches `test-results.json`
# if present and silently skips otherwise — no build error when
# the file is missing.
COPY test-results.jso[n] /app/test-results.json
# Same glob trick for the mutation-check artifact — present in CI
# builds (uploaded by the mutation job, downloaded by the deploy
# job), absent for local `docker build` without going through CI.
COPY mutation-check.jso[n] /app/mutation-check.json

# Persistent data dir for OAuth token + Apps Script config.
# In production, mount a Fly Volume here so token.json survives restarts.
RUN mkdir -p /data/google-docs-mcp
ENV GOOGLE_DOCS_DATA_DIR=/data/google-docs-mcp

# HTTP transport mode
ENV MCP_TRANSPORT=http
ENV PORT=8080

EXPOSE 8080

# Health endpoint for Fly.io probes is at /health (unauthenticated)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)"

CMD ["google-docs-mcp"]
