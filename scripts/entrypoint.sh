#!/bin/sh
# Container entrypoint — supervises litestream + google-docs-mcp.
#
# PR-Δ4: stub-but-wired litestream supervision.
#
# Behavior:
#   - If LITESTREAM_BUCKET is set → run google-docs-mcp under
#     `litestream replicate -exec`, so litestream takes a final
#     WAL checkpoint on exit + replicates to the operator's S3-
#     compatible bucket (Cloudflare R2 recommended; see
#     docs/runbooks/backup-restore.md for operator activation).
#   - If LITESTREAM_BUCKET is NOT set → fall through to plain
#     `google-docs-mcp`, no replication. The container starts
#     normally for operators who haven't enabled DR yet (local
#     dev, fresh deploys before the R2 secrets are wired).
#
# Why sh (not bash): the runtime image is python:3.13-slim which
# ships busybox-style /bin/sh, not bash. POSIX sh covers the case
# distinction below without needing bash-specific syntax.
#
# Why exec (not background): exec REPLACES this shell process with
# the supervised process so signals from Fly (SIGTERM on deploy
# rollover) reach the right PID. A backgrounded litestream would
# leave the shell as PID 1, swallowing signals and breaking
# graceful shutdown.

set -e

if [ -n "${LITESTREAM_BUCKET:-}" ]; then
  echo "entrypoint: LITESTREAM_BUCKET=${LITESTREAM_BUCKET} — replicating /data via litestream"
  exec litestream replicate \
    -config /etc/litestream.yml \
    -exec "google-docs-mcp"
else
  echo "entrypoint: LITESTREAM_BUCKET unset — running google-docs-mcp without backup replication"
  echo "  (operator activation: see docs/runbooks/backup-restore.md)"
  exec google-docs-mcp
fi
