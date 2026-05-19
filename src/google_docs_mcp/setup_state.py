"""Persistent ledger for setup-apps-script-auto's multi-step pipeline.

Setup is a 4-step chain: create project → push files → create version
→ deploy webapp. Each step is a separate network call; any one can
fail. Without a ledger, a partial failure orphans an Apps Script
project in the user's Drive AND the next retry creates another one,
producing "ghost scripts" that need manual cleanup.

This module persists per-step results to a JSON file keyed by
(content_hash, impersonate_user). On retry:
  - If hash + impersonate match: resume from first incomplete step
  - If they differ: start fresh (different setup target = different state)
  - If saved script_id no longer exists in Drive (user deleted it
    manually): clear stale state and start fresh
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict


class SetupState(TypedDict, total=False):
    """Persisted state of a setup-apps-script-auto run.

    Fields populate as each step completes. The next run inspects which
    keys are present to determine where to resume.
    """
    content_hash: str          # SHA-256 of manifest + restructure.gs
    impersonate: str | None    # Workspace user email; None for OAuth path
    script_id: str             # After projects.create
    version_number: int        # After projects.versions.create
    deployment_id: str         # After projects.deployments.create
    url: str                   # /exec URL captured with the deployment


def state_path(data_dir: Path) -> Path:
    return data_dir / "setup-state.json"


def compute_content_hash(manifest: dict, files: dict[str, str]) -> str:
    """SHA-256 over manifest + (sorted) file name/contents.

    Stable across runs as long as the input hasn't changed. Used to
    detect "user updated restructure.gs and wants a fresh deploy" —
    in that case, we DON'T resume a stale partial state.
    """
    h = hashlib.sha256()
    h.update(json.dumps(manifest, sort_keys=True).encode("utf-8"))
    for name in sorted(files):
        h.update(b"\x00")
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(files[name].encode("utf-8"))
    return h.hexdigest()


def load_state(data_dir: Path) -> SetupState:
    p = state_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data_dir: Path, state: SetupState) -> None:
    """Atomic write via tmpfile + os.replace.

    The tmpfile MUST live in the same directory as the target — os.replace
    requires same-filesystem (cross-device fails on POSIX, raises OSError
    on Windows). Both .tmp and target share data_dir.

    Crash safety (v1.3.1 hardening): a SIGKILL between write and rename
    leaves the original file intact. The .tmp may linger but never
    corrupts the canonical state. Best-effort cleanup of the tmpfile
    happens on the failure path; orphan-tmp cleanup is acceptable
    since save_state is invoked rarely (only during apps-script setup).
    """
    p = state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # os.replace is atomic on POSIX; on Windows it overwrites the
        # destination atomically when both files share a volume.
        os.replace(str(tmp), str(p))
    except Exception:
        # Best-effort cleanup of partial tmpfile on failure path.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def clear_state(data_dir: Path) -> None:
    p = state_path(data_dir)
    if p.exists():
        p.unlink()


def state_matches_target(
    state: SetupState, content_hash: str, impersonate: str | None
) -> bool:
    """True if cached state is for the SAME setup target.

    Mismatch = content changed (restructure.gs edited) or different
    impersonate user (Workspace SA + DWD targeting a different account).
    In either case, the cached script_id refers to a different project
    than what we're trying to deploy now → start fresh.
    """
    return (
        state.get("content_hash") == content_hash
        and state.get("impersonate") == impersonate
    )
