"""Unit tests for ``appscriptly.http_server.jobs`` (the T1.1 job runner).

The load-bearing property under test: the work is a DETACHED task, so
cancelling a coroutine that awaits it (what uvicorn does to the request
handler when the client disconnects) does NOT cancel the work - the
conversion completes and the row records the outcome. Plus: error
classification parity with the historical sync handler, heartbeats
while the converter thread runs, and registry hygiene.

All async tests drive their own event loop via ``asyncio.run`` inside
sync test functions - the repo's established pattern (no pytest-asyncio
markers needed).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from appscriptly import job_store
from appscriptly.http_server import jobs


@pytest.fixture(autouse=True)
def _reset_job_state():
    job_store._initialized_paths.clear()
    jobs._TASKS.clear()
    yield
    job_store._initialized_paths.clear()
    jobs._TASKS.clear()


def test_client_disconnect_does_not_kill_the_job():
    """T1.1 core: cancel the AWAITING coroutine mid-job; the job itself
    must run to completion and the row must read done with the result.

    The awaiter goes through ``jobs.wait_for_outcome`` exactly like the
    production sync path - a bare ``await task`` would propagate the
    cancellation INTO the job (Task.cancel cancels the awaited future),
    which is the asyncio trap the shield exists for.
    """
    job_id = job_store.create_job("user-A", "fp-disconnect")
    started = []

    def slow_convert(_prior=None):
        started.append(True)
        time.sleep(0.3)
        return {"doc_id": "SURVIVED", "tabs": []}

    async def scenario():
        task = jobs.start_job(job_id, slow_convert)

        async def request_handler():
            # Stands in for the sync response path: shielded await.
            return await jobs.wait_for_outcome(task)

        handler = asyncio.create_task(request_handler())
        await asyncio.sleep(0.05)  # let the converter thread start
        handler.cancel()  # the "client disconnected" moment
        with pytest.raises(asyncio.CancelledError):
            await handler
        # The detached job task is unaffected by the awaiter's death.
        outcome = await jobs.wait_for_outcome(task)
        return outcome

    outcome = asyncio.run(scenario())
    assert started, "converter never started"
    assert outcome == ("done", {"doc_id": "SURVIVED", "tabs": []}, None)
    row = job_store.get_job(job_id)
    assert row is not None
    assert row["status"] == "done"
    assert job_store.result_dict(row) == {"doc_id": "SURVIVED", "tabs": []}


def test_error_outcome_is_classified_and_persisted():
    """A converter exception must be recorded with the SAME http status
    + payload the pre-job-model sync handler would have returned."""
    job_id = job_store.create_job("user-A", "fp-error")

    def failing_convert(_prior=None):
        raise ValueError("bad docx")

    async def scenario():
        return await jobs.start_job(job_id, failing_convert)

    outcome = asyncio.run(scenario())
    assert outcome == ("error", 400, {"error": "bad docx"})
    row = job_store.get_job(job_id)
    assert row is not None
    assert row["status"] == "error"
    assert job_store.error_dict(row) == {
        "http_status": 400, "payload": {"error": "bad docx"},
    }


def test_returned_error_envelope_finishes_as_error_row():
    """N3: a converter that RETURNS the S2.5 kept-doc envelope with an
    ``error`` field is a FAILED job. The row must be terminal
    status=error carrying the FULL envelope (a poller reading
    status=="done" may trust it as success)."""
    job_id = job_store.create_job("user-A", "fp-envelope")
    envelope = {
        "doc_id": "KEPT", "url": "https://x", "tabs": [],
        "error": "quota death mid-transplant",
        "completion": {"steps_completed": ["import"], "moved_sections": [],
                       "pending_sections": ["A", "B"]},
    }

    async def scenario():
        return await jobs.start_job(job_id, lambda _prior=None: envelope)

    outcome = asyncio.run(scenario())
    assert outcome == ("error", 500, envelope)
    row = job_store.get_job(job_id)
    assert row is not None
    assert row["status"] == "error"
    assert job_store.error_dict(row) == {"http_status": 500, "payload": envelope}
    assert job_store.result_dict(row) is None


def test_classifier_matches_historical_sync_mapping():
    assert jobs.classify_convert_error(FileNotFoundError("x"))[0] == 400
    assert jobs.classify_convert_error(ValueError("x"))[0] == 400
    assert jobs.classify_convert_error(RuntimeError("x"))[0] == 500
    status, payload = jobs.classify_convert_error(KeyError("surprise"))
    assert status == 500
    assert "KeyError" in payload["error"]


def test_heartbeat_fires_while_converter_runs(monkeypatch):
    """With a shrunken interval, a slow converter gets multiple
    heartbeat touches - the signal the stalled derivation depends on."""
    monkeypatch.setattr(jobs, "HEARTBEAT_INTERVAL", 0.05)
    touches: list[str] = []
    real_touch = job_store.touch_heartbeat
    monkeypatch.setattr(
        job_store, "touch_heartbeat",
        lambda jid: (touches.append(jid), real_touch(jid))[1],
    )

    job_id = job_store.create_job("user-A", "fp-heartbeat")

    async def scenario():
        return await jobs.start_job(job_id, lambda _prior=None: time.sleep(0.3) or {"ok": 1})

    asyncio.run(scenario())
    # 0.3s of work at a 0.05s interval: at least 3 touches even with
    # generous scheduling slop (exact count is timing-dependent).
    assert len(touches) >= 3
    assert all(jid == job_id for jid in touches)


def test_task_registry_holds_then_releases():
    job_id = job_store.create_job("user-A", "fp-registry")

    async def scenario():
        task = jobs.start_job(job_id, lambda _prior=None: {"ok": 1})
        assert jobs.get_task(job_id) is task
        await task
        # Done-callbacks run soon after; yield once to let them fire.
        await asyncio.sleep(0)
        return jobs.get_task(job_id)

    assert asyncio.run(scenario()) is None
    row = job_store.get_job(job_id)
    assert row is not None and row["status"] == "done"


# ---------------------------------------------------------------------
# Concurrency cap (application-level semaphore)
# ---------------------------------------------------------------------


def _statuses(job_ids: list[str]) -> list[str]:
    out = []
    for jid in job_ids:
        row = job_store.get_job(jid)
        assert row is not None
        out.append(job_store.derive_status(row))
    return out


def test_concurrency_cap_two_running_rest_honestly_queued(monkeypatch):
    """A 5-job burst on the default cap (2) runs exactly 2 converters
    concurrently; the other 3 rows report QUEUED (not running - the
    pre-limiter aggravator was mark_running firing before a worker
    thread was even available) and everything completes."""
    import threading

    monkeypatch.delenv("CONVERT_JOB_MAX_CONCURRENCY", raising=False)
    monkeypatch.setattr(jobs, "HEARTBEAT_INTERVAL", 0.05)
    gate = threading.Event()
    concurrent = []
    lock = threading.Lock()

    def gated_convert(_prior=None):
        with lock:
            concurrent.append(1)
            high_water = len(concurrent)
        try:
            gate.wait(timeout=10)
            return {"ok": high_water}
        finally:
            with lock:
                concurrent.pop()

    job_ids = [
        job_store.create_job("user-A", f"fp-burst-{i}") for i in range(5)
    ]

    async def scenario():
        tasks = [jobs.start_job(jid, gated_convert) for jid in job_ids]
        # Let the runners settle: 2 acquire slots and start their
        # threads; 3 wait on the semaphore.
        await asyncio.sleep(0.3)
        mid_flight = _statuses(job_ids)
        assert mid_flight.count("running") == 2, mid_flight
        assert mid_flight.count("queued") == 3, mid_flight
        # No queued row may read stalled later either - they heartbeat
        # while waiting. (Implicitly covered: derive_status returned
        # queued, meaning the heartbeat is fresh.)
        assert len(concurrent) == 2, "only 2 converter threads may run"
        gate.set()
        outcomes = await asyncio.gather(*tasks)
        return outcomes

    outcomes = asyncio.run(scenario())
    assert all(kind == "done" for kind, _, _ in outcomes)
    assert _statuses(job_ids) == ["done"] * 5


def test_concurrency_cap_env_tunable_and_released_on_error(monkeypatch):
    """CONVERT_JOB_MAX_CONCURRENCY=1 serializes jobs, and a converter
    FAILURE releases the slot (job 2 would hang forever otherwise)."""
    monkeypatch.setenv("CONVERT_JOB_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(jobs, "HEARTBEAT_INTERVAL", 0.05)

    j1 = job_store.create_job("user-A", "fp-serial-1")
    j2 = job_store.create_job("user-A", "fp-serial-2")

    def failing(_prior=None):
        time.sleep(0.15)
        raise RuntimeError("first job dies holding the slot")

    async def scenario():
        t1 = jobs.start_job(j1, failing)
        t2 = jobs.start_job(j2, lambda _prior=None: {"ok": 2})
        await asyncio.sleep(0.05)
        # Cap 1: the second job must be waiting, honestly queued.
        assert _statuses([j2]) == ["queued"]
        return await asyncio.gather(t1, t2)

    o1, o2 = asyncio.run(scenario())
    assert o1[0] == "error" and o1[1] == 500
    assert o2 == ("done", {"ok": 2}, None)
    assert _statuses([j1, j2]) == ["error", "done"]


# ---------------------------------------------------------------------
# A2 (retest 3): rate-limit deaths requeue instead of going terminal
# ---------------------------------------------------------------------


def _rl_envelope(doc_id: str = "KEPT-PARTIAL") -> dict:
    """A rate-limit-caused S2.5 partial-failure envelope, as
    docx_import builds it (rate_limited=True is the runner's signal)."""
    return {
        "doc_id": doc_id, "url": "https://x", "tabs": [],
        "error": "quota exhausted past the backoff budget",
        "rate_limited": True,
        "completion": {"steps_completed": ["import", "shells"],
                       "moved_sections": [], "pending_sections": ["A"]},
    }


@pytest.fixture
def fast_requeue(monkeypatch):
    monkeypatch.setattr(jobs, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setenv("CONVERT_JOB_REQUEUE_DELAY_SECONDS", "0.05")
    monkeypatch.delenv("CONVERT_JOB_MAX_REQUEUES", raising=False)


def test_rate_limited_envelope_requeues_once_then_succeeds(
    fast_requeue, monkeypatch
):
    """The A2 safety net end to end: a rate-limit partial failure does
    NOT go terminal - the row re-arms (honest queued), the runner waits
    the quota window, re-runs the work with the PRIOR envelope handed
    over, and the retry's success is the job's outcome."""
    rearms: list[str] = []
    real_rearm = job_store.rearm_job
    monkeypatch.setattr(
        job_store, "rearm_job",
        lambda jid: (rearms.append(jid), real_rearm(jid))[1],
    )

    job_id = job_store.create_job("user-A", "fp-requeue")
    calls: list[dict | None] = []

    def work(prior=None):
        calls.append(prior)
        if len(calls) == 1:
            return _rl_envelope()
        return {"doc_id": "SECOND-TRY", "url": "https://x", "tabs": []}

    async def scenario():
        return await jobs.start_job(job_id, work)

    outcome = asyncio.run(scenario())
    assert outcome == (
        "done", {"doc_id": "SECOND-TRY", "url": "https://x", "tabs": []}, None,
    )
    assert rearms == [job_id], "the row must re-arm exactly once"
    assert calls[0] is None
    assert calls[1] is not None and calls[1]["doc_id"] == "KEPT-PARTIAL", (
        "the retry must receive the prior attempt's envelope"
    )
    row = job_store.get_job(job_id)
    assert row is not None and row["status"] == "done"


def test_rate_limited_twice_goes_terminal_with_requeue_count(fast_requeue):
    """Bounded: after CONVERT_JOB_MAX_REQUEUES (default 1) the failure
    is terminal, carrying the LAST envelope + the requeue count."""
    job_id = job_store.create_job("user-A", "fp-requeue-2")
    calls: list[dict | None] = []

    def work(prior=None):
        calls.append(prior)
        return _rl_envelope(doc_id=f"KEPT-{len(calls)}")

    async def scenario():
        return await jobs.start_job(job_id, work)

    kind, status, payload = asyncio.run(scenario())
    assert (kind, status) == ("error", 500)
    assert payload["doc_id"] == "KEPT-2", "the LAST attempt's envelope wins"
    assert payload["requeue_attempts"] == 1
    assert len(calls) == 2
    row = job_store.get_job(job_id)
    assert row is not None and row["status"] == "error"
    stored = job_store.error_dict(row)
    assert stored is not None
    assert stored["payload"]["requeue_attempts"] == 1


def test_raised_429_also_requeues(fast_requeue):
    """A pre-transplant 429 RAISES (no envelope; staging was cleaned).
    The runner requeues it the same way, with no prior envelope."""
    from googleapiclient.errors import HttpError as _HttpError

    class _Resp:
        status = 429
        reason = "Too Many Requests"

    job_id = job_store.create_job("user-A", "fp-requeue-raise")
    calls: list[dict | None] = []

    def work(prior=None):
        calls.append(prior)
        if len(calls) == 1:
            raise _HttpError(resp=_Resp(), content=b"rate limited")
        return {"ok": 1}

    async def scenario():
        return await jobs.start_job(job_id, work)

    outcome = asyncio.run(scenario())
    assert outcome == ("done", {"ok": 1}, None)
    assert calls == [None, None], "raised-429 retries carry no envelope"


def test_non_rate_limit_failures_never_requeue(fast_requeue):
    """N1/N3 terminal semantics are untouched for ordinary failures."""
    job_id = job_store.create_job("user-A", "fp-no-requeue")
    calls: list[dict | None] = []

    def work(prior=None):
        calls.append(prior)
        raise ValueError("corrupt docx")

    async def scenario():
        return await jobs.start_job(job_id, work)

    kind, status, payload = asyncio.run(scenario())
    assert (kind, status) == ("error", 400)
    assert "requeue_attempts" not in payload
    assert len(calls) == 1, "ordinary failures must not re-run"


def test_requeue_releases_the_slot_during_its_delay(monkeypatch):
    """The requeue delay must not squat on a concurrency slot: with cap
    1, job B runs to completion WHILE job A waits out its quota window."""
    monkeypatch.setattr(jobs, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setenv("CONVERT_JOB_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("CONVERT_JOB_REQUEUE_DELAY_SECONDS", "0.5")

    ja = job_store.create_job("user-A", "fp-slot-a")
    jb = job_store.create_job("user-A", "fp-slot-b")
    finish_order: list[str] = []

    calls_a: list[dict | None] = []

    def work_a(prior=None):
        calls_a.append(prior)
        if len(calls_a) == 1:
            return _rl_envelope()
        finish_order.append("A")
        return {"ok": "A"}

    def work_b(prior=None):
        finish_order.append("B")
        return {"ok": "B"}

    async def scenario():
        ta = jobs.start_job(ja, work_a)
        tb = jobs.start_job(jb, work_b)
        return await asyncio.gather(ta, tb)

    oa, ob = asyncio.run(scenario())
    assert oa[0] == "done" and ob[0] == "done"
    assert finish_order == ["B", "A"], (
        "B must complete during A's requeue delay (slot released)"
    )
    assert _statuses([ja, jb]) == ["done", "done"]


def test_storm_completes_fully_with_requeues(fast_requeue, monkeypatch):
    """The tester's acceptance criterion, unit-shaped: an 8-job storm
    where several jobs hit the rate limit once still completes 8/8 with
    zero manual steps (requeue + retry), truthful terminal rows."""
    monkeypatch.delenv("CONVERT_JOB_MAX_CONCURRENCY", raising=False)

    job_ids = [
        job_store.create_job("user-A", f"fp-storm-{i}") for i in range(8)
    ]
    rate_limited_once = {job_ids[1], job_ids[4], job_ids[6]}
    attempts: dict[str, int] = {}

    def make_work(jid):
        def work(prior=None):
            attempts[jid] = attempts.get(jid, 0) + 1
            if jid in rate_limited_once and attempts[jid] == 1:
                return _rl_envelope(doc_id=f"KEPT-{jid[:8]}")
            return {"doc_id": f"OK-{jid[:8]}", "url": "u", "tabs": []}
        return work

    async def scenario():
        tasks = [jobs.start_job(jid, make_work(jid)) for jid in job_ids]
        return await asyncio.gather(*tasks)

    outcomes = asyncio.run(scenario())
    assert all(kind == "done" for kind, _, _ in outcomes), outcomes
    assert _statuses(job_ids) == ["done"] * 8
    assert sum(attempts.values()) == 8 + len(rate_limited_once)
