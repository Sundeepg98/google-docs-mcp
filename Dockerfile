FROM python:3.13-slim

# Minimal runtime image. Build the package, then install it. No dev
# tooling (uv etc.) shipped to the final image.
WORKDIR /app

# Install package
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

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
