"""/v1/golden-journeys: the preview's daily journeys and the feature freeze's counter.

    GET  /v1/golden-journeys                    today's result per journey and host, the
                                                consecutive green days (BOSSAPP-1285), the last
                                                two weeks, the pending journeys and the schedule
    POST /v1/golden-journeys/results            {results: [...], host?} rows a runner judged
                                                (scripts/golden-journeys.py run --record)
    POST /v1/golden-journeys/run                {project_id, journeys?, variables?} start today's
                                                run now, in the background
    POST /v1/golden-journeys/schedule           {project_id, at_time, timezone?, days?} the daily
                                                run, as a schedule_work wake

Journeys are install-wide (assurance at org scope); the run and the schedule act in a project.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit


def _refuse(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def _actor(request: Request, fallback: str = "operator") -> str:
    try:
        from apps.api.app import identity_guard

        ctx = identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
        ctx = None
    return ctx.principal.id if ctx is not None else fallback


def build_golden_journeys_router(engine_getter: Optional[Callable[[], Any]] = None,
                                 settings_getter: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.services.golden_journeys import definitions, results, service

    router = APIRouter(tags=["assurance"])

    def _engine() -> Any:
        if engine_getter is not None:
            return engine_getter()
        from boss_engine.db import get_engine

        return get_engine()

    def _settings() -> Any:
        if settings_getter is not None:
            return settings_getter()
        from boss_engine.config import get_settings

        return get_settings()

    @router.get("/v1/golden-journeys")
    @permit(P.ASSURANCE_READ, scope=Scope.org())
    def golden_journeys(project_id: Optional[str] = None) -> Any:
        with _engine().begin() as c:
            out = results.summary(c)
            out["schedule"] = service.daily_wake(c, project_id) if project_id else None
        out["running"] = service.status().get("running", False)
        return out

    @router.post("/v1/golden-journeys/results", status_code=201)
    @permit(P.ASSURANCE_RUN, scope=Scope.org())
    def record_results(request: Request, payload: dict = Body(...)) -> Any:
        rows = (payload or {}).get("results")
        if not isinstance(rows, list) or not rows:
            return _refuse(422, "bad_request", "Send results: a list of journey results.")
        known = {j.id for j in definitions.load_all()}
        host = str((payload or {}).get("host") or "").strip() or None
        stored = []
        try:
            with _engine().begin() as c:
                for row in rows:
                    if not isinstance(row, dict) or row.get("journey_id") not in known:
                        return _refuse(422, "unknown_journey",
                                       f"No journey {row.get('journey_id') if isinstance(row, dict) else row!r}.")
                    stored.append(results.record(c, row, host=host, actor=_actor(request)))
        except ValueError as exc:
            return _refuse(422, "bad_request", str(exc))
        return {"stored": stored}

    @router.post("/v1/golden-journeys/run", status_code=202)
    @permit(P.ASSURANCE_RUN, scope=Scope.project(body="project_id"))
    def run_now(request: Request, payload: dict = Body(...)) -> Any:
        body = payload or {}
        project_id = str(body.get("project_id") or "").strip()
        if not project_id:
            return _refuse(422, "bad_request", "project_id is required.")
        variables = body.get("variables") if isinstance(body.get("variables"), dict) else None
        only = [str(j) for j in body.get("journeys") or []] or None
        out = service.start(_engine(), _settings(), project_id=project_id,
                            actor=_actor(request), variables=variables, only=only)
        if not out.get("ok"):
            return _refuse(409, "not_started", str(out.get("error")))
        return out

    @router.post("/v1/golden-journeys/schedule", status_code=201)
    @permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
    def schedule(request: Request, payload: dict = Body(...)) -> Any:
        from boss_engine.services.agent_wakeups import WakeupError

        body = payload or {}
        project_id = str(body.get("project_id") or "").strip()
        at_time = str(body.get("at_time") or "").strip()
        if not project_id or not at_time:
            return _refuse(422, "bad_request", "project_id and at_time (24h, e.g. 06:00) are required.")
        try:
            with _engine().begin() as c:
                return service.arm_daily(c, project_id=project_id, actor=_actor(request, "ceo"),
                                         at_time=at_time,
                                         tz_name=str(body.get("timezone") or "UTC").strip() or "UTC",
                                         days=[str(x) for x in body.get("days") or []])
        except WakeupError as exc:
            return _refuse(422, "bad_schedule", str(exc))

    return router
