# PR-Δ4 (2026-05-27): Litestream binary fetch stage.
# Litestream is a single static Go binary; we copy it from the
# official image rather than installing via curl/apt to keep
# provenance + bytes-pinned. The whole image is ~25 MB but we only
# COPY the `/usr/local/bin/litestream` binary into the runtime layer
# (~15 MB), so the multi-stage approach adds essentially nothing to
# the final image size compared to fetching+extracting via curl.
#
# Pin to v0.3.13 (current stable as of 2026-05-22) for reproducible
# builds. Dependabot's `docker` ecosystem tracks bumps.
FROM litestream/litestream:0.3.13 AS litestream-binary

# PR-Δ3 (2026-05-27): SHA-pin the base image digest.
# Rationale: an unpinned ``python:3.13-slim`` lets the registry serve a
# different upstream layer set at build time without any change in this
# file — a supply-chain MITM, an upstream rebuild, or a Docker Hub
# repository swap would all silently land in our image. Pinning to the
# manifest digest makes the dependency content-addressable: identical
# bytes every build, until we explicitly bump the digest.
#
# Bumped via dependabot's ``docker`` ecosystem (see .github/dependabot.yml).
# The digest below was the current ``3.13-slim`` tag as of 2026-05-22
# (Docker Hub manifest lookup).
FROM python:3.13-slim@sha256:b04b5d7233d2ad9c379e22ea8927cd1378cd15c60d4ef876c065b25ea8fb3bf3

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

# PR-Δ3 (2026-05-27): drop root.
# Create an unprivileged ``app`` user (uid 10001) and switch to it for
# the runtime. The application never needs to write outside ``/data``
# or read outside ``/app`` + the python stdlib, so root inside the
# container is gratuitous attack surface (escape-via-shared-kernel,
# accidental writes to /etc, etc.).
#
# uid 10001 is high enough to avoid colliding with anything in
# Debian's reserved 0–999 range, and matches the ``app``-user
# convention used by the Distroless and Chainguard images.
#
# ``chown`` /app + /data so the venv and the persistent OAuth/Apps
# Script state are owned by the runtime user — Fly Volumes mounted
# at /data will already be owned by uid 10001 on subsequent boots
# (Fly preserves volume ownership across deploys).
#
# ``--create-home`` + ``--shell /sbin/nologin``: this user is strictly
# a runtime UID, never an interactive shell account, BUT it still
# needs a real ``/home/app`` because Python's standard library writes
# to ``$HOME`` during normal startup (pathlib, importlib caches,
# etc.). PR-Δ3 (PR #127) shipped ``--no-create-home``; that crashed
# production every restart with
# ``PermissionError: [Errno 13] '/home/app'`` because the home dir
# didn't exist and ``$HOME`` defaults there for the app user.
# Hotfixed in PR-Δ3-hotfix to ``--create-home`` — writability is what
# matters, not the interactive-shell affordance (``/sbin/nologin``
# still blocks login).
# PR-Δ4 (2026-05-27): Litestream binary + config + entrypoint.
#
# Copies the static litestream binary from the multi-stage builder
# above (~15 MB), the repo's litestream.yml config, and a tiny shell
# entrypoint that supervises BOTH processes:
#
#   1. ``litestream replicate -exec "google-docs-mcp"`` runs the
#      server as a child of litestream so litestream can:
#        - take a final WAL checkpoint when the server exits
#        - exit with the same code as the server
#        - propagate signals (SIGTERM from Fly's deploy/restart) so
#          shutdown is graceful and the last replica is up to date
#
# The replicate path is GATED on ``LITESTREAM_BUCKET`` being set —
# the entrypoint script falls through to plain ``google-docs-mcp``
# when the env is unset (the stub-but-wired pattern: code is in
# place, config is committed, operator activates by setting the
# Fly secrets per docs/runbooks/backup-restore.md).
COPY --from=litestream-binary /usr/local/bin/litestream /usr/local/bin/litestream
COPY litestream.yml /etc/litestream.yml
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN useradd --uid 10001 --user-group --create-home --shell /sbin/nologin app \
    && chown -R app:app /app /data
USER app

CMD ["/usr/local/bin/entrypoint.sh"]
