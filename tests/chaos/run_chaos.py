"""Chaos harness for cloud-MCP resilience scenarios (v1.4.0b).

Standalone CLI — argparse-driven, JSON-emitting, exit-code-respecting.
NOT a pytest module on purpose: scenarios are long-running and need
their own process / dir / DB; pytest's fixtures and discovery would
get in the way. See ``chaos_plan.md`` for the operator-facing
scenario catalogue.

Run:

    uv run python tests/chaos/run_chaos.py \
        --scenarios all --max-duration 30s --json-output out.json

Exit codes:
    0 — all scenarios passed
    1 — at least one scenario failed (or harness errored)
    2 — CLI arg error
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------
# argparse glue
# ---------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$")
_SCENARIOS_AVAILABLE = ["S1"]


def _parse_duration(text: str) -> float:
    """Accept human shorthand: ``500ms``, ``30s``, ``5m``, ``1h``, ``45``."""
    m = _DURATION_RE.match(text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid duration {text!r}: expected e.g. 30s, 500ms, 5m, 1h"
        )
    value = float(m.group(1))
    unit = m.group(2) or "s"
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return value * multiplier


def _parse_scenarios(text: str) -> list[str]:
    if text.strip().lower() == "all":
        return list(_SCENARIOS_AVAILABLE)
    parts = [p.strip().upper() for p in text.split(",") if p.strip()]
    unknown = [p for p in parts if p not in _SCENARIOS_AVAILABLE]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown scenarios: {unknown}; "
            f"available: {_SCENARIOS_AVAILABLE} or 'all'"
        )
    return parts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cloud MCP chaos harness (v1.4.0b).",
    )
    p.add_argument(
        "--scenarios",
        type=_parse_scenarios,
        default=["S1"],
        help="Comma-separated scenario IDs (e.g. 'S1,S2') or 'all'. "
             f"Available: {_SCENARIOS_AVAILABLE}",
    )
    p.add_argument(
        "--max-duration",
        type=_parse_duration,
        default=30.0,
        help="Soft cap on per-scenario runtime (e.g. 30s, 500ms). Default: 30s.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Concurrent worker threads per scenario. Default: 16.",
    )
    p.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="Write the full report to this JSON file. Stdout always "
             "prints a summary.",
    )
    p.add_argument(
        "--p99-budget-ms",
        type=float,
        default=500.0,
        help="Per-op p99 latency budget. Above this S1 fails. Default: 500ms "
             "(generous to absorb dev-laptop noise).",
    )
    return p


# ---------------------------------------------------------------------
# Scenario S1 — concurrent user_store saturation
# ---------------------------------------------------------------------


def _run_s1(
    *,
    max_duration: float,
    workers: int,
    p99_budget_ms: float,
) -> dict[str, Any]:
    """Concurrent read-modify-write hammer on user_store.

    Each worker owns a distinct user_id, runs save_state → get_state →
    save_state in a tight loop until ``max_duration`` elapses. Records
    every op's latency and any exception. At end, verifies each
    worker's last write is intact (no torn write, no cross-worker
    bleed).
    """
    # Isolated SQLite path so we don't trample the developer's real DB.
    tmpdir = Path(tempfile.mkdtemp(prefix="chaos_s1_"))
    db_path = tmpdir / "user_state.db"
    prior_path = os.environ.get("GOOGLE_DOCS_USER_STORE_PATH")
    prior_data = os.environ.get("GOOGLE_DOCS_DATA_DIR")
    os.environ["GOOGLE_DOCS_USER_STORE_PATH"] = str(db_path)
    os.environ["GOOGLE_DOCS_DATA_DIR"] = str(tmpdir)

    # Clear the module-level path cache so the new path actually inits.
    # Late import: only pull in user_store after env is set, so
    # default_data_dir() picks up the override.
    from appscriptly import user_store
    user_store._initialized_paths.clear()

    latencies_ms: list[float] = []
    errors: list[str] = []
    ops_total = 0
    _final_values: dict[str, str] = {}  # populated by workers in finally
    lock = threading.Lock()  # guards latencies_ms / errors / ops_total

    stop_at = time.monotonic() + max_duration
    deadline_reached = threading.Event()

    def worker(idx: int) -> None:
        nonlocal ops_total
        user_id = f"chaos-s1-user-{idx:04d}"
        local_ops = 0
        local_lat: list[float] = []
        local_errs: list[str] = []
        last_value = ""
        try:
            while time.monotonic() < stop_at:
                local_ops += 1
                value = (
                    f"https://script.google.com/macros/s/"
                    f"chaos{idx:04d}iter{local_ops}/exec"
                )
                start = time.perf_counter()
                try:
                    user_store.save_state(user_id, {"apps_script_url": value})
                    state = user_store.get_state(user_id)
                    if state.get("apps_script_url") != value:
                        # Worker just wrote `value` and read back something
                        # else — that's the torn-write/cross-worker bleed
                        # we're hunting for.
                        local_errs.append(
                            f"worker {idx} read back stale or cross-worker "
                            f"value: wrote={value!r} got={state.get('apps_script_url')!r}"
                        )
                    last_value = value
                except Exception as e:  # noqa: BLE001 — record-all by design
                    local_errs.append(
                        f"worker {idx} op {local_ops}: {type(e).__name__}: {e}"
                    )
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                local_lat.append(elapsed_ms)
        finally:
            with lock:
                ops_total += local_ops
                latencies_ms.extend(local_lat)
                errors.extend(local_errs)
            # Stash the final value for post-run verification.
            _final_values[user_id] = last_value

    threads = [
        threading.Thread(target=worker, args=(i,), name=f"chaos-s1-{i}")
        for i in range(workers)
    ]
    started_at = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        # Generous join timeout — slightly above the soft cap.
        t.join(timeout=max_duration + 10.0)
        if t.is_alive():
            errors.append(f"thread {t.name} did not finish within join timeout")
    duration = time.monotonic() - started_at
    deadline_reached.set()

    # Post-run verification: every worker's last_value must survive.
    for user_id, expected in _final_values.items():
        if not expected:
            continue  # worker never completed an iteration
        try:
            state = user_store.get_state(user_id)
            actual = state.get("apps_script_url")
            if actual != expected:
                errors.append(
                    f"post-run verify failed for {user_id}: "
                    f"expected={expected!r} actual={actual!r}"
                )
        except Exception as e:  # noqa: BLE001
            errors.append(
                f"post-run verify raised for {user_id}: "
                f"{type(e).__name__}: {e}"
            )

    # Restore env so the process can keep being used (matters in tests).
    if prior_path is None:
        os.environ.pop("GOOGLE_DOCS_USER_STORE_PATH", None)
    else:
        os.environ["GOOGLE_DOCS_USER_STORE_PATH"] = prior_path
    if prior_data is None:
        os.environ.pop("GOOGLE_DOCS_DATA_DIR", None)
    else:
        os.environ["GOOGLE_DOCS_DATA_DIR"] = prior_data

    # Reset the cache so subsequent runs of the harness in the same
    # process don't carry the tmp path across.
    user_store._initialized_paths.clear()

    # Best-effort cleanup of the tmpdir.
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    ops_failed = len(errors)
    summary: dict[str, Any] = {
        "ops_total": ops_total,
        "ops_failed": ops_failed,
        "workers": workers,
        "duration_seconds": round(duration, 3),
        "errors": errors[:20],  # cap the report so it doesn't grow unbounded
        "errors_truncated": len(errors) > 20,
    }
    if latencies_ms:
        sorted_lat = sorted(latencies_ms)
        n = len(sorted_lat)
        summary["latency_ms_p50"] = round(
            statistics.median(sorted_lat), 3
        )
        summary["latency_ms_p95"] = round(
            sorted_lat[min(n - 1, int(n * 0.95))], 3
        )
        summary["latency_ms_p99"] = round(
            sorted_lat[min(n - 1, int(n * 0.99))], 3
        )
        summary["latency_ms_max"] = round(sorted_lat[-1], 3)
    else:
        summary["latency_ms_p50"] = None
        summary["latency_ms_p95"] = None
        summary["latency_ms_p99"] = None
        summary["latency_ms_max"] = None

    # Pass / fail decision.
    p99 = summary.get("latency_ms_p99")
    if ops_failed > 0:
        status = "fail"
    elif p99 is not None and p99 > p99_budget_ms:
        status = "fail"
        summary["errors"].append(
            f"p99 latency {p99}ms exceeds budget {p99_budget_ms}ms"
        )
    elif ops_total == 0:
        status = "fail"
        summary["errors"].append("no ops completed — harness defect")
    else:
        status = "pass"
    summary["status"] = status
    return summary


_SCENARIO_RUNNERS = {
    "S1": _run_s1,
}


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "version": "1.4.0b",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "max_duration_requested_seconds": args.max_duration,
        "scenarios": {},
    }

    overall_pass = True
    for scenario in args.scenarios:
        runner = _SCENARIO_RUNNERS[scenario]
        print(
            f"[chaos] running {scenario} "
            f"(max_duration={args.max_duration}s, workers={args.workers})",
            flush=True,
        )
        try:
            result = runner(
                max_duration=args.max_duration,
                workers=args.workers,
                p99_budget_ms=args.p99_budget_ms,
            )
        except Exception as e:  # noqa: BLE001 — harness defect, not a scenario fail
            result = {
                "status": "fail",
                "errors": [
                    f"harness raised: {type(e).__name__}: {e}",
                    traceback.format_exc(),
                ],
            }
        report["scenarios"][scenario] = result
        print(
            f"[chaos] {scenario}: {result['status']} "
            f"(ops={result.get('ops_total', '?')}, "
            f"failed={result.get('ops_failed', '?')}, "
            f"p99={result.get('latency_ms_p99', '?')}ms)",
            flush=True,
        )
        if result["status"] != "pass":
            overall_pass = False

    report["overall_status"] = "pass" if overall_pass else "fail"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, indent=2))
        print(f"[chaos] wrote report to {args.json_output}", flush=True)

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
