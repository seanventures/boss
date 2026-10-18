"""BOSS FIXES ITSELF: the self-fix routes (docs/product/BOSS_FIXES_ITSELF.md §11.4, BOSSAPP-571).

Three callers, three kinds of route:
  * PEOPLE (the console, MCP): list, read and stop self-fixes, start one from an open
    recommendation, read and change the install-wide switch and settings, read the upgrade plan.
    Approval is the existing decisions API (`decide`): there is no approval route here.
  * THE HOST SELF-INSTALLER, as `sys_self_installer` with its own key and nothing else
    (identity_guard.require_self_installer, under every authz mode): list and take tickets,
    report install phases, ask for a fresh ticket of an approved fix, read the health window,
    report an upgrade's stack plan. No other principal ever reaches these.
  * THE WORKER: run a leased `self_fix.build` job (the build runs here, under its lease).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    project_id: str
    actor: Optional[str] = None


class StartBody(Body):
    recommendation_id: str


class CancelBody(Body):
    reason: Optional[str] = None


class SettingsBody(Body):
    on: Optional[bool] = None
    settings: Optional[dict[str, Any]] = None
    why: Optional[str] = None


class TakeBody(BaseModel):
    pass


class PreviewEditBody(Body):
    send_id: str
    body: Optional[str] = None
    evidence: Optional[str] = None


class SendBody(Body):
    send_id: str
    checked: bool = False


class ReportBody(BaseModel):
    phase: str
    ok: bool
    detail: Optional[str] = ""
    stack_head: Optional[str] = None


class ReissueBody(BaseModel):
    self_fix_id: str


class StackReportBody(BaseModel):
    release: str
    fixes: list[dict[str, Any]]


class BuildBody(BaseModel):
    job_id: str
    worker_id: str
    lease_token: str
    note: Optional[str] = ""


def build_self_fix_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    require_self_installer: Callable[[], None],
    require_worker: Callable[[], None],
    jobs: Any = None,
) -> APIRouter:
    from boss_engine.services import autonomy_metric
    from boss_engine.services import self_fix as sf
    from boss_engine.services import self_fix_install as si
    from boss_engine.services import self_fix_upstream as up

    router = APIRouter(tags=["self-fix"])

    def _pid(project_id: str) -> str:
        pid = (project_id or "").strip()
        if not pid:
            raise HTTPException(status_code=400, detail="project_id is required")
        return pid

    def _own(connection, sfx_id: str, project_id: str) -> dict[str, Any]:
        found = sf.get(connection, sfx_id)
        if found is None or found["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="no such self-fix in this project")
        return found

    # ── people ─────────────────────────────────────────────────────────────────────────────
    @router.get("/v1/self-fixes")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def list_self_fixes(project_id: str, status: Optional[str] = None) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            items = sf.list_self_fixes(c, project_id=pid, status=status or None)
            for item in items:
                item["line"] = sf.step_line(item)
        return {"self_fixes": items, "count": len(items)}

    # ── the host self-installer (declared before /v1/self-fixes/{id}, which would match it) ─────────────────────────────────────────────────────────────
    @router.get("/v1/self-fixes/tickets")
    @permit(P.SELF_FIX_TICKET_READ, scope=Scope.org())
    def list_tickets() -> dict:
        require_self_installer()
        with begin(engine) as c:
            si.seen_installer(c)
            return {"tickets": si.pending_tickets(c, settings())}

    @router.post("/v1/self-fixes/tickets/reissue")
    @permit(P.SELF_FIX_TICKET_READ, scope=Scope.org())
    def reissue_ticket(payload: ReissueBody) -> dict:
        require_self_installer()
        with begin(engine) as c:
            try:
                return si.reissue(c, settings(), payload.self_fix_id)
            except si.InstallRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/v1/self-fixes/tickets/{ticket_id}/take")
    @permit(P.SELF_FIX_TICKET_READ, scope=Scope.org())
    def take_ticket(ticket_id: str, payload: TakeBody) -> dict:
        require_self_installer()
        with begin(engine) as c:
            try:
                return si.take(c, ticket_id)
            except si.InstallRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/v1/self-fixes/tickets/{ticket_id}/report")
    @permit(P.SELF_FIX_TICKET_REPORT, scope=Scope.org())
    def report_ticket(ticket_id: str, payload: ReportBody) -> dict:
        require_self_installer()
        with begin(engine) as c:
            try:
                return si.report(c, settings(), ticket_id, phase=payload.phase, ok=payload.ok,
                                 detail=payload.detail or "", stack_head=payload.stack_head)
            except si.InstallRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/v1/self-fixes/health")
    @permit(P.SELF_FIX_TICKET_READ, scope=Scope.org())
    def install_health() -> dict:
        require_self_installer()
        with begin(engine) as c:
            return si.health_report(c)

    @router.post("/v1/self-fixes/stack-report")
    @permit(P.SELF_FIX_TICKET_REPORT, scope=Scope.org())
    def stack_report(payload: StackReportBody) -> dict:
        require_self_installer()
        with begin(engine) as c:
            return {"recorded": si.record_upgrade(c, payload.fixes, release=payload.release)}

    @router.get("/v1/self-fixes/{self_fix_id}")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def get_self_fix(self_fix_id: str, project_id: str) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            _own(c, self_fix_id, pid)
            out = sf.lineage(c, self_fix_id) or {}
            out["line"] = sf.step_line(out.get("self_fix") or {})
            if (out.get("self_fix") or {}).get("status") == sf.AWAITING_APPROVAL:
                out["card"] = si.card(sf.get(c, self_fix_id))
        return out

    @router.get("/v1/self-fixes/{self_fix_id}/diff")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def get_self_fix_diff(self_fix_id: str, project_id: str) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            found = _own(c, self_fix_id, pid)
        pr = found.get("pr") or {}
        red_green = found.get("red_green") or {}
        return {"self_fix_id": self_fix_id, "diff": pr.get("diff") or "",
                "diff_stat": found.get("diff_stat") or {}, "tests": red_green.get("tests") or [],
                "test_files": red_green.get("test_files") or {},
                "red": red_green.get("base") or {}, "green": red_green.get("head") or {},
                "gates": found.get("gates") or {}, "review": found.get("review") or {}}

    @router.post("/v1/self-fixes", status_code=201)
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def start_self_fix(payload: StartBody) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            try:
                out = sf.start_from_recommendation(
                    c, settings(), project_id=pid, recommendation_id=payload.recommendation_id,
                    actor=actor, jobs=jobs)
            except sf.SelfFixError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        if out.get("outcome") == "skipped":
            raise HTTPException(status_code=409, detail=out.get("why") or "not started")
        sfx = out.get("self_fix") or {}
        return {"outcome": out["outcome"], "self_fix_id": sfx.get("id"),
                "status": sfx.get("status")}

    @router.post("/v1/self-fixes/{self_fix_id}/cancel")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def cancel_self_fix(self_fix_id: str, payload: CancelBody) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            _own(c, self_fix_id, pid)
            out = sf.cancel(c, self_fix_id, actor=actor,
                            why=(payload.reason or "a person stopped it")[:300])
        return {"self_fix_id": self_fix_id, "status": out.get("status")}

    @router.get("/v1/self-fix/settings")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def get_self_fix_settings(project_id: str) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            state = sf.switch(c)
            values = {k: sf.setting(c, settings(), k) for k in sf.DEFAULTS}
            stack = si.stack(c)
            spent = sf.month_spent(c)
            installer = si.installer_seen(c)
        ok, why = sf.install_capable(settings())
        return {"switch": state, "settings": values, "auto_install": sf.AUTO_INSTALL_POLICY,
                "auto_install_why": sf.AUTO_INSTALL_WHY,
                "can_install": ok, "why_not": why, "stack": stack,
                "env_off": sf.env_off(), "month_spent_usd": round(spent / 1e6, 2),
                "installer": installer}

    @router.put("/v1/self-fix/settings")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def put_self_fix_settings(payload: SettingsBody) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            current = sf.switch(c)
            try:
                state = sf.set_switch(c, on=current["on"] if payload.on is None else payload.on,
                                      actor=actor, why=payload.why or "",
                                      settings_patch=payload.settings or None)
            except sf.SelfFixError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        return {"switch": state}

    @router.get("/v1/self-fix/upgrade-plan")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def get_upgrade_plan(project_id: str, to: str) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            try:
                return si.upgrade_plan(c, settings(), to_ref=to)
            except Exception as error:  # noqa: BLE001 — a plan BOSS cannot make says why
                raise HTTPException(status_code=409, detail=f"no plan: {error}") from error

    # ── removing installed fixes (a person's decision; the host runs the ticket) ─────────────
    @router.post("/v1/self-fixes/remove-all")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def remove_all_self_fixes(payload: Body) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            try:
                return si.request_removal(c, settings(), project_id=pid, actor=actor)
            except si.InstallRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/v1/self-fixes/{self_fix_id}/remove")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def remove_self_fix(self_fix_id: str, payload: Body) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            _own(c, self_fix_id, pid)
            try:
                return si.request_removal(c, settings(), project_id=pid, actor=actor,
                                          sfx_id=self_fix_id)
            except si.InstallRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    # ── Send to BOSS team (R9 R11 R18): preview → edit → send, or export ───────────────────
    @router.get("/v1/self-fixes/{self_fix_id}/upstream")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def get_upstream(self_fix_id: str, project_id: str) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            _own(c, self_fix_id, pid)
            return {"send": up.latest(c, self_fix_id),
                    "telemetry_off": up.telemetry_off(settings())}

    @router.post("/v1/self-fixes/{self_fix_id}/upstream/preview")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def preview_upstream(self_fix_id: str, payload: Body) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            _own(c, self_fix_id, pid)
            try:
                return up.preview(c, settings(), self_fix_id, actor=actor)
            except up.UpstreamRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.put("/v1/self-fixes/{self_fix_id}/upstream/preview")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def edit_upstream(self_fix_id: str, payload: PreviewEditBody) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            _own(c, self_fix_id, pid)
            try:
                return up.edit(c, settings(), self_fix_id, payload.send_id, actor=actor,
                               body=payload.body, evidence=payload.evidence)
            except up.UpstreamRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/v1/self-fixes/{self_fix_id}/upstream/send")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def send_upstream(self_fix_id: str, payload: SendBody) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            _own(c, self_fix_id, pid)
        # Its own transaction: a refused or failed send still records what happened to it.
        try:
            with begin(engine) as c:
                return up.send(c, settings(), self_fix_id, payload.send_id, actor=actor,
                               checked=payload.checked)
        except up.UpstreamRefused as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/v1/self-fixes/{self_fix_id}/upstream/fork")
    @permit(P.IMPROVEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def fork_upstream(self_fix_id: str, payload: Body) -> dict:
        pid = _pid(payload.project_id)
        with begin(engine) as c:
            actor = require_write_actor(c, pid, payload.actor, "workflow_manage")
            _own(c, self_fix_id, pid)
            try:
                return up.create_fork(c, settings(), self_fix_id, actor=actor)
            except up.UpstreamRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/v1/self-fixes/{self_fix_id}/patch")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def export_patch(self_fix_id: str, project_id: str, actor: Optional[str] = None
                     ) -> Response:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            _own(c, self_fix_id, pid)
            try:
                body, name = up.export(c, settings(), self_fix_id, actor=actor or "a person")
            except up.UpstreamRefused as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(content=body, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ── the autonomy metric (R15) ──────────────────────────────────────────────────────────
    @router.get("/v1/autonomy")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def autonomy(project_id: str, window: str = "30d") -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            return autonomy_metric.report(c, project_id=pid, window=window)

    @router.get("/v1/autonomy/runs/{run_id}")
    @permit(P.IMPROVEMENT_READ, scope=Scope.project(query="project_id"))
    def run_autonomy(run_id: str, project_id: str) -> dict:
        pid = _pid(project_id)
        with begin(engine) as c:
            require_reader(c, pid)
            return autonomy_metric.run_report(c, project_id=pid, run_id=run_id)

    # ── the worker ──────────────────────────────────────────────────────────────────────────
    @router.post("/v1/self-fixes/{self_fix_id}/build")
    @permit(P.WORKER_LEASE)
    def run_build(self_fix_id: str, payload: BuildBody) -> dict:
        require_worker()
        from boss_engine.services import self_fix_build

        return self_fix_build.execute_job(engine, settings(), self_fix_id=self_fix_id,
                                          job_id=payload.job_id, worker_id=payload.worker_id,
                                          lease_token=payload.lease_token,
                                          note=payload.note or "")

    return router


__all__ = ["build_self_fix_router"]
