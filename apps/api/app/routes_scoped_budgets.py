"""/v1/budget-policies/scoped: budgets at org, project, agent and workflow scope (BOSSAPP-863).

    GET    /v1/budget-policies/scoped?project_id=   the org's active scoped budgets, with spend
    POST   /v1/budget-policies/scoped               {project_id, scope_type, scope_id?, cap_usd_micros,
                                                     period?, warn_pct?, hard_stop?, role_project_id?}
    DELETE /v1/budget-policies/scoped/{policy_id}?project_id=

A scoped budget reaches across projects (an org cap, an agent everywhere), so writing one needs
budget:manage at ORG scope; reading needs budget:read there. `project_id` names the project the
console is on (it locates the org; a role budget may be pinned to it with role_project_id).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit


def _refuse(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _context(request: Request):
    try:
        from apps.api.app import identity_guard

        return identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
        return None


def build_scoped_budgets_router(engine_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.services import scoped_budgets

    router = APIRouter(tags=["budget"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    def _org(c: Any, request: Request, project_id: Optional[str]) -> Optional[str]:
        ctx = _context(request)
        if ctx is not None:
            return ctx.principal.org_id
        return scoped_budgets._org_of_project(c, str(project_id or "")) if project_id else None

    def _actor(request: Request) -> str:
        ctx = _context(request)
        return ctx.principal.id if ctx is not None else "operator"

    @router.get("/v1/budget-policies/scoped")
    @permit(P.BUDGET_READ, scope=Scope.org())
    def list_scoped_budgets(request: Request, project_id: Optional[str] = None) -> Any:
        with _engine().begin() as c:
            org = _org(c, request, project_id)
            if not org:
                return _refuse(404, "org_not_found")
            return {"policies": scoped_budgets.list_policies(c, org)}

    @router.post("/v1/budget-policies/scoped", status_code=201)
    @permit(P.BUDGET_MANAGE, scope=Scope.org())
    def set_scoped_budget(request: Request, payload: dict = Body(...)) -> Any:
        body = payload or {}
        try:
            with _engine().begin() as c:
                org = _org(c, request, body.get("project_id"))
                if not org:
                    return _refuse(404, "org_not_found")
                return scoped_budgets.set_policy(
                    c, org_id=org, scope_type=str(body.get("scope_type") or ""),
                    scope_id=str(body.get("scope_id") or body.get("project_id") or ""),
                    cap_usd_micros=int(body.get("cap_usd_micros") or 0),
                    period=str(body.get("period") or "month"),
                    warn_pct=float(body.get("warn_pct") if body.get("warn_pct") is not None else 0.8),
                    hard_stop=bool(body.get("hard_stop", True)),
                    project_id=body.get("role_project_id") or None,
                    actor=_actor(request))
        except (scoped_budgets.ScopedBudgetError, ValueError, TypeError) as refused:
            return _refuse(422, "scoped_budget_refused", str(refused))

    @router.delete("/v1/budget-policies/scoped/{policy_id}")
    @permit(P.BUDGET_MANAGE, scope=Scope.org())
    def remove_scoped_budget(policy_id: str, request: Request,
                             project_id: Optional[str] = None) -> Any:
        with _engine().begin() as c:
            org = _org(c, request, project_id)
            if not org or not scoped_budgets.remove_policy(c, org, policy_id):
                return _refuse(404, "policy_not_found")
        return {"removed": policy_id}

    return router
