"""The incident squad's HTTP surface (BOSSAPP-910; services/incident_squad.py).

* `POST /v1/ambient/incidents/{work_item_id}/route` — run the squad on an incident item now (the
  Chief-of-Staff tick calls the same handler; this is "Work it now"). work_item:manage.
* `POST /v1/ambient/demo/order-api-incident` — load and run the Order API scenario into a project
  (fixtures, scripted engine, no host touched). Only when the API runs with
  BOSS_DEMO_SCENARIOS=1, and only for autonomy:manage. `approve: true` answers the production
  Needs-you card as the signed-in person, so the whole flow is drawn.

The routes add no behaviour of their own: each is one call into the service.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class DemoBody(Body):
    environment: str = Field(default="production", pattern="^[a-z]{2,20}$")
    approve: bool = False


def build_incident_squad_router(*, engine, settings: Callable[[], Any],
                                require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import incident_squad
    from boss_engine.services.ambient import board, ledger
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
        except (board.Conflict, board.LeaseLost, ledger.LedgerConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (EvidenceError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/ambient/incidents/{work_item_id}/route")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def route(work_item_id: str, payload: Body) -> dict:
        with begin(engine) as c:
            require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            out = _errors(lambda: incident_squad.route_incident(
                c, project_id=payload.project_id, item_id=work_item_id, settings=settings()))
            return {"stage": out["stage"], "item": out.get("item")}

    @router.post("/v1/ambient/demo/order-api-incident")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def demo(payload: DemoBody) -> dict:
        from boss_engine.services import incident_squad_demo

        if not incident_squad_demo.enabled():
            raise HTTPException(status_code=404, detail="demo scenarios are off here "
                                f"(start the API with {incident_squad_demo.ENV_FLAG}=1)")
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            out = _errors(lambda: incident_squad_demo.run_scenario(
                c, payload.project_id, environment=payload.environment,
                approve_as=actor if payload.approve else None, settings=settings()))
            return {k: out[k] for k in ("item_id", "incident_id", "signal_id", "stages",
                                        "needs_you_decision_id", "graph")}

    return router
