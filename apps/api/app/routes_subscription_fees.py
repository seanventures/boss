"""Seat fees and invoice reconciliation (BOSSAPP-864).

    GET  /v1/spend/reconciliation?project_id=&start=&end=   estimated, reconciled, fees, total
    GET  /v1/subscription-prices?project_id=                the project's seat prices (history)
    POST /v1/subscription-prices                            {project_id, provider, plan, seats,
                                                             monthly_usd_micros, effective_from?}
    POST /v1/invoices/import                                {project_id, provider, content}: a CSV
                                                             or JSON export; one adjustment per line
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit


def _refuse(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def _actor(request: Request) -> str:
    try:
        from apps.api.app import identity_guard

        ctx = identity_guard.request_auth(request)
        return ctx.principal.id if ctx is not None else "operator"
    except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
        return "operator"


def _date(value: Optional[str], default: date) -> date:
    if not value:
        return default
    return date.fromisoformat(str(value)[:10])


def build_subscription_fees_router(engine_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.services import subscription_fees as fees

    router = APIRouter(tags=["budget"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    @router.get("/v1/spend/reconciliation")
    @permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
    def spend_reconciliation(project_id: str, start: Optional[str] = None,
                             end: Optional[str] = None) -> Any:
        today = datetime.now(timezone.utc).date()
        try:
            first, last = _date(start, today.replace(day=1)), _date(end, today)
        except ValueError:
            return _refuse(422, "bad_date", "dates are YYYY-MM-DD")
        with _engine().begin() as c:
            return fees.report(c, project_id, first, last)

    @router.get("/v1/subscription-prices")
    @permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
    def list_subscription_prices(project_id: str) -> Any:
        from sqlalchemy import text

        with _engine().begin() as c:
            rows = c.execute(text(
                "SELECT id, provider, plan, seats, monthly_usd_micros, effective_from, effective_to, "
                "created_by FROM subscription_prices WHERE project_id = :p "
                "ORDER BY provider, plan, effective_from DESC"), {"p": project_id}).mappings().all()
        return {"prices": [{**dict(r), "effective_from": r["effective_from"].isoformat(),
                            "effective_to": r["effective_to"].isoformat() if r["effective_to"] else None}
                           for r in rows]}

    @router.post("/v1/subscription-prices", status_code=201)
    @permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
    def set_subscription_price(request: Request, payload: dict = Body(...)) -> Any:
        body = payload or {}
        try:
            with _engine().begin() as c:
                return fees.set_price(
                    c, str(body.get("project_id") or ""), provider=str(body.get("provider") or ""),
                    plan=str(body.get("plan") or ""),
                    monthly_usd_micros=int(body.get("monthly_usd_micros") or 0),
                    seats=int(body.get("seats") or 1),
                    effective_from=(date.fromisoformat(str(body["effective_from"])[:10])
                                    if body.get("effective_from") else None),
                    actor=_actor(request))
        except (fees.SubscriptionFeeError, ValueError, TypeError) as refused:
            return _refuse(422, "price_refused", str(refused))

    @router.post("/v1/invoices/import", status_code=201)
    @permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
    def import_invoice(request: Request, payload: dict = Body(...)) -> Any:
        body = payload or {}
        try:
            with _engine().begin() as c:
                return fees.import_invoice(c, str(body.get("project_id") or ""),
                                           provider=str(body.get("provider") or ""),
                                           content=str(body.get("content") or ""),
                                           actor=_actor(request))
        except fees.SubscriptionFeeError as refused:
            return _refuse(422, "invoice_refused", str(refused))

    return router
