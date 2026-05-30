"""Env-var-driven StorageBackend factory.

PR-Δ6 (Vercel pilot) introduced this seam to let the same
codebase pick its backend at runtime based on deploy target:

  - Fly (default): ``SqliteBackend`` over the /data volume —
    behavior identical to pre-PR-Δ6.

  - Vercel: ``VercelKvBackend`` over the project-bound Upstash KV.

  - Tests: callers continue to pass ``InMemoryBackend()`` directly
    via ``with_backend(...)``. The selector is not consulted by tests.

Selection rule:

  ``STORAGE_BACKEND`` env var:

    | Value         | Backend            | Notes                          |
    |---------------|--------------------|--------------------------------|
    | unset         | SqliteBackend      | Default. Pre-PR-Δ6 behavior.   |
    | ``sqlite``    | SqliteBackend      | Explicit. Same as unset.       |
    | ``vercel_kv`` | VercelKvBackend    | Requires KV_REST_API_URL +     |
    |               |                    | KV_REST_API_TOKEN env vars.    |
    | anything else | SqliteBackend      | + WARNING log explaining the   |
    |               |                    | fallback. Fail-soft so a typo  |
    |               |                    | (``STORAGE_BACKEND=vercelkv``) |
    |               |                    | doesn't 500 the deploy.        |

Fail-soft rationale: on Vercel, ``VercelKvBackend`` construction
itself raises ``RuntimeError`` if KV_REST_API_URL / TOKEN are unset.
We catch that here, log loudly, and fall back to SqliteBackend with
a WARNING — better than the entire serverless function 500ing on
import. The operator sees the warning in Vercel function logs and
fixes the missing KV binding. The SqliteBackend fallback won't
persist anything across cold starts (Vercel's tmpfs dies with the
container), but every request still returns; the user just sees
"please re-authorize" responses until the operator wires KV
correctly. That's an acceptable transient-failure mode for an
opt-in pilot.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("appscriptly.storage.selector")


def select_backend() -> Any:
    """Resolve the operator-selected StorageBackend.

    Reads ``STORAGE_BACKEND`` env var. Returns an INSTANCE (not a
    class) so the caller can ``user_store.set_backend(select_backend())``
    directly.

    Returns:
        A live StorageBackend instance.

    Defensive behavior:
      - Unknown ``STORAGE_BACKEND`` value → SqliteBackend + WARNING log.
      - ``vercel_kv`` requested but KV env vars missing → SqliteBackend +
        WARNING log explaining the missing var.
      - Both ``vercel_kv`` env vars present → VercelKvBackend.
    """
    # Import here, not at module load — keeps the selector module
    # cheap (importable without pulling httpx/sqlite into the import
    # graph until a backend actually instantiates).
    from appscriptly.user_store import SqliteBackend

    raw = os.environ.get("STORAGE_BACKEND", "").strip().lower()

    if raw in ("", "sqlite"):
        return SqliteBackend()

    if raw == "vercel_kv":
        # Lazy import keeps vercel_kv_backend off the module-load path
        # for stdio + Fly users who never touch Vercel KV.
        try:
            from appscriptly.storage.vercel_kv_backend import (
                VercelKvBackend,
            )
            return VercelKvBackend()
        except RuntimeError as e:
            # KV env vars missing. Log loudly + fall back.
            log.warning(
                "storage: STORAGE_BACKEND=vercel_kv requested but "
                "VercelKvBackend construction failed (%s); falling "
                "back to SqliteBackend. State will NOT persist across "
                "cold starts on Vercel. See "
                "docs/runbooks/vercel-activation.md.",
                e,
            )
            return SqliteBackend()

    # Unknown value — fail-soft to SqliteBackend.
    log.warning(
        "storage: unknown STORAGE_BACKEND value %r (expected one of "
        "'sqlite' or 'vercel_kv'); defaulting to SqliteBackend. "
        "Check the env var for typos.",
        raw,
    )
    return SqliteBackend()
