"""Autonomous agents: the Chief of Staff's tick, Discovery's dossier and the demo over HTTP
(BOSSAPP-904/909; contracts in docs/product/AUTONOMOUS_AGENTS_SPEC.md §7.2).

    POST /v1/ambient/tick                     the worker (worker:lease): leases, sweep, triage
    POST /v1/ambient/chief-of-staff/run       "Triage now" for one project (work_item:manage)
    GET  /v1/ambient/dossier?project_id&repo  the project's dossier(s) (work_item:read)
    POST /v1/ambient/dossier                  build it now (work_item:manage)
    POST /v1/ambient/demo                     seed the demo morning on a demo project
                                              (autonomy:manage; refused on a real project)

Each route is one call into services/ambient; the kill switch, the gate, the guards and the
envelope hold inside those calls, never here.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class TickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunBody(_Body):
    sweep: bool = True


class DossierBody(_Body):
    repo: Optional[str] = Field(default=None, max_length=200)


class DemoBody(_Body):
    mode: str = Field(default="route", pattern="^(route|propose)$")


def build_ambient_cos_router(*, engine, settings: Callable[[], Any],
                             require_reader: Callable[..., None],
                             require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services.ambient import chief_of_staff, demo, dossier, policy

    router = APIRouter(tags=["ambient"])

    @router.post("/v1/ambient/tick")
    @permit(P.WORKER_LEASE)
    def ambient_tick(body: Optional[TickBody] = None) -> dict:
        """The worker's minute: expire leases, sweep every adapter, triage every project with
        work. BOSS_AMBIENT_SIGNALS / BOSS_AMBIENT_COS and every stop are honoured inside."""
        from apps.api.app.main import require_worker_principal

        require_worker_principal()
        return chief_of_staff.tick(engine, settings())

    @router.post("/v1/ambient/chief-of-staff/run")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def run_now(payload: RunBody) -> dict:
        """Triage now: this project's sweep and the Chief of Staff's turn, at once. A person
        pressing it while BOSS_AMBIENT_COS is off (the rc10.11 default) gets `propose`: every
        handover is a Needs-you card, nothing is handed to an agent without them."""
        current = settings()
        with begin(engine) as c:
            require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            mode = policy.effective_mode(c, current)
        return chief_of_staff.tick(engine, current, project_id=payload.project_id,
                                   sweep=payload.sweep,
                                   mode="propose" if mode == "off" else mode)

    @router.get("/v1/ambient/dossier")
    @permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
    def get_dossier(project_id: str, repo: Optional[str] = None) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"dossiers": dossier.get(c, project_id, repo)}

    @router.post("/v1/ambient/dossier")
    @permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
    def build_dossier(payload: DossierBody) -> dict:
        """Read the project's repository now: the index's checkout when it is on disk, else the
        code graph's file list. Seeds the signals the Chief of Staff will triage."""
        current = settings()
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "decision_resolve")
            repo, root, paths = dossier.checkout_for(c, current, payload.project_id, payload.repo)
            if not repo:
                raise HTTPException(status_code=404,
                                    detail="this project has no registered repository")
            if root is None and not paths:
                raise HTTPException(status_code=409, detail=(
                    "BOSS has not read this repository yet; its code map is still being built"))
            use_osv = bool(int(getattr(current, "ambient_dossier_osv", 1) or 0))
            built = dossier.build(c, project_id=payload.project_id, repo=repo, root=root,
                                  paths=paths, actor=f"person:{actor}",
                                  osv=dossier.osv_lookup if (root is not None and use_osv)
                                  else None)
            return {"dossier": built}

    @router.post("/v1/ambient/demo")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def load_demo(payload: DemoBody) -> dict:
        """The demo morning (services/ambient/demo.py) on a demo project only."""
        with begin(engine) as c:
            require_write_actor(c, payload.project_id, payload.actor, "role_review")
            try:
                out = demo.load(c, settings(), project_id=payload.project_id,
                                mode=payload.mode)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            return {"project_id": out["project_id"], "signals": len(out["signals"]),
                    "counts": out["triage"].get("counts") or {},
                    "owners_played": out["owners_played"]}

    @router.get("/v1/ambient/demo/status")
    @permit(P.AUTONOMY_READ, scope=Scope.project(query="project_id"))
    def demo_status(project_id: str) -> dict:
        """Whether this project may load the demo (the console's "Load demo" control)."""
        with begin(engine) as c:
            require_reader(c, project_id)
            return demo.status(c, project_id)

    @router.post("/v1/ambient/demo/load")
    @permit(P.AUTONOMY_MANAGE, scope=Scope.project(body="project_id"))
    def load_everything(payload: _Body) -> dict:
        """Every demo story at once on a demo project (services/ambient/demo.load_all)."""
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
        try:
            return demo.load_all(engine, settings(), project_id=payload.project_id,
                                 actor=actor)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return router
