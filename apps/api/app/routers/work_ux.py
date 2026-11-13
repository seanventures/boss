"""Work UX, set 2 over HTTP (BOSSAPP-879..884). Each route is one call into its service.

* 879 decision options with typed, signed effects (services/decision_options): raise a
  choose-one decision; read what a choice carried out. Choosing is the decision's own approve
  (POST /v1/decisions/{id}/approve with ``choose``).
* 880 a person's watchdog on a work item's subtree (services/work_watchdogs): attach, list,
  detach. The monitor tick evaluates it and tells the watcher once per stop.

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


class WatchdogBody(Body):
    conditions: Optional[list[str]] = Field(default=None, max_length=4)
    stall_hours: int = Field(default=24, ge=1, le=720)
    instructions: str = Field(default="", max_length=2000)
    contact_point_ids: list[str] = Field(default_factory=list, max_length=10)
    enabled: bool = True


def _party(actor: str) -> str:
    return actor if ":" in actor else f"person:{actor}"


def build_work_ux_router(*, engine, settings: Callable[[], Any],
                         require_reader: Callable[..., None],
                         require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import decision_options, work_watchdogs

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

    # ── 880: a person's watchdog on a work tree ────────────────────────────────────────────
    @router.get("/v1/work-items/{work_item_id}/watchdogs")
    @permit(P.WORK_ITEM_READ, scope=Scope.of("work_item", path="work_item_id"))
    def list_watchdogs(work_item_id: str, project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"watchdogs": work_watchdogs.of_item(c, project_id, work_item_id),
                    "conditions": list(work_watchdogs.CONDITIONS)}

    @router.post("/v1/work-items/{work_item_id}/watchdogs", status_code=201)
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def attach_watchdog(work_item_id: str, payload: WatchdogBody) -> dict:
        """Watch this item's subtree as the signed-in member (one watchdog each; again = update)."""
        return {"watchdog": _write(payload, lambda c, actor: work_watchdogs.attach(
            c, project_id=payload.project_id, root_item_id=work_item_id, watcher=_party(actor),
            conditions=payload.conditions, stall_hours=payload.stall_hours,
            instructions=payload.instructions, contact_point_ids=payload.contact_point_ids,
            enabled=payload.enabled))}

    @router.delete("/v1/work-items/{work_item_id}/watchdogs/{watchdog_id}")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def detach_watchdog(work_item_id: str, watchdog_id: str, project_id: str,
                        actor: Optional[str] = None) -> dict:
        with begin(engine) as c:
            who = require_write_actor(c, project_id, actor, "task_create")
            _errors(lambda: work_watchdogs.detach(c, project_id=project_id,
                                                  watchdog_id=watchdog_id, actor=who))
        return {"detached": watchdog_id}

    return router
