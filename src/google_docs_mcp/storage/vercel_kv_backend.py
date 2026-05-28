"""VercelKvBackend — StorageBackend Protocol adapter for Vercel KV.

PR-Δ6 (Vercel pilot): third implementation of the
``user_store.StorageBackend`` Protocol after ``SqliteBackend``
(production on Fly) and ``InMemoryBackend`` (test fixture). Vercel
KV is powered by Upstash Redis; the HTTP REST API surface is what
we talk to here (the protocol is documented at
<https://upstash.com/docs/redis/features/restapi>).

Why HTTP REST not a native redis client:

- Vercel KV exposes ``KV_REST_API_URL`` + ``KV_REST_API_TOKEN``
  env vars that point at the REST endpoint, NOT a raw
  ``redis://`` URL. The native redis-py client is a different
  surface that requires TCP egress (which Vercel's serverless
  Python runtime restricts).
- httpx is already in the transitive dep tree (via FastMCP), so
  no new runtime dep needed. The Upstash REST protocol is dead
  simple: ``POST <URL>`` with body ``["COMMAND", "arg1", "arg2"]``,
  auth header ``Authorization: Bearer <TOKEN>``, JSON response
  ``{"result": ...}``.

Storage layout:

  Each ``user_id`` → one Redis HSET (hash) at key
  ``user_state:<user_id>``. The HSET maps field name → JSON-serialized
  value, identical to the column-per-field shape SqliteBackend uses.
  Merge semantics are bit-for-bit identical:

    - ``save_state(user_id, updates)``: HSET each ``(field, json_value)``
      pair from ``updates``, plus ``updated_at`` = ``int(time.time())``.
      Creates the hash on first call (with ``user_id`` + ``created_at``
      + ``updated_at`` set to ``now``) — Redis HSET is upsert by default.

    - ``get_state(user_id)``: HGETALL the hash. Returns ``{}`` for an
      absent key (matches Sqlite's "no row" return shape). JSON-decode
      each value back to its native type. Drop any ``None`` values so
      callers can use ``if "field" in state`` per the SQLite contract.

    - ``clear_state(user_id)``: DEL the hash key. Idempotent — DEL
      on an absent key is a 0-return, no error.

    - ``init_schema()``: no-op. Redis has no schema; the hash is
      created on first write.

**Statelessness contract**: the VercelKvBackend is suitable for
Vercel's stateless serverless containers (each cold-start re-imports
the module and re-creates the backend instance). The httpx client is
constructed lazily on first call so module import is cheap. Subsequent
calls in the same warm container reuse the client.

**Connection pool sizing**: Vercel functions are short-lived (max 60s
on Hobby tier per PR-Δ6 ADR). One httpx.Client per process with the
default pool limits (max 100 connections) is more than sufficient for
the request-throughput a single Vercel function instance handles.

**Failure modes**:

- Upstash 429 (rate-limited): bubble up as
  ``UpstashRestError`` with the rate-limit headers attached. Vercel
  function returns 5xx to the caller; PR-Δ3's retry adapter (which
  wraps Google API calls, NOT KV calls) does NOT cover this path.
  If 429 becomes a real operational issue, add a retry wrapper here.

- Upstash 5xx: bubble up as ``UpstashRestError``. Caller's
  responsibility to translate into a user-facing error (the existing
  ``@workspace_tool`` envelope catches arbitrary exceptions and
  surfaces them as ToolError).

- KV_REST_API_URL / KV_REST_API_TOKEN missing: construction-time
  ``RuntimeError`` with operator-facing instructions. The backend
  selector catches this and falls back to SqliteBackend with a
  loud WARNING log — fail-soft so a misconfigured Vercel deploy
  doesn't 500 on every request.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("google_docs_mcp.storage.vercel_kv")


# Redis key prefix: namespaces the user_state hashes so a future
# multi-tenant Vercel project (multiple appscriptly deploys sharing
# one KV instance) wouldn't collide. The prefix is fixed for now;
# operators wanting tenant separation use distinct KV instances
# rather than distinct prefixes on a shared one.
_KEY_PREFIX = "user_state:"


class UpstashRestError(RuntimeError):
    """Raised when the Upstash REST API returns a non-2xx.

    Carries the HTTP status code + Upstash's error message body so
    operator log review can disambiguate 429 (rate-limit, recoverable)
    from 401 (bad token, config issue) from 5xx (Upstash incident).
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Upstash REST API returned {status_code}: {body[:200]}"
        )


class VercelKvBackend:
    """StorageBackend implementation backed by Vercel KV (Upstash REST).

    See module docstring for the protocol shape + Redis storage layout.

    Args:
        rest_url: Upstash REST endpoint URL. Required. Vercel sets
            ``KV_REST_API_URL`` automatically when KV is bound to
            the project.
        rest_token: Upstash REST API token. Required. Vercel sets
            ``KV_REST_API_TOKEN`` automatically when KV is bound.
        http_client: Optional httpx.Client to use for requests. Tests
            inject a mock; production constructs a default one
            lazily on first call.
        timeout_seconds: HTTP timeout per request. Default 10s —
            Upstash is fast enough that a 10s timeout is generous
            (typical responses <100ms); higher would let a hung
            Upstash burn the entire Vercel function budget.
    """

    def __init__(
        self,
        *,
        rest_url: str | None = None,
        rest_token: str | None = None,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        # Allow explicit args (used by tests) OR env-var fallback
        # (used by production). Validate eagerly so a misconfigured
        # backend surfaces the problem at construction time rather
        # than on first request.
        resolved_url = rest_url or os.environ.get("KV_REST_API_URL", "")
        resolved_token = rest_token or os.environ.get("KV_REST_API_TOKEN", "")
        if not resolved_url:
            raise RuntimeError(
                "VercelKvBackend requires KV_REST_API_URL — set via "
                "Vercel project KV binding (Vercel populates the env "
                "var automatically) or pass rest_url= explicitly. "
                "See docs/runbooks/vercel-activation.md."
            )
        if not resolved_token:
            raise RuntimeError(
                "VercelKvBackend requires KV_REST_API_TOKEN — set via "
                "Vercel project KV binding (Vercel populates the env "
                "var automatically) or pass rest_token= explicitly. "
                "See docs/runbooks/vercel-activation.md."
            )
        # Strip trailing slash so URL joining is unambiguous.
        self._rest_url = resolved_url.rstrip("/")
        self._rest_token = resolved_token
        self._timeout = timeout_seconds
        self._http_client = http_client  # may be None; lazy-init on first call

    # --- Protocol surface (matches user_store.StorageBackend) -------------

    def init_schema(self) -> None:
        """No-op. Redis has no schema; hashes are created on first write.

        Kept for Protocol conformance — every StorageBackend impl
        gets ``init_schema()`` called by the user_store facade's
        ``_ensure_initialized`` analogue at first read/write.
        """
        return None

    def get_state(self, user_id: str) -> dict[str, Any]:
        """Return the full user-state row as a dict, or {} if absent.

        Matches SqliteBackend's contract: empty dict on missing user,
        full dict on present user, NO None values surfaced (callers
        can use ``if field in state`` safely).

        Implementation: HGETALL the hash. Upstash returns
        ``["field1", "value1", "field2", "value2", ...]`` for HGETALL
        (the RESP array, flattened). Decode each value via JSON
        (we always JSON-encode on write).
        """
        raw = self._execute(["HGETALL", _KEY_PREFIX + user_id])
        # Upstash returns the HGETALL result as a flat array of
        # [field, value, field, value, ...]. Convert to dict; drop
        # None values to match SqliteBackend's "NULL columns omitted"
        # contract.
        if not raw:
            return {}
        if not isinstance(raw, list):
            # Defensive: future Upstash API change could return a
            # different shape. Fail loudly rather than silently.
            raise UpstashRestError(
                500,
                f"HGETALL returned non-list shape: {type(raw).__name__}",
            )
        result: dict[str, Any] = {}
        # Pair up field/value entries.
        for i in range(0, len(raw), 2):
            field = raw[i]
            value_json = raw[i + 1]
            try:
                value = json.loads(value_json)
            except (json.JSONDecodeError, TypeError):
                # Defensive: a corrupted KV write (or a manual SET
                # bypass) could leave non-JSON bytes in the hash.
                # Skip rather than crash the whole get_state.
                log.warning(
                    "vercel_kv: dropping non-JSON value at field=%r "
                    "(corruption or external write?)",
                    field,
                )
                continue
            if value is None:
                # Mirror SqliteBackend: drop None values.
                continue
            result[field] = value
        return result

    def save_state(self, user_id: str, updates: dict[str, Any]) -> None:
        """Merge ``updates`` into the user's hash. Inserts if absent.

        Bit-for-bit semantic match with SqliteBackend.save_state:

          - On insert (hash absent): set ``user_id`` + ``created_at``
            + ``updated_at`` + every key in ``updates``.
          - On update (hash present): set every key in ``updates``,
            bump ``updated_at``, preserve ``created_at``.

        Single HSET call per save_state (Upstash supports multi-pair
        HSET). The "is the hash present?" check is done via HEXISTS on
        ``created_at`` — cheaper than HGETALL + Python-side branching.
        """
        now = int(time.time())

        # HEXISTS returns 1 if the field exists, 0 otherwise.
        key = _KEY_PREFIX + user_id
        exists = self._execute(["HEXISTS", key, "created_at"])

        # Build the HSET arg list: [HSET, key, field1, value1, field2, value2, ...]
        # Every value is JSON-encoded so the wire shape is uniform —
        # ints, strs, bools all round-trip cleanly through json.dumps/loads.
        pairs: list[Any] = []
        if not exists:
            # First write — stamp user_id + created_at.
            pairs.extend(["user_id", json.dumps(user_id)])
            pairs.extend(["created_at", json.dumps(now)])
        # Either way, bump updated_at + apply caller's updates.
        pairs.extend(["updated_at", json.dumps(now)])
        for field, value in updates.items():
            pairs.extend([field, json.dumps(value)])

        self._execute(["HSET", key, *pairs])

    def clear_state(self, user_id: str) -> None:
        """Delete the user's hash. Idempotent — DEL on absent key returns 0."""
        self._execute(["DEL", _KEY_PREFIX + user_id])

    # --- HTTP plumbing ---------------------------------------------------

    def _client(self) -> httpx.Client:
        """Lazy-init the httpx.Client.

        Module-import time stays cheap (the test suite imports
        VercelKvBackend without needing Upstash to be reachable). The
        client is constructed on first ``_execute`` call and reused
        for the lifetime of the backend instance.
        """
        if self._http_client is None:
            self._http_client = httpx.Client(
                timeout=self._timeout,
                # No Upstash-specific connection pool tuning — defaults
                # are fine for the per-Vercel-function request rate.
            )
        return self._http_client

    def _execute(self, command: list[Any]) -> Any:
        """POST a Redis command to Upstash REST. Return the ``result`` field.

        Command shape: ``[command, arg1, arg2, ...]`` per Upstash's
        REST protocol. Authentication: ``Authorization: Bearer <token>``
        header. Response is JSON ``{"result": <value>}`` on success
        or ``{"error": "..."}`` on failure.
        """
        client = self._client()
        try:
            response = client.post(
                self._rest_url,
                json=command,
                headers={
                    "Authorization": f"Bearer {self._rest_token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as e:
            # Network-level failure (DNS, connection refused, timeout).
            # Wrap so callers see a uniform exception type.
            raise UpstashRestError(
                0, f"network error talking to Upstash: {e}",
            ) from e

        if response.status_code >= 400:
            raise UpstashRestError(response.status_code, response.text)

        try:
            payload = response.json()
        except ValueError as e:
            raise UpstashRestError(
                response.status_code,
                f"non-JSON response body: {response.text[:200]}",
            ) from e

        if "error" in payload:
            # Upstash returns 200 OK with a JSON ``error`` field for
            # protocol-level errors (bad command, wrong arity). Treat
            # as a failure even though HTTP was 2xx.
            raise UpstashRestError(
                response.status_code, str(payload.get("error", ""))
            )

        return payload.get("result")

    def close(self) -> None:
        """Release the httpx.Client connection pool.

        Called by the backend selector at process shutdown when
        relevant. Vercel cold-starts don't need this (the process
        dies anyway); it exists for test cleanup + long-lived
        instances on Fly that might one day switch to VercelKvBackend
        for shared multi-region state.
        """
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None
