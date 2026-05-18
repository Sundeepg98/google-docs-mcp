#!/usr/bin/env bash
# Deploy to Fly with provenance baked into the image.
#
# Wraps `flyctl deploy` to pass GIT_COMMIT + BUILD_TIME as Docker
# build args, which the Dockerfile turns into env vars that the
# server exposes via gdocs_server_info. Without this wrapper those
# fields default to "unknown".
#
# Also writes test-results.json (via pytest --json-report) so
# gdocs_server_info's test_suite block reflects what was actually
# tested when the image was built — not the most recent local run.
#
# Pass through any extra flyctl deploy flags as args, e.g.:
#   ./deploy.sh                 # standard deploy
#   ./deploy.sh --strategy=immediate
set -euo pipefail

GIT_COMMIT=$(git rev-parse --short HEAD)
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Run unit tests first — fail fast before pushing a broken image.
# Also write test-results.json so gdocs_server_info can surface the
# build's CI status at runtime. Skip with SKIP_TESTS=1 ./deploy.sh
# if you really need to bypass (e.g. tests themselves are broken and
# you're hot-fixing) — in that case the test_suite block will report
# status="unknown" with the SKIP_TESTS reason.
if [ "${SKIP_TESTS:-0}" = "0" ]; then
  echo "running unit tests (set SKIP_TESTS=1 to bypass)..."
  python -m pytest tests/unit -q \
      --json-report --json-report-file=test-results.json || {
    echo ""
    echo "❌ unit tests FAILED — refusing to deploy."
    echo "   fix the tests OR run with SKIP_TESTS=1 to bypass."
    exit 1
  }
  echo "✅ unit tests passed."

  # Inject GIT_COMMIT into the results so the runtime can compare it
  # against the deployed commit — divergence means the image shipped
  # without a matching test run (which is itself a red flag).
  python -c "
import json
with open('test-results.json') as f:
    d = json.load(f)
d['_git_commit'] = '${GIT_COMMIT}'
with open('test-results.json', 'w') as f:
    json.dump(d, f)
"
else
  echo "⚠️  SKIP_TESTS=1 — writing stub test-results.json"
  cat > test-results.json <<EOF
{"_git_commit": "${GIT_COMMIT}", "_skipped_reason": "SKIP_TESTS=1 at build time", "summary": {}}
EOF
fi

echo "deploying GIT_COMMIT=${GIT_COMMIT} BUILD_TIME=${BUILD_TIME}"

flyctl deploy --remote-only \
  --build-arg "GIT_COMMIT=${GIT_COMMIT}" \
  --build-arg "BUILD_TIME=${BUILD_TIME}" \
  "$@"
