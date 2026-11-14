"""Run traces and agent health over HTTP (AP-P4, BOSSAPP-1055; AGENT_PLATFORM §4.8).

The data the console's Run trace (wireframe A6) and Health (A5) screens draw:

  GET /v1/runs/{run_id}/trace               the run as a tree of act-parented spans, each with its
                                            place on the timeline (offset_pct/width_pct), cost and
                                            tokens (from llm_calls: the spans sum to the run's
                                            llm_calls total, `totals.reconciled`), child runs
  GET /v1/decisions/{decision_id}/trace     the run behind a Decision card, and its tree — the
                                            "open the trace from the decision" path (two hops)
  GET /v1/spans/{span_id}                   one span with its redacted input and output
  GET /v1/observability/agent-health        per agent: runs, ok rate, p95, cost, last run, the
                                            last failed run to open; online evaluators' week

Reads only; `observability:read` at the project.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


def build_traces_router(*, engine, require_reader: Callable[..., None]) -> APIRouter:
    from boss_engine.services import online_evaluators, traces

    router = APIRouter(tags=["observability"])

    @router.get("/v1/runs/{run_id}/trace")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def run_trace(run_id: str, project_id: str, depth: int = 2) -> dict[str, Any]:
        """The run's span tree (see the module docstring), child runs to `depth` levels."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            got = traces.run_trace(connection, project_id, run_id, depth=max(1, min(depth, 4)))
        if got is None:
            raise HTTPException(status_code=404, detail="BOSS has no trace of that run.")
        return got

    @router.get("/v1/decisions/{decision_id}/trace")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def decision_trace(decision_id: str, project_id: str, depth: int = 2) -> dict[str, Any]:
        """The run that produced this Decision, and its tree: decision → run → spans."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            decision = connection.execute(text(
                "SELECT id, question, status, created_at FROM decisions "
                "WHERE id = :d AND project_id = :p"),
                {"d": decision_id, "p": project_id}).mappings().first()
            if decision is None:
                raise HTTPException(status_code=404, detail="Decision not found.")
            found = traces.run_for_decision(connection, project_id, decision_id)
            tree = traces.run_trace(connection, project_id, found["run_id"],
                                    depth=max(1, min(depth, 4))) if found else None
        out = {"decision": {"id": decision["id"], "question": decision["question"],
                            "status": decision["status"],
                            "created_at": decision["created_at"].isoformat()},
               "run_id": (found or {}).get("run_id"), "found_by": (found or {}).get("found_by"),
               "trace": tree}
        if tree is None:
            out["note"] = ("No run is linked to this Decision (a person raised it, or it "
                           "predates traces).") if found is None else \
                "The run is known but BOSS holds no trace of it."
        return out

    @router.get("/v1/spans/{span_id}")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def span(span_id: str, project_id: str) -> dict[str, Any]:
        """One span with its redacted input and output (hot from the database, cold from the
        payload store; an expired payload says so)."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            got = traces.span_detail(connection, project_id, span_id)
        if got is None:
            raise HTTPException(status_code=404, detail="Span not found.")
        return got

    @router.get("/v1/observability/agent-health")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def agent_health(project_id: str, hours: int = 24) -> dict[str, Any]:
        """Is every agent alive, fast and within budget? Per agent over the window, and each
        online evaluator's week (paused at its cap or not)."""
        hours = max(1, min(int(hours), 24 * 30))
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"hours": hours,
                    "agents": traces.agent_health(connection, project_id, hours=hours),
                    "evaluators": online_evaluators.weeks(connection, project_id)}

    return router


__all__ = ["build_traces_router"]
