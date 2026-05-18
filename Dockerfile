FROM python:3.13-slim

# Minimal runtime image. Build the package, then install it. No dev
# tooling (uv etc.) shipped to the final image.
WORKDIR /app

# Install package
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

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
