"""#103 — load/soak harness for high-scale task execution (spec 10 §6).

The Go services (S1 sandbox pool, S2 gateway) exist to run **100s of concurrent** code tasks. You can't
tune density/backpressure without measuring throughput, the saturation point, and failure modes — so
this drives many tasks concurrently and reports those numbers.

**Cost guard:** it defaults to a **stub** runner (a simulated task with a configurable duration/failure
rate) so a load test costs **zero model tokens** and measures the *scheduling/concurrency* layer. A real
runner (fire actual code tasks) is an opt-in adapter — the point is the harness is ready to point at the
Go sandbox pool the moment it exists, and to measure the local provider today.

Pure: `percentile`/`summarize`/`stub_task` are model-free and unit-tested; `run_load`/`sweep` drive them
with a thread pool.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

# A task runner takes a 0-based index and returns {"ok": bool, "error": str|None}.
TaskFn = Callable[[int], dict[str, Any]]


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list (q in [0,1])."""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 4)


def stub_task(index: int, *, duration_s: float = 0.02, fail_every: int = 0) -> dict[str, Any]:
    """A simulated task: sleeps `duration_s`, and fails deterministically every `fail_every`-th index
    (0 = never fail). Deterministic — no randomness — so a run is reproducible."""
    time.sleep(max(0.0, duration_s))
    if fail_every and index % fail_every == 0:
        return {"ok": False, "error": "stub_injected_failure"}
    return {"ok": True, "error": None}


def summarize(results: list[dict[str, Any]], wall_clock_s: float) -> dict[str, Any]:
    """Throughput, success rate, latency percentiles, peak concurrency, and an error breakdown."""
    n = len(results)
    ok = sum(1 for r in results if r.get("ok"))
    durations = sorted(float(r.get("duration_s", 0.0)) for r in results)
    errors: dict[str, int] = {}
    for r in results:
        if not r.get("ok"):
            errors[str(r.get("error") or "unknown")] = errors.get(str(r.get("error") or "unknown"), 0) + 1
    return {
        "tasks": n, "succeeded": ok, "failed": n - ok,
        "success_rate": round(ok / n, 3) if n else None,
        "wall_clock_s": round(wall_clock_s, 3),
        "throughput_per_s": round(n / wall_clock_s, 2) if wall_clock_s > 0 else None,
        "latency_s": {
            "p50": percentile(durations, 0.5), "p90": percentile(durations, 0.9),
            "p99": percentile(durations, 0.99), "max": round(durations[-1], 4) if durations else None,
            "mean": round(sum(durations) / n, 4) if n else None,
        },
        "peak_concurrency": max((r.get("concurrency_at_start", 0) for r in results), default=0),
        "errors": errors,
    }


def run_load(task_fn: TaskFn, n: int, concurrency: int, *, timeout_s: float = 120.0) -> dict[str, Any]:
    """Run `n` tasks through `task_fn` with at most `concurrency` in flight; time each; summarize.
    Records the observed in-flight count when each task starts, so the report shows peak concurrency."""
    n = max(0, int(n))
    concurrency = max(1, int(concurrency))
    results: list[dict[str, Any]] = []
    in_flight = {"cur": 0, "peak": 0}
    import threading
    lock = threading.Lock()

    def _wrapped(i: int) -> dict[str, Any]:
        with lock:
            in_flight["cur"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["cur"])
            started_at_conc = in_flight["cur"]
        t0 = time.monotonic()
        try:
            out = task_fn(i) or {}
        except Exception as exc:  # noqa: BLE001 — a crashing task is a failure, not a harness abort
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        dur = time.monotonic() - t0
        with lock:
            in_flight["cur"] -= 1
        return {"index": i, "ok": bool(out.get("ok")), "error": out.get("error"),
                "duration_s": dur, "concurrency_at_start": started_at_conc}

    t_start = time.monotonic()
    if n:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for r in pool.map(_wrapped, range(n), timeout=timeout_s):
                results.append(r)
    wall = time.monotonic() - t_start
    report = summarize(results, wall)
    report["requested"] = {"n": n, "concurrency": concurrency}
    return report


def sweep(task_fn: TaskFn, concurrencies: list[int], n_per_level: int) -> dict[str, Any]:
    """Run `n_per_level` tasks at each concurrency level and report throughput per level — the
    **saturation point** is where throughput stops rising as concurrency increases."""
    levels = []
    for c in concurrencies:
        rep = run_load(task_fn, n_per_level, c)
        levels.append({"concurrency": c, "throughput_per_s": rep["throughput_per_s"],
                       "p90_latency_s": rep["latency_s"]["p90"], "success_rate": rep["success_rate"]})
    best = max((lvl for lvl in levels if lvl["throughput_per_s"] is not None),
               key=lambda x: x["throughput_per_s"], default=None)
    return {"levels": levels,
            "saturation_at_concurrency": best["concurrency"] if best else None,
            "peak_throughput_per_s": best["throughput_per_s"] if best else None}
