"""/v1/golden-journeys: the preview's daily journeys and the feature freeze's counter.

    GET  /v1/golden-journeys                    today's result per journey and host, the
                                                consecutive green days (BOSSAPP-1285), the last
                                                two weeks, the pending journeys and the schedule
    POST /v1/golden-journeys/results            {results: [...], host?} rows a runner judged
                                                (scripts/golden-journeys.py run --record)
    POST /v1/golden-journeys/run                {journeys?} start today's run now, in the
                                                background, in the fixture project
    POST /v1/golden-journeys/schedule           {at_time, timezone?, days?} the install's one
                                                daily run (a schedule_work row BOSS starts itself)

Nothing a caller sends chooses where journeys run or with what: the fixture project and its
values are the operator's (BOSS_JOURNEY_PROJECT, BOSS_JOURNEY_VARS; services/golden_journeys/
service.py). A POSTed green is believed only when BOSS's verdict, run again here over the row's
own calls, accepts every item; and only the journey account's own rows (BOSS_JOURNEY_PRINCIPAL)
count toward the freeze — anyone else's are shown as `posted`, never counted.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.identity import P, Scope, permit


def _refuse(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def _principal(request: Request) -> Optional[str]:
    try:
        from apps.api.app import identity_guard

        ctx = identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
        ctx = None
    return ctx.principal.id if ctx is not None else None


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
    def golden_journeys() -> Any:
        with _engine().begin() as c:
            out = results.summary(c)
            out["schedule"] = service.daily_wake(c)
        out["running"] = service.status().get("running", False)
        out["fixture_project"] = service.fixture_project() or None
        return out

    @router.post("/v1/golden-journeys/results", status_code=201)
    @permit(P.ASSURANCE_RUN, scope=Scope.org())
    def record_results(request: Request, payload: dict = Body(...)) -> Any:
        from sqlalchemy.exc import DataError

        rows = (payload or {}).get("results")
        if not isinstance(rows, list) or not rows:
            return _refuse(422, "bad_request", "Send results: a list of journey results.")
        by_id = {j.id: j for j in definitions.load_all()}
        host = str((payload or {}).get("host") or "").strip() or None
        who = _principal(request) or "anonymous"
        account = (os.environ.get("BOSS_JOURNEY_PRINCIPAL") or "").strip()
        source = "run" if account and who == account else "posted"
        for row in rows:
            if not isinstance(row, dict) or row.get("journey_id") not in by_id:
                return _refuse(422, "unknown_journey",
                               f"No journey {row.get('journey_id') if isinstance(row, dict) else row!r}.")
            if row.get("status") == "green":
                ok, why = results.rejudge(row, by_id[row["journey_id"]])
                if not ok:
                    return _refuse(422, "not_green",
                                   f"{row['journey_id']} is not green on BOSS's verdict: {why}")
        stored = []
        try:
            with _engine().begin() as c:
                for row in rows:
                    stored.append(results.record(c, row, host=host, actor=who, source=source))
        except ValueError as exc:
            return _refuse(422, "bad_request", str(exc))
        except DataError as exc:
            return _refuse(422, "bad_request", f"a result could not be stored ({type(exc).__name__})")
        return {"stored": stored, "source": source}

    @router.post("/v1/golden-journeys/run", status_code=202)
    @permit(P.ASSURANCE_RUN, scope=Scope.org())
    def run_now(request: Request, payload: Optional[dict] = Body(default=None)) -> Any:
        body = payload or {}
        only = [str(j) for j in body.get("journeys") or []] or None
        out = service.start(_engine(), _settings(), actor=_principal(request) or "operator",
                            only=only)
        if not out.get("ok"):
            return _refuse(409, "not_started", str(out.get("error")))
        return out

    @router.post("/v1/golden-journeys/schedule", status_code=201)
    @permit(P.AUTOPILOT_OPERATE, scope=Scope.org())
    def schedule(request: Request, payload: dict = Body(...)) -> Any:
        from boss_engine.services.agent_wakeups import WakeupError

        body = payload or {}
        project_id = service.fixture_project()
        at_time = str(body.get("at_time") or "").strip()
        if not project_id:
            return _refuse(409, "no_fixture_project",
                           "Set BOSS_JOURNEY_PROJECT to the journeys' fixture project first.")
        if not at_time:
            return _refuse(422, "bad_request", "at_time (24h, e.g. 06:00) is required.")
        try:
            with _engine().begin() as c:
                return service.arm_daily(c, project_id=project_id,
                                         actor=_principal(request) or "operator", at_time=at_time,
                                         tz_name=str(body.get("timezone") or "UTC").strip() or "UTC",
                                         days=[str(x) for x in body.get("days") or []])
        except WakeupError as exc:
            return _refuse(422, "bad_schedule", str(exc))

    return router
