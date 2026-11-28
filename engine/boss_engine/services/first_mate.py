"""PE-Phase 3 (BOSSAPP-791): First mate parallel fan-out (§3H), objective loops (§3J), tool output budgets (§3I).

First mate (§3H):
  - Thin client over the existing agent pipeline (never a second engine) that splits a multi-repo
    or multi-app request into independent tasks, executes them in parallel, sweeps actionable
    backlog items, and returns one consolidated progress report.

Objective loops (§3J):
  - Workflow primitive with an objective, stop condition, and hard caps (max_iterations,
    max_tokens, max_cost_usd, max_seconds), returning a structured list of PRs and evidence.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


def fan_out_multi_repo(
    request: str,
    targets: Sequence[Mapping[str, Any]],
    *,
    dispatch_fn: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    max_workers: int = 4,
) -> dict[str, Any]:
    """Fan out a multi-repo / multi-app request in parallel and consolidate into one report (§3H)."""
    items = [dict(t) for t in (targets or ()) if isinstance(t, Mapping)]
    if not items:
        return {
            "request": request,
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "summary": "No targets to dispatch.",
        }

    results: list[dict[str, Any]] = [{} for _ in items]
    workers = max(1, min(int(max_workers), len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(dispatch_fn, {**item, "request": request}): idx
            for idx, item in enumerate(items)
        }
        for fut in as_completed(future_map):
            idx = future_map[fut]
            target = items[idx]
            label = str(target.get("repo") or target.get("app") or target.get("id") or f"target_{idx}")
            try:
                res = dict(fut.result() or {})
                ok = bool(res.get("ok", True))
                results[idx] = {"target": label, "ok": ok, **res}
            except Exception as exc:  # noqa: BLE001 — isolate per-target failures
                results[idx] = {"target": label, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded
    lines = [f"First mate report for: {request} ({succeeded}/{len(results)} succeeded)"]
    for r in results:
        mark = "OK" if r.get("ok") else "FAILED"
        detail = r.get("pr_url") or r.get("error") or r.get("status") or ""
        lines.append(f"- [{mark}] {r['target']}: {detail}")

    return {
        "request": request,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
        "summary": "\n".join(lines),
    }


_PRIO_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}


def sweep_backlog(
    items: Sequence[Mapping[str, Any]],
    *,
    max_items: int = 10,
) -> list[dict[str, Any]]:
    """Select actionable, unblocked backlog items ordered by priority (§3H)."""
    actionable = [
        dict(i)
        for i in (items or ())
        if isinstance(i, Mapping)
        and str(i.get("status") or "todo").lower() in ("backlog", "todo", "open")
        and not i.get("blocked")
    ]
    actionable.sort(
        key=lambda i: (
            _PRIO_RANK.get(str(i.get("priority") or "none").lower(), 4),
            str(i.get("id") or i.get("key") or ""),
        )
    )
    return actionable[: max(1, int(max_items))]


@dataclass(frozen=True)
class ObjectiveLoopSpec:
    objective: str
    stop_condition: str
    max_iterations: int = 5
    max_tokens: int = 200_000
    max_cost_usd: float = 10.0
    max_seconds: float = 3600.0


@dataclass
class ObjectiveLoopReport:
    objective: str
    stopped_by: str
    iterations: int
    tokens_used: int
    cost_usd: float
    prs: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


def run_objective_loop(
    spec: ObjectiveLoopSpec,
    *,
    iterate_fn: Callable[[int, list[dict[str, Any]]], Mapping[str, Any]],
    clock: Callable[[], float] = time.monotonic,
) -> ObjectiveLoopReport:
    """Execute a bounded unattended objective loop (§3J), stopping on condition or any hard cap."""
    started = clock()
    tokens_used = 0
    cost_usd = 0.0
    prs: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    stopped_by = "max_iterations"
    iterations = 0

    for idx in range(1, max(1, int(spec.max_iterations)) + 1):
        if clock() - started >= spec.max_seconds:
            stopped_by = "max_seconds"
            break
        if tokens_used >= spec.max_tokens:
            stopped_by = "max_tokens"
            break
        if cost_usd >= spec.max_cost_usd:
            stopped_by = "max_cost_usd"
            break

        iterations = idx
        step = dict(iterate_fn(idx, list(evidence)) or {})
        tokens_used += int(step.get("tokens") or 0)
        cost_usd += float(step.get("cost_usd") or 0.0)
        if step.get("pr"):
            prs.append(dict(step["pr"]))
        evidence.append({
            "iteration": idx,
            "summary": str(step.get("summary") or ""),
            "stop_met": bool(step.get("stop_met", False)),
            **({"metric": step["metric"]} if "metric" in step else {}),
        })
        if step.get("stop_met"):
            stopped_by = "stop_condition"
            break

    return ObjectiveLoopReport(
        objective=spec.objective,
        stopped_by=stopped_by,
        iterations=iterations,
        tokens_used=tokens_used,
        cost_usd=round(cost_usd, 6),
        prs=prs,
        evidence=evidence,
    )


__all__ = [
    "ObjectiveLoopReport",
    "ObjectiveLoopSpec",
    "fan_out_multi_repo",
    "run_objective_loop",
    "sweep_backlog",
]
