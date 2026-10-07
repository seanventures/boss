"""Routing overhead (docs/product/MODEL_ROUTER.md, "Efficiency"): with no network on the hot
path, routing is pure computation over a snapshot plus the classifier's cache. The design target
is p95 well under 50 ms; this asserts p95 < 5 ms over thousands of varied routes (a ceiling
that leaves a slow CI machine room) and prints the measured numbers.
"""
from __future__ import annotations

import statistics
import time

from ._router_fixtures import claude, metered, snap
from boss_engine.services.model_router.classifier import CLASSIFIER
from boss_engine.services.model_router.escalation import EscalationLedger
from boss_engine.services.model_router.router import RouteRequest, Router

STEPS = ("plan", "write_tests", "implement", "review_code", "security_check", "docs")
TASKS = [
    "Fix the typo in the existing footer label.",
    "Build a new full-stack app for tracking leads with a React frontend and a FastAPI backend.",
    "Add OAuth login with refresh tokens to the existing app.",
    "Update the existing CSV export to include the created date column.",
    "Redesign the architecture of the existing scheduler for concurrency.",
]


def _requests(n: int):
    for i in range(n):
        task = TASKS[i % len(TASKS)] + f" (ticket {i % 250})"
        step = STEPS[i % len(STEPS)]
        kind = "workflow_step" if step == "docs" else "build_stage"
        yield RouteRequest(kind=kind, step=step, project_id="p1", unit_id=f"r{i % 400}",
                           spec={"title": "", "instructions": task}, text=task)


def test_routing_overhead_p95():
    CLASSIFIER.clear()
    router = Router()
    snapshots = [snap(), snap(sources={"claude_code": claude(0.08),
                                       "openrouter": metered("openrouter")}), snap(budget="red")]
    ledger = EscalationLedger()
    ledger.record("implement", "tests_failed", 2)
    samples = []
    for i, request in enumerate(_requests(5000)):
        started = time.perf_counter()
        router.route(request, snapshots[i % 3], ledger=ledger if i % 7 == 0 else None)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[int(len(samples) * 0.95)]
    p99 = samples[int(len(samples) * 0.99)]
    print(f"\nrouting overhead over {len(samples)} routes: p50 {p50:.3f} ms, "
          f"p95 {p95:.3f} ms, p99 {p99:.3f} ms, max {samples[-1]:.3f} ms")
    assert p95 < 5.0, f"p95 {p95:.3f} ms"


def test_a_cold_classification_is_still_fast():
    CLASSIFIER.clear()
    samples = []
    for i, request in enumerate(_requests(500)):
        CLASSIFIER.clear()
        started = time.perf_counter()
        Router().route(request, snap())
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    print(f"\ncold (uncached) routing p95 {p95:.3f} ms")
    assert p95 < 20.0
