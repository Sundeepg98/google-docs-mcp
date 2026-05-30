#!/bin/sh
# Container entrypoint — fixes volume ownership, then supervises
# litestream + google-docs-mcp as the unprivileged ``app`` user.
#
# ====================================================================
# PR-Δ-volfix: runtime /data ownership reconciliation (root -> app).
# ====================================================================
#
# THE BUG THIS FIXES (critical prod incident, SQLITE_READONLY):
#   The server process runs as the non-root ``app`` user (uid 10001,
#   added in PR #127). But the persistent Fly Volume mounted at /data
#   still contained files OWNED BY ROOT — created back when the server
#   ran as root (pre-#127). The Dockerfile's ``chown -R app:app /data``
#   runs at BUILD time against the IMAGE's (empty) /data; at runtime the
#   volume is mounted OVER that path, shadowing the image's /data, so
#   the build-time chown never touched the volume's real contents.
#
#   Result: ``app`` couldn't write the root-owned
#   /data/google-docs-mcp/user_state.db NOR create the WAL sidecar files
#   (-wal/-shm) in the root-owned directory. EVERY tool that reads or
#   writes user state hit ``sqlite3.OperationalError: attempt to write a
#   readonly database`` at request initiation — the apps_script tools
#   aborted before making a single Apps Script API call. (The
#   /data/fastmcp OAuth store written fine because it was a NEW dir the
#   ``app`` process itself created post-#127.)
#
# THE FIX:
#   The container now starts as ROOT (the Dockerfile no longer sets
#   ``USER app``). This entrypoint, running as root, recursively chowns
#   /data to app:app — idempotent and fast (a handful of small files) —
#   so the volume's pre-existing root-owned files become writable by the
#   runtime user. It then verifies /data is actually writable as ``app``
#   and FAILS LOUD if not (rather than booting into the same silent
#   readonly-DB failure). Finally it drops privileges to uid/gid 10001
#   via ``setpriv`` (util-linux, already in the slim image — no extra
#   dependency like gosu/su-exec) and ``exec``s the workload, so the
#   server NEVER runs as root.
#
#   This self-heals on every boot, so it also covers any future file a
#   stray root context might leave on the volume.
#
# ====================================================================
# PR-Δ4: stub-but-wired litestream supervision (unchanged behavior).
# ====================================================================
#   - If LITESTREAM_BUCKET is set → run google-docs-mcp under
#     `litestream replicate -exec`, so litestream takes a final
#     WAL checkpoint on exit + replicates to the operator's S3-
#     compatible bucket (Cloudflare R2 recommended; see
#     docs/runbooks/backup-restore.md for operator activation).
#   - If LITESTREAM_BUCKET is NOT set → fall through to plain
#     `google-docs-mcp`, no replication.
#
# Why sh (not bash): the runtime image is python:3.13-slim which
# ships busybox-style /bin/sh, not bash. POSIX sh covers everything
# below without bash-specific syntax.
#
# Why exec (not background): exec REPLACES this shell process with the
# supervised process so signals from Fly (SIGTERM on deploy rollover)
# reach the right PID. setpriv likewise execs (no fork), so the signal
# path stays intact: Fly -> PID 1 (litestream or the server).

set -e

APP_UID=10001
APP_GID=10001
DATA_DIR=/data

# ----------------------------------------------------------------------
# Stage 1 (root only): reconcile /data ownership, then drop privileges.
#
# If we're NOT root (e.g. a local `docker run --user` or a platform that
# starts us unprivileged), we can't chown and there's nothing to drop —
# skip straight to the workload and let it run as whoever we are. The
# in-process startup writability check (user_store) still guards
# correctness in that case.
# ----------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
  # Idempotent: a no-op once the volume is already app-owned (steady
  # state), cheap even when it isn't (few small files). `|| true` is
  # deliberately NOT used — if chown itself errors we want to fall into
  # the writability check below and fail loud with a clear message.
  echo "entrypoint: reconciling ${DATA_DIR} ownership -> ${APP_UID}:${APP_GID} (root stage)"
  chown -R "${APP_UID}:${APP_GID}" "${DATA_DIR}" 2>/dev/null || \
    echo "entrypoint: WARNING chown -R ${DATA_DIR} returned non-zero; verifying writability anyway"

  # Verify the runtime user can ACTUALLY write /data before we boot the
  # server into the very failure mode we're fixing. setpriv drops to the
  # app uid/gid and clears supplementary groups (same as the running
  # server will get). If this can't write, refuse to start.
  if ! setpriv --reuid="${APP_UID}" --regid="${APP_GID}" --clear-groups \
        sh -c "touch ${DATA_DIR}/.write_probe && rm -f ${DATA_DIR}/.write_probe"; then
    echo "entrypoint: FATAL — ${DATA_DIR} is not writable by uid ${APP_UID} after chown." >&2
    echo "  The persistent volume's files could not be made owner-writable by the" >&2
    echo "  runtime user. Booting would reproduce the SQLITE_READONLY failure that" >&2
    echo "  takes the whole tool surface offline, so we refuse to start." >&2
    echo "  Investigate: 'fly ssh console' then 'ls -lan ${DATA_DIR}' and check the" >&2
    echo "  mount is rw and not full ('df -h ${DATA_DIR}', 'grep ${DATA_DIR} /proc/mounts')." >&2
    exit 1
  fi
  echo "entrypoint: ${DATA_DIR} confirmed writable by uid ${APP_UID}"

  # Re-exec THIS script with privileges dropped. From here on $(id -u)
  # is the app uid, so the branch below runs the workload directly. The
  # double-pass keeps the litestream/plain logic in exactly one place.
  exec setpriv --reuid="${APP_UID}" --regid="${APP_GID}" --clear-groups "$0" "$@"
fi

# ----------------------------------------------------------------------
# Stage 2 (unprivileged: app user, post-drop OR already-non-root): run.
# ----------------------------------------------------------------------
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
