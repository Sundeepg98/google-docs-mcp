"""In-process execution of convert jobs (T1.1 async job model).

The request handler creates a job row (``appscriptly.job_store``), then
hands the blocking converter callable to ``start_job`` here. The work
runs in a DETACHED ``asyncio.Task`` that drives the callable on a
worker thread via ``asyncio.to_thread``:

- **Client disconnect cannot kill the work.** uvicorn/Starlette cancel
  the request coroutine when the client goes away. Beware the asyncio
  trap here: cancelling a coroutine that is ``await task``-ing CANCELS
  the awaited task too (``Task.cancel`` cancels the future currently
  being awaited). The handler must therefore await through
  ``wait_for_outcome`` (an ``asyncio.shield``), which breaks that
  cancellation link: the handler's death cancels only the shield
  wrapper, and the job task keeps running and records its outcome in
  the row. (The job model also unblocks the event loop: pre-job-model
  the handler called the multi-minute converter INLINE, blocking the
  whole loop for the duration.)

- **A Fly deploy/restart DOES kill the work** - the process dies, the
  thread dies, and nothing gets to write the row. The row must not lie:
  the runner heartbeats ``heartbeat_at`` every ``HEARTBEAT_INTERVAL``
  seconds while the thread runs, so a reader deriving status sees
  ``stalled`` once the heartbeat is older than
  ``job_store.STALLED_AFTER_SECONDS``. Recovery is the client re-POST
  (fingerprint attach re-arms the same row - see routes/convert.py).

- **Outcome recording happens exactly once, in the task.** Success
  writes ``result_json``; failure writes ``error_json`` carrying the
  SAME http_status + payload the synchronous path returns for that
  exception (``classify_convert_error``), so sync callers, async
  pollers and attach retries all see one truth.

- **Concurrency is capped application-side.** Each runner acquires a
  per-loop semaphore (size ``CONVERT_JOB_MAX_CONCURRENCY``, default 2)
  BEFORE marking the row running or touching a thread: jobs past the
  cap stay honestly ``queued`` (heartbeating while they wait, so they
  never derive stalled) until a slot frees. Without this the only
  brake was asyncio's default thread pool (~5 workers on prod's
  1-vCPU / 512MB machine), and one batch could run 5 genuinely
  concurrent multi-minute converts - the OOM class #226 fixed.

- **Rate-limit deaths requeue instead of going terminal (A2).** A
  failure whose root cause is the per-user Docs write quota (HTTP 429
  past the backoff budget) re-arms the row - honestly ``queued``,
  heartbeating, concurrency slot RELEASED - waits out the quota window
  (``CONVERT_JOB_REQUEUE_DELAY_SECONDS``, default 65s), then re-runs
  the work, up to ``CONVERT_JOB_MAX_REQUEUES`` (default 1) times. The
  retry gets the prior attempt's recovery envelope so the convert
  closure trashes the superseded partial doc before rebuilding from
  the intact source. The per-user write GOVERNOR in
  ``services/docs/content_transplant.py`` is the root fix that makes
  this net rarely needed.

The module keeps a strong reference to every live task (asyncio holds
only weak refs; an unreferenced task can be garbage-collected mid-run)
plus a job_id -> Task map so the sync response path and fingerprint
attaches can ``await`` an already-running job.
"""
from __future__ import annotations

import asyncio
import logging
import os
import weakref
from typing import Any, Callable

from googleapiclient.errors import HttpError

from appscriptly import job_store
from appscriptly.errors import friendly_http_error_message

log = logging.getLogger("appscriptly.http.jobs")

# Seconds between heartbeat_at bumps while the converter thread runs.
# Must stay well under job_store.STALLED_AFTER_SECONDS (120s); at 30s a
# job is declared stalled after roughly four missed beats. Tests shrink
# this via monkeypatch.
HEARTBEAT_INTERVAL: float = 30.0

# Default application-level cap on genuinely concurrent converter runs;
# override with the CONVERT_JOB_MAX_CONCURRENCY env var. Sized for the
# prod shared-cpu-1x / 512MB machine: two concurrent multi-minute
# converts fit; five (the default-executor ceiling) OOM.
_DEFAULT_MAX_CONCURRENCY = 2


def _max_concurrency() -> int:
    """CONVERT_JOB_MAX_CONCURRENCY, default 2, floor 1.

    Read at semaphore construction (not module import) so operators and
    tests can set the env var without an import-order dance.
    """
    raw = os.environ.get("CONVERT_JOB_MAX_CONCURRENCY", "")
    try:
        value = int(raw) if raw else _DEFAULT_MAX_CONCURRENCY
    except ValueError:
        log.warning(
            "invalid CONVERT_JOB_MAX_CONCURRENCY=%r; using default %d",
            raw, _DEFAULT_MAX_CONCURRENCY,
        )
        value = _DEFAULT_MAX_CONCURRENCY
    return max(1, value)


# One semaphore PER EVENT LOOP, weak-keyed so a torn-down loop drops its
# entry. asyncio primitives bind to the loop they first await on: prod
# has exactly one loop (one semaphore), while the test suite's many
# short-lived loops each get their own instead of tripping "Future
# attached to a different loop".
_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _concurrency_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_max_concurrency())
        _SEMAPHORES[loop] = sem
    return sem

# job_id -> live Task. Doubles as the strong-reference registry that
# keeps the detached tasks alive (asyncio.all_tasks holds weak refs
# only) and as the lookup the sync/attach paths use to await a running
# job. Entries self-remove on completion.
#
# Outcome tuples returned by the runner task (and therefore by any
# ``await`` on it):
#   ("done", result_dict, None)
#   ("error", http_status_int, payload_dict)
_TASKS: dict[str, "asyncio.Task[tuple[str, Any, Any]]"] = {}


def classify_convert_error(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """Map a converter exception to (http_status, response_payload).

    This is the EXACT mapping the pre-job-model synchronous handler
    implemented as except-clauses; it lives here so the job runner can
    persist the classification and every read path (sync response,
    status endpoint, attach) replays it identically.
    """
    if isinstance(exc, (FileNotFoundError, ValueError)):
        return 400, {"error": str(exc)}
    if isinstance(exc, HttpError):
        return 502, {
            "error": friendly_http_error_message(exc),
            "status_code": exc.status_code,
        }
    if isinstance(exc, RuntimeError):
        return 500, {"error": str(exc)}
    # Anything else was previously an unhandled 500 from the ASGI
    # server; record it as a generic 500 so the row never lies as
    # eternally "running" after an unexpected exception type.
    return 500, {"error": f"{type(exc).__name__}: {exc}"}


def get_task(job_id: str) -> "asyncio.Task[tuple[str, Any, Any]] | None":
    """The live task for a job, or None once it finished (row has truth)."""
    return _TASKS.get(job_id)


async def wait_for_outcome(
    task: "asyncio.Task[tuple[str, Any, Any]]",
) -> tuple[str, Any, Any]:
    """Await a job task WITHOUT linking the awaiter's fate to it.

    ``await task`` from a request coroutine would let a client
    disconnect (which cancels the request coroutine) cancel the job
    itself - ``Task.cancel`` cancels whatever future the task is
    currently awaiting. ``asyncio.shield`` breaks the link: cancelling
    the awaiter cancels only the shield wrapper; the job task runs on
    and records its outcome. Every sync/attach await of a job task MUST
    go through here.
    """
    return await asyncio.shield(task)


def start_job(
    job_id: str, work: Callable[[dict[str, Any] | None], dict[str, Any]]
) -> "asyncio.Task[tuple[str, Any, Any]]":
    """Spawn the detached runner task for ``work`` and register it.

    ``work`` is a one-arg BLOCKING callable (the converter closure,
    owning its temp-file cleanup); it runs on a worker thread. The
    argument is the PREVIOUS attempt's failure envelope when the runner
    requeues a rate-limited job (A2), or None on the first attempt -
    the closure uses it to trash the superseded partial doc before
    re-running. Await the returned task ONLY via ``wait_for_outcome`` -
    a bare ``await`` would propagate the awaiter's cancellation into
    the job (see ``wait_for_outcome``).

    Note on log correlation: ``asyncio.create_task`` copies the current
    contextvars, so log lines emitted by the runner carry the
    request_id of the request that CREATED the job - the right
    correlation for "who started this".
    """
    task = asyncio.create_task(
        _run_job(job_id, work), name=f"convert-job-{job_id}"
    )
    _TASKS[job_id] = task

    def _discard(_t: "asyncio.Task[tuple[str, Any, Any]]", *, _jid: str = job_id) -> None:
        _TASKS.pop(_jid, None)

    task.add_done_callback(_discard)
    return task


def _touch_heartbeat_safely(job_id: str) -> None:
    """Heartbeat write that can never kill the runner.

    A transient SQLite hiccup (volume pressure, WAL lock timeout) on a
    beat must not crash the runner task: that would orphan the still
    running converter thread with nobody left to record its outcome.
    Skipping one beat is harmless - the stalled threshold tolerates
    roughly four missed beats.
    """
    try:
        job_store.touch_heartbeat(job_id)
    except Exception as exc:  # noqa: BLE001 - deliberate: transient, retried next beat
        log.warning("convert job %s heartbeat write failed: %s", job_id, exc)


def _max_requeues() -> int:
    """CONVERT_JOB_MAX_REQUEUES, default 1, floor 0.

    How many times a rate-limit-exhausted job re-runs before its
    failure becomes terminal. One is enough once the write governor
    paces the storm; the env var exists for operators, not tuning
    enthusiasm."""
    raw = os.environ.get("CONVERT_JOB_MAX_REQUEUES", "")
    try:
        value = int(raw) if raw else 1
    except ValueError:
        log.warning("invalid CONVERT_JOB_MAX_REQUEUES=%r; using 1", raw)
        value = 1
    return max(0, value)


def _requeue_delay_seconds() -> float:
    """CONVERT_JOB_REQUEUE_DELAY_SECONDS, default 65 (the write quota
    refills per minute; 65s guarantees a fresh window), floor 0."""
    raw = os.environ.get("CONVERT_JOB_REQUEUE_DELAY_SECONDS", "")
    try:
        value = float(raw) if raw else 65.0
    except ValueError:
        log.warning(
            "invalid CONVERT_JOB_REQUEUE_DELAY_SECONDS=%r; using 65", raw
        )
        value = 65.0
    return max(0.0, value)


def _is_rate_limited_failure(exc: BaseException) -> bool:
    """True when a RAISED converter failure is a Docs 429 underneath.

    Covers the raw HttpError (transplant budget exhaustion), the
    docx_import pre-transplant RuntimeError wrap (``rate_limited``
    attribute + ``__cause__`` chain), and anything else that carries
    the marker."""
    if getattr(exc, "rate_limited", False):
        return True
    seen: BaseException | None = exc
    while seen is not None:
        if (
            isinstance(seen, HttpError)
            and getattr(seen, "status_code", None) == 429
        ):
            return True
        seen = seen.__cause__
    return False


async def _sleep_with_heartbeat(job_id: str, seconds: float) -> None:
    """Requeue delay that keeps the row's heartbeat fresh throughout,
    so a waiting-to-retry job polls as honestly ``queued``, never
    ``stalled``."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, HEARTBEAT_INTERVAL)
        await asyncio.sleep(chunk)
        remaining -= chunk
        _touch_heartbeat_safely(job_id)


async def _run_attempt(
    job_id: str,
    work: Callable[[dict[str, Any] | None], dict[str, Any]],
    prior_envelope: dict[str, Any] | None,
) -> tuple[str, Any]:
    """One converter attempt: slot wait + heartbeats + worker thread.

    Returns ``("result", converter_dict)`` or ``("exception", exc)``;
    terminal row writes are the CALLER's job (so a requeue decision can
    be made before anything terminal is recorded). The concurrency slot
    is held only for the duration of THIS attempt - a requeued job
    releases it during its delay so other queued jobs can run.
    """
    sem = _concurrency_semaphore()
    acquired = False
    # Waiting for a slot reuses the same wait-with-heartbeat shape as
    # the converter loop below (rather than wait_for, whose cancel-on-
    # timeout interacts badly with Semaphore.acquire wakeups).
    slot_waiter = asyncio.create_task(sem.acquire())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {slot_waiter}, timeout=HEARTBEAT_INTERVAL
            )
            _touch_heartbeat_safely(job_id)
            if done:
                slot_waiter.result()
                acquired = True
                break

        try:
            job_store.mark_running(job_id)
        except Exception as exc:  # noqa: BLE001 - status-write failure must not orphan the run
            # The row temporarily under-reports as queued; the converter
            # still runs and the terminal write below sets the truth.
            log.warning(
                "convert job %s mark_running write failed: %s", job_id, exc
            )
        log.info("convert job %s started", job_id)
        thread_task = asyncio.create_task(
            asyncio.to_thread(work, prior_envelope)
        )
        while True:
            done, _pending = await asyncio.wait(
                {thread_task}, timeout=HEARTBEAT_INTERVAL
            )
            # Bump the heartbeat even on the final pass - harmless, and
            # it keeps "fresh heartbeat" true up to the terminal write.
            _touch_heartbeat_safely(job_id)
            if done:
                break
        try:
            return ("result", thread_task.result())
        except Exception as exc:  # noqa: BLE001 - the caller classifies/persists
            return ("exception", exc)
    finally:
        if acquired:
            sem.release()
        else:
            # Cancelled (or failed) before holding a slot: make sure the
            # pending acquire cannot consume a permit with nobody left
            # to release it. If it ALREADY resolved between the last
            # check and this cancel, hand the permit straight back.
            slot_waiter.cancel()
            if slot_waiter.done() and not slot_waiter.cancelled():
                try:
                    if slot_waiter.result():
                        sem.release()
                except Exception:  # noqa: BLE001
                    pass


async def _run_job(
    job_id: str, work: Callable[[dict[str, Any] | None], dict[str, Any]]
) -> tuple[str, Any, Any]:
    """Drive one job to completion, requeuing rate-limit deaths (A2).

    Each attempt runs via ``_run_attempt`` (slot + heartbeats + worker
    thread). A failure whose root cause is the Docs write quota (HTTP
    429) is TRANSIENT by definition - the quota refills every minute -
    so instead of recording it terminally the runner re-arms the row
    (status honestly back to ``queued``, heartbeating through the
    delay) and re-runs the work after ``CONVERT_JOB_REQUEUE_DELAY_
    SECONDS``, up to ``CONVERT_JOB_MAX_REQUEUES`` times. The retry
    receives the prior attempt's envelope so the convert closure can
    trash the superseded partial doc before rebuilding from the intact
    source. Every other failure keeps the N1/N3 terminal semantics
    exactly.

    Never raises on converter failure (the outcome tuple + row carry
    it). CancelledError (uvicorn shutdown / event-loop teardown) is
    deliberately NOT caught: the row simply stops heartbeating and
    derives ``stalled``, which is the documented deploy-kill semantics.
    The already-running converter thread cannot be interrupted; if it
    completes against Google after the loop died, the row still reads
    stalled - see job_store's module docstring for that honest limit.
    Store writes are best-effort (a transient DB error must not kill
    the runner and orphan the converter thread).
    """
    attempt = 0
    prior_envelope: dict[str, Any] | None = None
    while True:
        signal, value = await _run_attempt(job_id, work, prior_envelope)

        if signal == "exception":
            exc = value
            if _is_rate_limited_failure(exc) and attempt < _max_requeues():
                attempt += 1
                prior_envelope = None  # pre-transplant death: no doc kept
                await _requeue(job_id, attempt, cause=str(exc))
                continue
            http_status, payload = classify_convert_error(exc)
            if attempt:
                payload = {**payload, "requeue_attempts": attempt}
            try:
                job_store.finish_error(job_id, http_status, payload)
            except Exception as store_exc:  # noqa: BLE001
                # Row stays running -> derives stalled -> retry re-arms.
                log.error(
                    "convert job %s finish_error write failed: %s",
                    job_id, store_exc,
                )
            log.warning(
                "convert job %s failed (http %d): %s",
                job_id, http_status, payload.get("error"),
            )
            return ("error", http_status, payload)

        result = value
        # N3 (2026-07-10 retest): a converter that RETURNED a kept-doc
        # recovery envelope carrying ``error`` (the S2.5 partial-failure
        # contract) is a FAILED job, and the row must say so - a poller
        # reading status=="done" must be able to trust it as success.
        # A2 refinement: when that envelope is rate-limit-caused it is
        # requeued instead (the source is intact; the retry closure
        # trashes the superseded partial doc).
        if isinstance(result, dict) and result.get("error"):
            if result.get("rate_limited") and attempt < _max_requeues():
                attempt += 1
                prior_envelope = result
                await _requeue(
                    job_id, attempt, cause=str(result.get("error"))[:200]
                )
                continue
            payload = (
                {**result, "requeue_attempts": attempt} if attempt else result
            )
            try:
                job_store.finish_error(job_id, 500, payload)
            except Exception as store_exc:  # noqa: BLE001
                log.error(
                    "convert job %s finish_error write failed: %s",
                    job_id, store_exc,
                )
            log.warning(
                "convert job %s failed (partial-failure envelope): %s",
                job_id, result.get("error"),
            )
            return ("error", 500, payload)

        try:
            job_store.finish_done(job_id, result)
        except Exception as store_exc:  # noqa: BLE001
            # Best effort: record SOMETHING terminal rather than leave
            # the row lying "running" when the store recovers.
            log.error(
                "convert job %s finish_done write failed: %s",
                job_id, store_exc,
            )
            try:
                job_store.finish_error(
                    job_id, 500,
                    {"error": f"result could not be persisted: {store_exc}"},
                )
            except Exception:  # noqa: BLE001
                pass  # stalled derivation + fingerprint re-arm recover
        log.info("convert job %s done", job_id)
        return ("done", result, None)


async def _requeue(job_id: str, attempt: int, *, cause: str) -> None:
    """Re-arm the row and wait out the quota window before retrying.

    Status honesty: the row reads ``queued`` (never a lying done or a
    premature error) and heartbeats through the whole delay. Keeps the
    N2 budget-countdown logging style."""
    delay = _requeue_delay_seconds()
    log.warning(
        "convert job %s hit the write rate limit; requeue %d/%d after "
        "%.0fs (cause: %s)",
        job_id, attempt, _max_requeues(), delay, cause,
    )
    try:
        job_store.rearm_job(job_id)
    except Exception as store_exc:  # noqa: BLE001
        # Non-fatal: the retry still runs; the row just over-reports
        # running for the delay (heartbeats keep it un-stalled).
        log.warning(
            "convert job %s requeue rearm write failed: %s",
            job_id, store_exc,
        )
    await _sleep_with_heartbeat(job_id, delay)
