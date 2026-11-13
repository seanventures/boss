"""Work UX, set 2 over HTTP (BOSSAPP-879..884). Each route is one call into its service.

* 879 decision options with typed, signed effects (services/decision_options): raise a
  choose-one decision; read what a choice carried out. Choosing is the decision's own approve
  (POST /v1/decisions/{id}/approve with ``choose``).

WHO MAY DO WHAT: reading follows the thing read (decision:read, work_item:read ...); every change
is attributed to the signed-in member (require_write_actor), never to a claimed actor.
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


class OptionsBody(Body):
    question: str = Field(min_length=1, max_length=500)
    options: list[dict[str, Any]] = Field(min_length=1, max_length=8)
    work_item_id: Optional[str] = None
    context: str = Field(default="", max_length=1000)
    risk_tier: int = Field(default=1, ge=0, le=3)


def build_work_ux_router(*, engine, settings: Callable[[], Any],
                         require_reader: Callable[..., None],
                         require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import decision_options

    router = APIRouter(tags=["work-ux"])

    def _errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError,) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _write(payload: Body, fn: Callable[[Any, str], Any], permission: str = "task_create") -> Any:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, permission)
            return _errors(lambda: fn(c, actor))

    # ── 879: decision options with signed effects ───────────────────────────────────────────
    @router.post("/v1/decision-options", status_code=201)
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def offer_options(payload: OptionsBody) -> dict:
        """Raise a choose-one decision whose options carry typed effects (≤ 8), each signed."""
        return _write(payload, lambda c, actor: decision_options.offer(
            c, project_id=payload.project_id, question=payload.question,
            options=payload.options, asker=actor, work_item_id=payload.work_item_id,
            context=payload.context, risk_tier=payload.risk_tier))

    @router.get("/v1/decision-options/{decision_id}/runs")
    @permit(P.DECISION_READ, scope=Scope.of("decision", path="decision_id"))
    def option_runs(decision_id: str, project_id: str) -> dict:
        """What a choice carried out: one row per effect (done | skipped_stale | failed)."""
        from sqlalchemy import text

        with begin(engine) as c:
            require_reader(c, project_id)
            row = c.execute(text(
                "SELECT id, project_id, recommendation FROM decisions WHERE id = :d "
                "AND project_id = :p"), {"d": decision_id, "p": project_id}).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"no decision {decision_id}")
            rec = dict(row).get("recommendation") or {}
            return {"decision_id": decision_id,
                    "chosen_option": rec.get("chosen_option") if isinstance(rec, dict) else None,
                    "runs": decision_options.effect_runs(c, decision_id)}

    return router
