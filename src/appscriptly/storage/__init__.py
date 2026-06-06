"""Storage backend implementations beyond the canonical
``SqliteBackend`` + ``InMemoryBackend`` that live inline in
``user_store.py``.

PR-Δ6 (Vercel pilot) introduced this package to host the
``VercelKvBackend`` adapter — a third StorageBackend Protocol
implementation that talks to Upstash Redis via Vercel KV's
HTTP REST API. The canonical SqliteBackend stays in user_store.py
for backward-compat (every existing test patch site and import path
keeps working); only NEW backends live under this package.

Layout:

  storage/
  ├── __init__.py          — this file
  ├── vercel_kv_backend.py — Upstash REST adapter (PR-Δ6)
  └── backend_selector.py  — env-var-driven default-backend factory

**Why a new package rather than another inline class in user_store.py?**

Three reasons:

1. **Optional dep isolation.** ``VercelKvBackend`` requires no new
   *runtime* dependencies (httpx is already transitive via FastMCP),
   but a future backend (PostgresBackend → asyncpg, RedisBackend →
   redis, S3Backend → boto3) would. A separate module per backend
   lets each one own its imports without polluting the top-level
   ``user_store`` import graph that every existing tool path
   touches at module load.

2. **Selector seam.** ``backend_selector.py`` reads the
   ``STORAGE_BACKEND`` env var and returns the right backend
   instance. Lets the test suite + the new ``api/index.py`` Vercel
   entrypoint share a single resolution path.

3. **Discoverability.** Future contributors looking for "how do I
   add a Postgres backend" find ``storage/`` and have an obvious
   template to follow — vs hunting through ``user_store.py``'s
   500+ lines of mixed facade + validators + SqliteBackend
   internals.
"""
