#!/usr/bin/env bash
# Deploy to Fly with provenance baked into the image.
#
# Wraps `flyctl deploy` to pass GIT_COMMIT + BUILD_TIME as Docker
# build args, which the Dockerfile turns into env vars that the
# server exposes via gdocs_server_info. Without this wrapper those
# fields default to "unknown".
#
# Also writes test-results.json (via pytest --json-report) with:
#   - _git_commit  : the commit the suite ran against
#   - _ci_run_url  : link to the GitHub Actions run for this commit
#                    (best-effort via `gh` CLI; "" if no run found)
#   - _meta.digest : sha256 of the canonicalized payload (minus _meta)
# so gdocs_server_info's test_suite block can: (a) prove which CI run
# produced the results, and (b) detect post-build tampering with the
# numbers (server recomputes the digest at read time and reports
# status="tampered" on mismatch).
#
# Pass through any extra flyctl deploy flags as args, e.g.:
#   ./deploy.sh                 # standard deploy
#   ./deploy.sh --strategy=immediate
set -euo pipefail

GIT_COMMIT=$(git rev-parse --short HEAD)
GIT_COMMIT_FULL=$(git rev-parse HEAD)
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Best-effort: query GitHub Actions for the most recent run on this
# commit. If gh isn't installed, no run exists yet, or the user isn't
# authenticated, fall back to empty string — gdocs_server_info will
# report ci_run_url="" which the caller can interpret.
CI_RUN_URL=""
if command -v gh >/dev/null 2>&1; then
  CI_RUN_URL=$(gh run list --commit="${GIT_COMMIT_FULL}" --limit=1 \
      --json url --jq '.[0].url // ""' 2>/dev/null || echo "")
fi
if [ -z "${CI_RUN_URL}" ]; then
  echo "⚠️  no CI run found yet for commit ${GIT_COMMIT_FULL} —"
  echo "    ci_run_url will be empty. Re-deploy after CI completes,"
  echo "    or just push first and let CI catch up."
fi

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
else
  echo "⚠️  SKIP_TESTS=1 — writing stub test-results.json"
  cat > test-results.json <<EOF
{"_skipped_reason": "SKIP_TESTS=1 at build time", "summary": {}}
EOF
fi

# Inject provenance + digest. The digest is computed AFTER the
# provenance fields are added (so the digest covers the as-deployed
# state) but BEFORE _meta itself is added (chicken-and-egg). Server
# re-canonicalizes the same way at read time.
python -c "
import hashlib, json
with open('test-results.json') as f:
    d = json.load(f)
d['_git_commit'] = '${GIT_COMMIT}'
d['_ci_run_url'] = '${CI_RUN_URL}'
# Drop any prior _meta before computing the digest.
d.pop('_meta', None)
canon = json.dumps(d, sort_keys=True, separators=(',', ':'))
digest = 'sha256:' + hashlib.sha256(canon.encode('utf-8')).hexdigest()
d['_meta'] = {'digest': digest}
with open('test-results.json', 'w') as f:
    json.dump(d, f)
print(f'  digest: {digest}')
print(f'  ci_run_url: {\"${CI_RUN_URL}\" or \"(none)\"}')
"

echo "deploying GIT_COMMIT=${GIT_COMMIT} BUILD_TIME=${BUILD_TIME}"

flyctl deploy --remote-only \
  --build-arg "GIT_COMMIT=${GIT_COMMIT}" \
  --build-arg "BUILD_TIME=${BUILD_TIME}" \
  "$@"
