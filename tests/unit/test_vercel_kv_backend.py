"""PR-Δ6 — VercelKvBackend (Upstash REST adapter).

Three test concerns:

  1. **Protocol conformance**: VercelKvBackend satisfies the
     ``user_store.StorageBackend`` Protocol (the @runtime_checkable
     isinstance assertion is the regression guard against future
     method-signature drift).

  2. **Behavioral parity with SqliteBackend / InMemoryBackend**:
     same merge semantics on save_state, same {} return for absent
     users, same NULL-column-omitted result shape, same idempotent
     clear_state, same created_at/updated_at bookkeeping.

  3. **Upstash REST wire-shape correctness**: verifies the right
     Redis commands hit the wire (HSET / HGETALL / HEXISTS / DEL),
     with the right key prefix, the right JSON encoding per field
     value, the right Authorization header.

The tests mock httpx.Client to avoid any real network I/O — Upstash
is a 3rd-party service we don't depend on at test time. Mocking at
the httpx layer (rather than a fake KV in-process) keeps the wire-
shape coverage real: a regression in the command construction would
fire here.
"""
from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest


# ---------------------------------------------------------------------
# Helpers — fake Upstash REST endpoint
# ---------------------------------------------------------------------


class _FakeUpstashClient:
    """A minimal in-memory fake of httpx.Client that emulates Upstash's
    REST surface.

    Stores the hashes locally so tests can drive a full save → get →
    save → clear sequence without any network. Each ``.post()`` call
    records the request for later assertions and dispatches on the
    Redis command in the body.
    """

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        # Capture every call so tests can assert on the wire shape.
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: Any, headers: dict) -> Any:
        self.calls.append({"url": url, "command": json, "headers": dict(headers)})
        command = json[0]
        if command == "HEXISTS":
            key, field = json[1], json[2]
            exists = key in self._hashes and field in self._hashes[key]
            return _FakeResponse(200, {"result": 1 if exists else 0})
        if command == "HSET":
            key = json[1]
            pairs = json[2:]
            hash_ = self._hashes.setdefault(key, {})
            for i in range(0, len(pairs), 2):
                hash_[pairs[i]] = pairs[i + 1]
            return _FakeResponse(200, {"result": len(pairs) // 2})
        if command == "HGETALL":
            key = json[1]
            hash_ = self._hashes.get(key, {})
            # Upstash returns the result as a flat alternating list.
            flat: list[str] = []
            for k, v in hash_.items():
                flat.extend([k, v])
            return _FakeResponse(200, {"result": flat})
        if command == "DEL":
            key = json[1]
            existed = key in self._hashes
            self._hashes.pop(key, None)
            return _FakeResponse(200, {"result": 1 if existed else 0})
        raise AssertionError(f"_FakeUpstashClient: unhandled command {command!r}")

    def close(self) -> None:
        pass


class _FakeResponse:
    """Minimal mock of httpx.Response."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body) if isinstance(body, str) else __import__("json").dumps(body)

    def json(self) -> Any:
        return self._body


def _make_backend(client: _FakeUpstashClient | None = None):
    """Build a VercelKvBackend with the fake REST client injected."""
    from appscriptly.storage.vercel_kv_backend import VercelKvBackend

    return VercelKvBackend(
        rest_url="https://fake.upstash.io",
        rest_token="fake-token-32-bytes-long-abcdefg",
        http_client=client or _FakeUpstashClient(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------
# 1. Protocol conformance
# ---------------------------------------------------------------------


def test_vercel_kv_backend_satisfies_storage_backend_protocol():
    """Runtime isinstance check against the @runtime_checkable Protocol.

    If a future refactor renames a method (e.g. ``get_state`` →
    ``read_state``), this fires BEFORE any caller breaks. The
    Protocol is the contract everything else trusts.
    """
    from appscriptly.user_store import StorageBackend

    backend = _make_backend()
    assert isinstance(backend, StorageBackend), (
        "VercelKvBackend no longer satisfies StorageBackend Protocol — "
        "a method was renamed or its signature changed"
    )


def test_vercel_kv_backend_init_schema_is_noop():
    """Redis has no schema; ``init_schema`` is a no-op for parity with
    SqliteBackend's contract that callers can always call it safely."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.init_schema()
    # No HTTP calls should have been issued.
    assert client.calls == []


# ---------------------------------------------------------------------
# 2. Construction-time validation
# ---------------------------------------------------------------------


def test_vercel_kv_backend_raises_when_url_missing(monkeypatch):
    """Without KV_REST_API_URL (and no explicit arg), construction
    must fail loudly rather than defer the error to first request."""
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    from appscriptly.storage.vercel_kv_backend import VercelKvBackend

    with pytest.raises(RuntimeError, match="KV_REST_API_URL"):
        VercelKvBackend()


def test_vercel_kv_backend_raises_when_token_missing(monkeypatch):
    """Same eagerness for the token. Misconfigured operator sees the
    error at startup, not on the first user request."""
    monkeypatch.setenv("KV_REST_API_URL", "https://fake.upstash.io")
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    from appscriptly.storage.vercel_kv_backend import VercelKvBackend

    with pytest.raises(RuntimeError, match="KV_REST_API_TOKEN"):
        VercelKvBackend()


def test_vercel_kv_backend_reads_env_vars_when_args_omitted(monkeypatch):
    """Production path: Vercel sets KV_REST_API_URL +
    KV_REST_API_TOKEN automatically; the backend picks them up
    without explicit arg passing."""
    monkeypatch.setenv("KV_REST_API_URL", "https://prod.upstash.io/")
    monkeypatch.setenv("KV_REST_API_TOKEN", "prod-tok")
    from appscriptly.storage.vercel_kv_backend import VercelKvBackend

    backend = VercelKvBackend()
    # Trailing slash should have been stripped for unambiguous URL joining.
    assert backend._rest_url == "https://prod.upstash.io"
    assert backend._rest_token == "prod-tok"


# ---------------------------------------------------------------------
# 3. Behavioral parity with InMemoryBackend / SqliteBackend
# ---------------------------------------------------------------------


def test_get_state_returns_empty_dict_for_absent_user():
    """SqliteBackend returns {} when no row exists. VercelKvBackend
    must match — every consumer branches on ``if state.get('field')``
    or ``if 'field' in state`` and would break on a different absent-
    user representation."""
    backend = _make_backend()
    assert backend.get_state("ghost-user") == {}


def test_save_state_creates_row_with_user_id_created_at_updated_at():
    """First save_state for a user must stamp the user_id +
    created_at + updated_at, matching SqliteBackend's INSERT path.
    The user_id stamp matters because consumers read it back from
    ``state['user_id']``."""
    backend = _make_backend()
    backend.save_state("user-A", {"google_creds_json": '{"tok": "x"}'})

    state = backend.get_state("user-A")
    assert state["user_id"] == "user-A"
    assert state["google_creds_json"] == '{"tok": "x"}'
    assert isinstance(state["created_at"], int)
    assert isinstance(state["updated_at"], int)


def test_save_state_merges_updates_does_not_overwrite():
    """The killer guard from test_storage_backend.py applied to
    VercelKvBackend: a partial update must NOT erase other fields.
    SqliteBackend gets this from SET-clause-of-only-given-cols UPDATE;
    VercelKvBackend gets it from HSET only touching the fields in
    the call."""
    backend = _make_backend()
    backend.save_state(
        "u",
        {
            "apps_script_url": "https://script.google.com/macros/s/X/exec",
            "apps_script_script_id": "SID-1",
        },
    )
    backend.save_state("u", {"apps_script_deployment_id": "D-1"})  # partial

    state = backend.get_state("u")
    assert state["apps_script_url"] == "https://script.google.com/macros/s/X/exec"
    assert state["apps_script_script_id"] == "SID-1"
    assert state["apps_script_deployment_id"] == "D-1"


def test_save_state_bumps_updated_at_preserves_created_at():
    """SqliteBackend's UPDATE path preserves created_at and bumps
    updated_at. VercelKvBackend's HSET-on-existing path mirrors
    this via the HEXISTS-then-conditional-HSET pattern."""
    backend = _make_backend()
    backend.save_state("u", {"google_creds_json": '{"tok": "a"}'})
    state_1 = backend.get_state("u")
    created_at_1 = state_1["created_at"]

    # Sleep 1.1s so the second-resolution timestamp moves.
    time.sleep(1.1)

    backend.save_state("u", {"google_creds_json": '{"tok": "b"}'})
    state_2 = backend.get_state("u")

    assert state_2["created_at"] == created_at_1, (
        "created_at must be preserved across saves"
    )
    assert state_2["updated_at"] > created_at_1, (
        "updated_at must move forward on the second save"
    )


def test_clear_state_removes_the_user():
    backend = _make_backend()
    backend.save_state("u", {"google_creds_json": '{"tok": "x"}'})
    assert backend.get_state("u") != {}

    backend.clear_state("u")
    assert backend.get_state("u") == {}


def test_clear_state_idempotent_on_absent_user():
    """SqliteBackend's DELETE-WHERE is idempotent (0 rows affected,
    no error). DEL on an absent Redis key is also a 0-return.
    VercelKvBackend must NOT raise."""
    backend = _make_backend()
    # User was never created. Must not raise.
    backend.clear_state("user-never-existed")


def test_get_state_omits_None_values():
    """SqliteBackend filters NULL columns out of the returned dict so
    callers can use ``if "field" in state``. VercelKvBackend must
    match — a None value in the hash MUST be skipped, not surfaced."""
    client = _FakeUpstashClient()
    # Manually inject a None JSON-encoded value into the fake hash
    # (simulating either external mutation or a future caller bug).
    backend = _make_backend(client)
    backend.save_state("u", {"google_creds_json": '{"tok": "x"}'})
    # Now poke a literal "null" JSON value directly into the fake
    # hash to simulate the None-value case.
    client._hashes["user_state:u"]["legacy_null_field"] = "null"

    state = backend.get_state("u")
    assert "google_creds_json" in state
    assert "legacy_null_field" not in state, (
        "None-valued fields must be filtered out to match Sqlite's "
        "NULL-column-omitted contract"
    )


def test_cross_user_isolation():
    """User A's state must be invisible to a read for user B. Pinned
    explicitly because future cache layers / multi-tenant prefix
    schemes could regress isolation without breaking the merge
    semantics tests above."""
    backend = _make_backend()
    backend.save_state("alice", {"google_creds_json": '{"tok": "alice-tok"}'})
    backend.save_state("bob", {"google_creds_json": '{"tok": "bob-tok"}'})

    alice = backend.get_state("alice")
    bob = backend.get_state("bob")

    assert json.loads(alice["google_creds_json"])["tok"] == "alice-tok"
    assert json.loads(bob["google_creds_json"])["tok"] == "bob-tok"
    assert backend.get_state("carol") == {}


# ---------------------------------------------------------------------
# 4. Upstash REST wire-shape correctness
# ---------------------------------------------------------------------


def test_wire_shape_uses_key_prefix():
    """Every Redis command must target a key with the ``user_state:``
    prefix — namespacing the user_state hashes so future multi-
    tenant projects can share a KV instance without collision."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.save_state("alice", {"google_creds_json": '{"tok": "x"}'})
    backend.get_state("alice")
    backend.clear_state("alice")

    keys_seen = set()
    for call in client.calls:
        # command[1] is the Redis key for HSET / HGETALL / HEXISTS / DEL.
        if len(call["command"]) >= 2:
            keys_seen.add(call["command"][1])
    # Every key must be prefixed.
    for key in keys_seen:
        assert key.startswith("user_state:"), (
            f"Redis key {key!r} missing user_state: prefix"
        )


def test_wire_shape_authorization_header_is_bearer_token():
    """Upstash requires ``Authorization: Bearer <token>``. A wrong
    header shape would get 401 on every request — pin the format
    so a future refactor doesn't accidentally use
    ``Authorization: Token <...>`` or omit ``Bearer ``."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.get_state("alice")
    assert client.calls, "expected at least one HTTP call"
    auth = client.calls[0]["headers"].get("Authorization", "")
    assert auth.startswith("Bearer "), (
        f"Authorization header should be 'Bearer <token>'; got {auth!r}"
    )


def test_wire_shape_save_state_emits_hset_command():
    """save_state should hit HSET — NOT a SET (string) or SADD (set).
    Hash is the right Redis data structure for a per-user state row."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.save_state("u", {"google_creds_json": '{"tok": "x"}'})
    commands = [c["command"][0] for c in client.calls]
    assert "HSET" in commands, (
        f"save_state should emit HSET; saw commands={commands!r}"
    )


def test_wire_shape_get_state_emits_hgetall_command():
    """get_state should hit HGETALL — fetches the whole hash in one
    round-trip rather than N HGET calls (avoids the N+1 latency)."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.save_state("u", {"google_creds_json": '{"tok": "x"}'})
    client.calls.clear()  # discard the save_state calls
    backend.get_state("u")
    commands = [c["command"][0] for c in client.calls]
    assert commands == ["HGETALL"], (
        f"get_state should emit a single HGETALL; saw {commands!r}"
    )


def test_wire_shape_clear_state_emits_del_command():
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.clear_state("u")
    commands = [c["command"][0] for c in client.calls]
    assert commands == ["DEL"]


def test_wire_shape_values_are_json_encoded():
    """Every value written to the hash should be JSON-encoded so
    ints / strs / bools all round-trip cleanly. A future regression
    that wrote str() instead of json.dumps() would lose the type
    distinction (int 42 → str "42", which deserializes to "42" not
    42 on get_state)."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.save_state("u", {"apps_script_version_number": 42})

    # Find the HSET call.
    hset_call = next(c for c in client.calls if c["command"][0] == "HSET")
    pairs = hset_call["command"][2:]
    # Find the version_number entry.
    idx = pairs.index("apps_script_version_number")
    encoded_value = pairs[idx + 1]
    # JSON-encoded int is the literal "42", not Python's str(42).
    assert encoded_value == "42"
    # Round-trip: get_state must return the int, not the string.
    state = backend.get_state("u")
    assert state["apps_script_version_number"] == 42
    assert isinstance(state["apps_script_version_number"], int)


def test_wire_shape_first_save_includes_user_id_and_created_at_pairs():
    """The first HSET for a user must include both ``user_id`` and
    ``created_at`` so the row is queryable + datable. Subsequent
    saves must NOT re-set created_at (preserved per the contract)."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.save_state("alice", {"google_creds_json": '{"tok": "x"}'})

    # First save: HEXISTS + HSET. The HSET command should include
    # user_id + created_at + updated_at + google_creds_json.
    hset_call = next(c for c in client.calls if c["command"][0] == "HSET")
    pairs = hset_call["command"][2:]
    field_names = pairs[::2]  # even indices are field names
    assert "user_id" in field_names
    assert "created_at" in field_names
    assert "updated_at" in field_names
    assert "google_creds_json" in field_names


def test_wire_shape_second_save_does_not_resend_created_at():
    """The second HSET for the same user must NOT include
    ``created_at`` (preserve the original). Only ``updated_at`` +
    the caller's update fields."""
    client = _FakeUpstashClient()
    backend = _make_backend(client)
    backend.save_state("alice", {"google_creds_json": '{"tok": "x"}'})
    client.calls.clear()

    backend.save_state("alice", {"google_creds_json": '{"tok": "y"}'})
    hset_call = next(c for c in client.calls if c["command"][0] == "HSET")
    pairs = hset_call["command"][2:]
    field_names = pairs[::2]
    assert "created_at" not in field_names, (
        "Second save must preserve created_at; HSET should only carry "
        f"the update + updated_at. Got fields: {field_names!r}"
    )
    assert "updated_at" in field_names
    assert "google_creds_json" in field_names


# ---------------------------------------------------------------------
# 5. Upstash error surface
# ---------------------------------------------------------------------


def test_upstash_429_raises_UpstashRestError():
    """Rate-limit responses must surface as UpstashRestError carrying
    the status code so caller-side retry / monitoring can branch on
    the 429 specifically."""
    from appscriptly.storage.vercel_kv_backend import (
        UpstashRestError,
        VercelKvBackend,
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "rate limited"
    mock_client.post.return_value = mock_response

    backend = VercelKvBackend(
        rest_url="https://fake.upstash.io",
        rest_token="t",
        http_client=mock_client,
    )

    with pytest.raises(UpstashRestError) as exc:
        backend.get_state("u")
    assert exc.value.status_code == 429


def test_upstash_error_in_json_body_raises_UpstashRestError():
    """Upstash returns 200 OK with a JSON ``error`` field for
    protocol-level errors (bad command, wrong arity). The backend
    must treat that as a failure even though HTTP succeeded."""
    from appscriptly.storage.vercel_kv_backend import (
        UpstashRestError,
        VercelKvBackend,
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '{"error": "ERR wrong arity"}'
    mock_response.json.return_value = {"error": "ERR wrong arity"}
    mock_client.post.return_value = mock_response

    backend = VercelKvBackend(
        rest_url="https://fake.upstash.io",
        rest_token="t",
        http_client=mock_client,
    )

    with pytest.raises(UpstashRestError, match="ERR wrong arity"):
        backend.get_state("u")


def test_network_error_raises_UpstashRestError_with_status_0():
    """httpx.HTTPError (DNS failure, connection refused, timeout)
    should bubble as UpstashRestError with status 0 so callers can
    branch on a uniform exception type rather than having to catch
    multiple httpx exception classes."""
    from appscriptly.storage.vercel_kv_backend import (
        UpstashRestError,
        VercelKvBackend,
    )

    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.ConnectError("connection refused")

    backend = VercelKvBackend(
        rest_url="https://fake.upstash.io",
        rest_token="t",
        http_client=mock_client,
    )

    with pytest.raises(UpstashRestError) as exc:
        backend.get_state("u")
    assert exc.value.status_code == 0
    assert "network error" in str(exc.value).lower()
