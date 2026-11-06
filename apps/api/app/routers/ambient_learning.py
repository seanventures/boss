"""Continuous learning and outcomes over HTTP (BOSSAPP-911/912; design §14; engine:
services/ambient/{roadmap,outcomes}.py).

WHO MAY DO WHAT (the same seats as routers/ambient.py)
-------------------------------------------------------
* READ the Recommended roadmap and one recommendation: work_item:read. Detector stats: autonomy:read.
* REVIEW it — accept (as ticket / epic / PRD), not now, reject with a reason — and "Check now"
  (run the detectors) or sweep outcomes: work_item:manage. A person choosing roadmap work is the
  same seat that takes over or dismisses an ambient item.
* SEED the demo roadmap: autonomy:manage, and only into a demo project.
* The worker's tick: the worker principal only.

Every write attributes to the signed-in principal (require_write_actor). The routes add no
behaviour of their own: each is one call into services/ambient.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class AcceptBody(Body):
    as_: Optional[str] = Field(default=None, alias="as", pattern="^(ticket|epic|prd)$")
    note: str = Field(default="", max_length=600)
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RejectBody(Body):
    reason: str = Field(min_length=1, max_length=500)
    category: Optional[str] = Field(default=None, max_length=40)


class SnoozeBody(Body):
    until: datetime
    reason: str = Field(default="", max_length=300)


class RunBody(Body):
    only: list[str] = Field(default_factory=list, max_length=20)


def build_ambient_learning_router(*, engine, settings: Callable[[], Any],
                                  require_reader: Callable[..., None],
                                  require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services.ambient import board, ledger, outcomes, roadmap
    from boss_engine.services.ambient._common import EvidenceError

    router = APIRouter(tags=["ambient"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ledger.GateRefused as exc:
            raise HTTPException(status_code=403, detail={"refused": str(exc),
                                                         "gate": exc.verdict}) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (board.Conflict, board.LeaseLost, ledger.LedgerConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (EvidenceError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/ambient/roadmap")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def list_roadmap(project_id: str, status: str = Query("open"), kind: Optional[str] = None,
                     detector: Optional[str] = None,
                     limit: int = Query(30, ge=1, le=100)) -> dict:
        """The Recommended roadmap: ranked recommendations, each a small diagram."""
        with begin(engine) as c:
            require_reader(c, project_id)
            return _errors(lambda: roadmap.list_roadmap(c, project_id, status=status, kind=kind,
                                                        detector=detector, limit=limit))

    @router.get("/v1/ambient/roadmap/detectors")
    @permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
    def detectors(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"detectors": list(roadmap.detector_stats(c, project_id).values())}

    @router.get("/v1/ambient/roadmap/{work_item_id}")
    @permit(P.WORK_ITEM_READ, scope=Scope.of("work_item", path="work_item_id"))
    def get_one(work_item_id: str, project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return _errors(lambda: roadmap.get(c, work_item_id, project_id=project_id))

    @router.post("/v1/ambient/roadmap/{work_item_id}/accept")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def accept(work_item_id: str, payload: AcceptBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return _errors(lambda: roadmap.accept(
                c, work_item_id, project_id=payload.project_id, actor=actor, as_=payload.as_,
                note=payload.note, settings=settings()))

    @router.post("/v1/ambient/roadmap/{work_item_id}/reject")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def reject(work_item_id: str, payload: RejectBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return _errors(lambda: roadmap.reject(
                c, work_item_id, project_id=payload.project_id, actor=actor,
                reason=payload.reason, category=payload.category))

    @router.post("/v1/ambient/roadmap/{work_item_id}/snooze")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def snooze(work_item_id: str, payload: SnoozeBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return _errors(lambda: roadmap.snooze(
                c, work_item_id, project_id=payload.project_id, actor=actor,
                until=payload.until, reason=payload.reason))

    @router.post("/v1/ambient/roadmap/run")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def run_now(payload: RunBody) -> dict:
        """Check now: run this project's detectors once (the worker does it every 6 hours)."""
        with begin(engine) as c:
            require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return _errors(lambda: roadmap.run(c, payload.project_id, settings=settings(),
                                               only=payload.only or None))

    @router.post("/v1/ambient/outcomes/sweep")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def sweep_outcomes(payload: Body) -> dict:
        with begin(engine) as c:
            require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return {"outcomes": outcomes.sweep(c, project_id=payload.project_id)}

    @router.post("/v1/ambient/roadmap/demo-seed")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def demo_seed(payload: Body) -> dict:
        """A realistic roadmap on a DEMO project (refused on any other project)."""
        from boss_engine.services.ambient import roadmap_demo

        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            out = _errors(lambda: roadmap_demo.seed(c, payload.project_id, settings=settings(),
                                                    actor=actor))
            return {"report": out["report"], "decided": out["decided"]}

    @router.post("/v1/ambient/learning/tick")
    @permit(P.WORKER_LEASE)
    def tick() -> dict:
        """The worker's loop: due outcome sweeps and detector runs (bounded; no model call)."""
        from apps.api.app.main import require_worker_principal

        require_worker_principal()
        return roadmap.tick(engine, settings=settings())

    return router
