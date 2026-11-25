"""/v1/pilot-metrics: the numbers a guided preview is judged on (BOSSAPP-1292).

    GET  /v1/pilot-metrics?project_id=&scope_type=org|team|project&scope_id=&days=7|30|90
    GET  /v1/pilot-metrics/export?...&format=csv|json    the same report as a download
    GET  /v1/pilot-metrics/baseline?project_id=          the customer's before-numbers
    POST /v1/pilot-metrics/baseline                      {project_id, values: {...}, note?}

Read with budget:read at org scope (it sits in Govern beside spend); the baseline is written
with budget:manage there. `project_id` names the project the console is on (it locates the org).
Everything is computed on read from runs, decisions, events and alerts (services/pilot_metrics.py).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response

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


def build_pilot_metrics_router(engine_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.services import pilot_metrics, scoped_budgets

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

    def _report(request: Request, project_id: Optional[str], scope_type: str,
                scope_id: Optional[str], days: int) -> Any:
        try:
            with _engine().begin() as c:
                org = _org(c, request, project_id)
                if not org:
                    return _refuse(404, "org_not_found")
                return pilot_metrics.compute(c, org, scope_type=scope_type,
                                             scope_id=str(scope_id or ""), days=int(days))
        except (scoped_budgets.ScopedBudgetError, ValueError) as refused:
            return _refuse(422, "pilot_metrics_refused", str(refused))

    @router.get("/v1/pilot-metrics")
    @permit(P.BUDGET_READ, scope=Scope.org())
    def pilot_metrics_report(request: Request, project_id: Optional[str] = None,
                             scope_type: str = "org", scope_id: Optional[str] = None,
                             days: int = 30) -> Any:
        return _report(request, project_id, scope_type, scope_id, days)

    @router.get("/v1/pilot-metrics/export")
    @permit(P.BUDGET_READ, scope=Scope.org())
    def pilot_metrics_export(request: Request, project_id: Optional[str] = None,
                             scope_type: str = "org", scope_id: Optional[str] = None,
                             days: int = 30, format: str = "csv") -> Any:  # noqa: A002
        report = _report(request, project_id, scope_type, scope_id, days)
        if isinstance(report, JSONResponse):
            return report
        stamp = str(report["until"])[:10]
        if format == "json":
            return Response(json.dumps(report, indent=2, default=str), media_type="application/json",
                            headers={"Content-Disposition":
                                     f'attachment; filename="pilot-metrics-{stamp}.json"'})
        if format != "csv":
            return _refuse(422, "pilot_metrics_refused", "format is csv or json")
        return Response(pilot_metrics.to_csv(report), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="pilot-metrics-{stamp}.csv"'})

    @router.get("/v1/pilot-metrics/baseline")
    @permit(P.BUDGET_READ, scope=Scope.org())
    def pilot_baseline(request: Request, project_id: Optional[str] = None) -> Any:
        with _engine().begin() as c:
            org = _org(c, request, project_id)
            if not org:
                return _refuse(404, "org_not_found")
            return {**pilot_metrics.get_baseline(c, org), "fields": pilot_metrics.BASELINE_FIELDS}

    @router.post("/v1/pilot-metrics/baseline")
    @permit(P.BUDGET_MANAGE, scope=Scope.org())
    def set_pilot_baseline(request: Request, payload: dict = Body(...)) -> Any:
        body = payload or {}
        values = body.get("values")
        if not isinstance(values, dict):
            return _refuse(422, "baseline_refused", "values is an object of numbers")
        try:
            with _engine().begin() as c:
                org = _org(c, request, body.get("project_id"))
                if not org:
                    return _refuse(404, "org_not_found")
                return pilot_metrics.set_baseline(c, org, values, str(body.get("note") or ""),
                                                  _actor(request))
        except (ValueError, TypeError) as refused:
            return _refuse(422, "baseline_refused", str(refused))

    return router
