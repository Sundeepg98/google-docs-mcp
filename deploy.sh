#!/usr/bin/env bash
# Deploy to Fly with provenance baked into the image.
#
# Wraps `flyctl deploy` to pass GIT_COMMIT + BUILD_TIME as Docker
# build args, which the Dockerfile turns into env vars that the
# server exposes via gdocs_server_info. Without this wrapper those
# fields default to "unknown".
#
# Pass through any extra flyctl deploy flags as args, e.g.:
#   ./deploy.sh                 # standard deploy
#   ./deploy.sh --strategy=immediate
set -euo pipefail

GIT_COMMIT=$(git rev-parse --short HEAD)
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "deploying GIT_COMMIT=${GIT_COMMIT} BUILD_TIME=${BUILD_TIME}"

flyctl deploy --remote-only \
  --build-arg "GIT_COMMIT=${GIT_COMMIT}" \
  --build-arg "BUILD_TIME=${BUILD_TIME}" \
  "$@"
