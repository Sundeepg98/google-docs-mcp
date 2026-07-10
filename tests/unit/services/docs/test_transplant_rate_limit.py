"""N2 (2026-07-10 retest): 429 backoff in the transplant WRITE path.

Concurrent convert jobs tripped the per-user Docs write quota
(WriteRequestsPerMinutePerUser = 60) and the job died on the FIRST 429
even though RATE_LIMIT_EXCEEDED is retryable by definition (the rate
limiter rejects the request BEFORE it executes). These tests pin the
new behavior of ``content_transplant._batch_update``:

- a 429 backs off (exponential + jitter) and re-sends the SAME chunk;
- the wait budget is bounded; exhaustion re-raises the HttpError into
  the caller's existing keep-the-doc + completion-manifest path;
- non-429 write errors keep the single-shot contract (a 5xx may have
  partially executed; replaying risks duplicate inserts).

Sleeps are monkeypatched to record-without-waiting and jitter is pinned
to zero, so the tests are instant and deterministic.
"""
from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from appscriptly.services.docs import content_transplant as ct


class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Too Many Requests" if status == 429 else "boom"


def _http_error(status: int = 429) -> HttpError:
    return HttpError(
        resp=_FakeResp(status),  # type: ignore[arg-type]
        content=b'{"error": {"status": "RESOURCE_EXHAUSTED", '
        b'"message": "Write requests per minute per user"}}',
    )


class _FlakyDocs:
    """documents().batchUpdate(...).execute() that fails N times."""

    def __init__(self, failures: int, status: int = 429) -> None:
        self.failures_left = failures
        self.status = status
        self.execute_calls = 0

    def documents(self):
        return self

    def batchUpdate(self, documentId, body):  # noqa: N803 - Google API casing
        self._last_body = body
        return self

    def execute(self):
        self.execute_calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise _http_error(self.status)
        return {}


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """No real waiting: capture the backoff schedule, pin jitter to 0."""
    sleeps: list[float] = []
    monkeypatch.setattr(ct.time, "sleep", sleeps.append)
    monkeypatch.setattr(ct.random, "uniform", lambda a, b: 0.0)
    return sleeps


def test_429_backs_off_and_completes(recorded_sleeps):
    """Scripted 429, 429, success: the chunk lands with zero manual
    steps and the waits grow exponentially (2s then 4s, jitter pinned)."""
    docs = _FlakyDocs(failures=2)
    ct._batch_update(docs, "DOC1", [{"insertText": {}}])

    assert docs.execute_calls == 3
    assert recorded_sleeps == [2.0, 4.0]


def test_429_budget_exhaustion_fails_into_the_existing_error_path(recorded_sleeps):
    """A persistent rate limit stops retrying once the shared budget is
    spent and re-raises the REAL HttpError (which the pipeline's
    keep-the-doc + manifest handling already consumes)."""
    docs = _FlakyDocs(failures=999)
    budget = ct._RateLimitBudget(seconds=10.0)

    with pytest.raises(HttpError):
        ct._batch_update(docs, "DOC1", [{"insertText": {}}], budget=budget)

    # 2s + 4s fit in the 10s budget; the next wait (8s) does not, so the
    # third 429 propagates. Total sleep stays within the budget.
    assert recorded_sleeps == [2.0, 4.0]
    assert docs.execute_calls == 3
    assert sum(recorded_sleeps) <= 10.0


def test_budget_is_shared_across_calls(recorded_sleeps):
    """One budget covers every write it is passed to: the second call's
    429s draw from the allowance the first call already spent (this is
    the property the budget-threading through _execute_phases gives a
    whole transplant; two direct calls model it without the machinery)."""
    budget = ct._RateLimitBudget(seconds=7.0)

    # Call 1: one 429 -> waits 2s (attempt 1; 5s left) -> succeeds.
    docs = _FlakyDocs(failures=1)
    ct._batch_update(docs, "DOC1", [{"a": {}}], budget=budget)
    assert recorded_sleeps == [2.0]

    # Call 2, same budget: attempt 2 wants 4s (fits; 1s left), attempt 3
    # wants 8s (does not fit) -> the second 429 propagates.
    docs = _FlakyDocs(failures=2)
    with pytest.raises(HttpError):
        ct._batch_update(docs, "DOC1", [{"b": {}}], budget=budget)
    assert recorded_sleeps == [2.0, 4.0]


def test_non_429_write_errors_stay_single_shot(recorded_sleeps):
    """A 500 on a write may have partially executed: no retry, no sleep
    (the pre-N2 contract, unchanged)."""
    docs = _FlakyDocs(failures=1, status=500)
    with pytest.raises(HttpError):
        ct._batch_update(docs, "DOC1", [{"insertText": {}}])
    assert docs.execute_calls == 1
    assert recorded_sleeps == []


def test_execute_tab_transplant_threads_one_budget_and_governor(
    monkeypatch, recorded_sleeps
):
    """The public entry creates ONE budget and resolves ONE per-user
    governor, pushing both through the phase executor into every
    _batch_update call."""
    seen_budgets: list[object] = []
    seen_governors: list[object] = []
    real_batch_update = ct._batch_update

    def spying_batch_update(docs, doc_id, requests, budget=None, governor=None):
        seen_budgets.append(budget)
        seen_governors.append(governor)
        return real_batch_update(
            docs, doc_id, requests, budget=budget, governor=governor
        )

    monkeypatch.setattr(ct, "_batch_update", spying_batch_update)

    docs = _FlakyDocs(failures=0)
    plan = ct.TabTransplantPlan(
        phases=[
            ct.SegmentPhase(requests=[{"a": {}}], length=1),
            ct.SegmentPhase(requests=[{"b": {}}], length=1),
        ],
        block_count=2,
    )
    document = {
        "tabs": [{
            "tabProperties": {"tabId": "t1"},
            "documentTab": {"body": {"content": [
                {"startIndex": 0, "endIndex": 2, "paragraph": {}},
            ]}},
        }],
    }
    ct.execute_tab_transplant(
        docs, "DOC1", "t1", plan, document=document, governor_key="user-G",
    )

    assert len(seen_budgets) == 2
    assert seen_budgets[0] is not None
    assert seen_budgets[0] is seen_budgets[1], "one shared budget per transplant"
    assert seen_governors[0] is not None
    assert seen_governors[0] is seen_governors[1], "one governor per transplant"
    assert seen_governors[0] is ct._governor_for("user-G"), (
        "the governor must be the per-user shared instance"
    )


# ---------------------------------------------------------------------
# Per-user cross-job write governor (A2 root fix)
# ---------------------------------------------------------------------


def _fresh_key() -> str:
    import uuid
    return f"gov-test-{uuid.uuid4()}"


def test_governor_paces_concurrent_threads(monkeypatch):
    """N threads writing through ONE user's governor space themselves at
    least the configured interval apart (reservation under a lock,
    sleep outside it)."""
    import threading
    import time as time_mod

    monkeypatch.setenv(ct._WRITE_GOVERNOR_ENV, "0.05")
    governor = ct._governor_for(_fresh_key())
    stamps: list[float] = []
    stamp_lock = threading.Lock()

    def write():
        governor.acquire()
        with stamp_lock:
            stamps.append(time_mod.monotonic())

    threads = [threading.Thread(target=write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # Generous scheduling slop: every gap must show real pacing.
    assert all(gap >= 0.03 for gap in gaps), gaps


def test_governor_keys_are_isolated_and_zero_interval_disables(monkeypatch):
    """Different users never pace each other, and interval 0 (the test
    suite's global default via conftest) makes acquire a no-op."""
    import time as time_mod

    monkeypatch.setenv(ct._WRITE_GOVERNOR_ENV, "5.0")
    a = ct._governor_for(_fresh_key())
    b = ct._governor_for(_fresh_key())
    assert a.acquire() == 0.0  # first slot is always free
    start = time_mod.monotonic()
    assert b.acquire() == 0.0, "user B must not inherit user A's reservation"
    assert time_mod.monotonic() - start < 1.0

    monkeypatch.setenv(ct._WRITE_GOVERNOR_ENV, "0")
    # Even a governor with a pending reservation stops pacing at 0.
    assert a.acquire() == 0.0
    assert a.acquire() == 0.0


def test_batch_update_takes_a_governor_slot_per_send(recorded_sleeps):
    """Every SEND acquires the governor - the first try AND each 429
    retry - so retries cannot stampede the shared quota either."""

    class _SpyGovernor:
        def __init__(self):
            self.acquires = 0

        def acquire(self):
            self.acquires += 1
            return 0.0

    governor = _SpyGovernor()
    docs = _FlakyDocs(failures=2)
    ct._batch_update(
        docs, "DOC1", [{"insertText": {}}], governor=governor,
    )
    assert docs.execute_calls == 3
    assert governor.acquires == 3, "one slot per send, retries included"
