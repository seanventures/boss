"""The harness score per change (BOSSAPP-959): the latest harness benchmark run per engine and
scaffold level, against its baseline, with the verdict per metric. Read only: a benchmark is run
by scripts/harness-bench on a test database, never from the API.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from boss_engine.db import begin
from boss_engine.harness_bench import store
from boss_engine.identity import P, Scope, permit


def build_harness_bench_router(*, engine) -> APIRouter:
    router = APIRouter(tags=["harness"])

    @router.get("/v1/harness/score")
    @permit(P.ASSURANCE_READ, scope=Scope.org())
    def harness_score():
        """One row per engine and level: the latest completed run, its baseline and verdict."""
        with begin(engine) as connection:
            return {"rows": store.harness_score(connection)}

    @router.get("/v1/harness/bench-runs")
    @permit(P.ASSURANCE_READ, scope=Scope.org())
    def list_bench_runs(limit: int = Query(default=50, ge=1, le=500)):
        with begin(engine) as connection:
            return {"runs": store.list_runs(connection, limit=limit)}

    @router.get("/v1/harness/bench-runs/{run_id}")
    @permit(P.ASSURANCE_READ, scope=Scope.org())
    def read_bench_run(run_id: str):
        with begin(engine) as connection:
            report = store.load_report(connection, run_id)
        if report is None:
            raise HTTPException(404, f"no harness benchmark run {run_id}")
        return report

    return router
