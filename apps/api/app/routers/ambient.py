"""Autonomous agents: the HTTP surface of services/ambient (BOSSAPP-902; contracts in
docs/product/AUTONOMOUS_AGENTS_SPEC.md §7).

WHO MAY DO WHAT — the safe direction is always the easy one
-----------------------------------------------------------
* READ the board, the graphs, the signals: work_item:read. The ledger: decision:read. The
  autonomy matrix, the envelope and the scorecard: autonomy:read.
* ACT ON AN ITEM (take it over, dismiss it) and STOP ambient work in a project: work_item:manage
  (owner, admin, operator). Stopping self-started work must never wait for an admin.
* OVERRIDE an agent's decision: decision:approve — the same seat that answers Needs you.
* RESUME after a stop, CHANGE a class's level or ceiling, the envelope or a guard limit, and the
  install-wide stop/resume: autonomy:manage (owner, admin; step-up). Giving agents MORE room is
  the privileged direction.

Every write attributes to the signed-in principal (require_write_actor), never to a claimed one.
The routes add no behaviour of their own: each is one call into services/ambient.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class ReasonBody(Body):
    reason: str = Field(default="", max_length=500)


class DismissBody(Body):
    reason: str = Field(min_length=1, max_length=500)


class OverrideBody(Body):
    chosen: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=800)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class LevelBody(Body):
    level: Optional[str] = Field(default=None, pattern="^A[0-3]$")
    ceiling: Optional[str] = Field(default=None, pattern="^A[0-3]$")
    reason: str = Field(default="", max_length=500)


class SettingsBody(Body):
    daily_cap_usd_micros: Optional[int] = None
    max_concurrent: Optional[int] = None
    max_spawn_depth: Optional[int] = None
    daily_attempt_cap: Optional[int] = None
    cooldown_hours: Optional[int] = None
    breaker_failure_ratio: Optional[float] = None
    breaker_min_runs: Optional[int] = None


class InstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Optional[str] = None
    reason: str = Field(default="", max_length=500)


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(pattern="^(off|propose|route)$")
    actor: Optional[str] = None
    reason: str = Field(default="", max_length=500)


class SweepBody(Body):
    only: list[str] = Field(default_factory=list, max_length=20)


def build_ambient_router(*, engine, settings: Callable[[], Any],
                         require_reader: Callable[..., None],
                         require_write_actor: Callable[..., str],
                         org_actor: Callable[[Any, Optional[str]], str]) -> APIRouter:
    from boss_engine.services.ambient import (board, ledger, policy, read_model, signals,
                                              task_classes)
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

    def _item(c, project_id: str, work_item_id: str) -> dict[str, Any]:
        item = board.get(c, work_item_id, project_id=project_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such ambient work item here")
        return item

    # ── the board ────────────────────────────────────────────────────────────────────────────
    @router.get("/v1/ambient/work-items")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def list_items(project_id: str, filter: str = Query("all"),  # noqa: A002 — the tab's name
                   task_class: Optional[str] = None, limit: int = Query(30, ge=1, le=100),
                   before: Optional[str] = None) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return _errors(lambda: read_model.list_view(c, project_id, filter_=filter,
                                                        task_class=task_class, limit=limit,
                                                        before=before))

    @router.get("/v1/ambient/work-items/{work_item_id}")
    @permit(P.WORK_ITEM_READ, scope=Scope.of("work_item", path="work_item_id"))
    def get_item(work_item_id: str, project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            _item(c, project_id, work_item_id)
            graph = _errors(lambda: read_model.graph(c, work_item_id, project_id=project_id))
            return {**graph, "links": board.links(c, work_item_id)}

    @router.get("/v1/ambient/work-items/{work_item_id}/messages")
    @permit(P.WORK_ITEM_READ, scope=Scope.of("work_item", path="work_item_id"))
    def item_messages(work_item_id: str, project_id: str) -> dict:
        from boss_engine.services.ambient import collab

        with begin(engine) as c:
            require_reader(c, project_id)
            _item(c, project_id, work_item_id)
            return {"messages": collab.messages(c, work_item_id)}

    @router.post("/v1/ambient/work-items/{work_item_id}/take-over")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def take_over(work_item_id: str, payload: ReasonBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            _item(c, payload.project_id, work_item_id)
            return {"item": _errors(lambda: board.take_over(c, work_item_id, actor=actor,
                                                            reason=payload.reason))}

    @router.post("/v1/ambient/work-items/{work_item_id}/dismiss")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.of("work_item", path="work_item_id"))
    def dismiss(work_item_id: str, payload: DismissBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            _item(c, payload.project_id, work_item_id)
            return {"item": _errors(lambda: board.dismiss(c, work_item_id, actor=actor,
                                                          reason=payload.reason))}

    # ── signals ──────────────────────────────────────────────────────────────────────────────
    @router.get("/v1/ambient/signals")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def list_signals(project_id: str, status: Optional[str] = None,
                     source: Optional[str] = None, limit: int = Query(100, ge=1, le=500)) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"signals": signals.list_signals(
                c, project_id=project_id, status=[s for s in (status or "").split(",") if s],
                source=source, limit=limit)}

    @router.post("/v1/ambient/signals/sweep")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def sweep(payload: SweepBody) -> dict:
        """Check now: run this project's signal adapters once (the CoS tick does it on its own)."""
        with begin(engine) as c:
            require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return {"adapters": signals.sweep(c, project_id=payload.project_id,
                                              only=payload.only or None)}

    # ── the decision ledger ──────────────────────────────────────────────────────────────────
    @router.get("/v1/ambient/decisions")
    @permit(P.DECISION_READ, scope=Scope.project(query="project_id"))
    def list_decisions(project_id: str, work_item_id: Optional[str] = None,
                       task_class: Optional[str] = None,
                       limit: int = Query(100, ge=1, le=500)) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"decisions": ledger.list_decisions(c, project_id, work_item_id=work_item_id,
                                                       task_class=task_class, limit=limit)}

    @router.get("/v1/ambient/decisions/{act_id}")
    @permit(P.DECISION_READ, scope=Scope.of("agent_act", path="act_id"))
    def get_decision(act_id: str, project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            row = _errors(lambda: ledger.get_decision(c, act_id))
            if row["project_id"] != project_id:
                raise HTTPException(status_code=404, detail="no such decision here")
            return {"decision": row}

    @router.post("/v1/ambient/decisions/{act_id}/override")
    @permit(P.DECISION_APPROVE, scope=Scope.of("agent_act", path="act_id"))
    def override(act_id: str, payload: OverrideBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            row = _errors(lambda: ledger.get_decision(c, act_id))
            if row["project_id"] != payload.project_id:
                raise HTTPException(status_code=404, detail="no such decision here")
            return {"decision": _errors(lambda: ledger.override(
                c, act_id, actor=actor, chosen=payload.chosen, rationale=payload.rationale,
                evidence=payload.evidence))}

    # ── autonomy, envelope, stop ─────────────────────────────────────────────────────────────
    @router.get("/v1/ambient/autonomy")
    @permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
    def autonomy(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"classes": policy.matrix(c, project_id),
                    "levels": [{"level": k, "name": v, "meaning": task_classes.LEVEL_MEANINGS[k]}
                               for k, v in task_classes.LEVEL_NAMES.items()],
                    "actions": [{"key": a.key, "title": a.title, "min_level": a.min_level,
                                 "risk": a.risk, "reversibility": a.reversibility,
                                 "always_a_person": a.always_a_person}
                                for a in task_classes.ACTIONS.values()],
                    "changes": policy.changes(c, project_id, limit=20)}

    @router.put("/v1/ambient/autonomy/{task_class}")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def set_level(task_class: str, payload: LevelBody) -> dict:
        if payload.level is None and payload.ceiling is None:
            raise HTTPException(status_code=422, detail="give a level, a ceiling or both")
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            return {"class": _errors(lambda: policy.set_level(
                c, payload.project_id, task_class, actor=actor, level=payload.level,
                ceiling=payload.ceiling, reason=payload.reason))}

    @router.get("/v1/ambient/settings")
    @permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
    def get_settings_(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"settings": policy.settings_for(c, project_id),
                    "envelope": policy.envelope(c, project_id),
                    "stop": policy.stop_state(c, project_id),
                    "mode": policy.mode_state(c, settings())}

    @router.put("/v1/ambient/settings")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def put_settings(payload: SettingsBody) -> dict:
        changes = payload.model_dump(exclude={"project_id", "actor"}, exclude_none=True)
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            return {"settings": _errors(lambda: policy.update_settings(
                c, payload.project_id, actor=actor, **changes))}

    @router.post("/v1/ambient/stop")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def stop(payload: ReasonBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            return _errors(lambda: policy.stop(c, project_id=payload.project_id, actor=actor,
                                               reason=payload.reason))

    @router.post("/v1/ambient/resume")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def resume(payload: ReasonBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            return {"stop": policy.resume(c, project_id=payload.project_id, actor=actor,
                                          reason=payload.reason)}

    @router.post("/v1/ambient/stop-all")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.org())
    def stop_all(payload: InstallBody) -> dict:
        with begin(engine) as c:
            actor = org_actor(c, payload.actor)
            return policy.stop(c, project_id=None, actor=actor,
                               reason=payload.reason or "stopped install-wide")

    @router.post("/v1/ambient/resume-all")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.org())
    def resume_all(payload: InstallBody) -> dict:
        with begin(engine) as c:
            actor = org_actor(c, payload.actor)
            return {"stop": policy.resume(c, project_id=None, actor=actor,
                                          reason=payload.reason)}

    # ── the always-on mode (install-wide; design §9 "UI plus API plus env var") ──────────────
    @router.get("/v1/ambient/mode")
    @permit(P.AUTONOMY_READ, scope=Scope.org())
    def get_mode() -> dict:
        with begin(engine) as c:
            return {"mode": policy.mode_state(c, settings())}

    @router.put("/v1/ambient/mode")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.org())
    def put_mode(payload: ModeBody) -> dict:
        """Turn the always-on agents on (propose: every handover asks a person; route: each class
        acts within its own level) or off. Giving agents more room is the privileged direction:
        autonomy:manage, like Resume. BOSS_AMBIENT_COS in .env pins it (409)."""
        with begin(engine) as c:
            actor = org_actor(c, payload.actor)
            try:
                return {"mode": policy.set_mode(c, settings(), mode=payload.mode, actor=actor,
                                                reason=payload.reason)}
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/v1/ambient/scorecard")
    @permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
    def scorecard(project_id: str, days: int = Query(7, ge=1, le=90),
                  task_class: Optional[str] = None) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return read_model.scorecard(c, project_id, days=days, task_class=task_class)

    return router
