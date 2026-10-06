"""Govern's reads: one view for the Limits board, and one per tab.

GET  /v1/govern                    meter + every control (effective value, source, enforcer,
                                   evidence, version) + thresholds + token guides
GET  /v1/govern/spend              where money went, over one window
GET  /v1/govern/envelopes          allocations, FinOps, reconciliations, initiative rule
GET  /v1/budget/daily              money per UTC day (the caps' own definition)
POST /v1/budget/cap/preview        "would have stopped N days" for a proposed cap (read-only)
POST /v1/budget/intensity/preview  exactly what Pace would write (read-only)

The screen used to make eleven reads on every render, one of them a POST. Every read here is a
GET of a project reader except the two previews, which are POSTs only because they take a body;
they write nothing. Writes stay on their own routes (settings, set_intensity, setup budget,
token policies, FinOps), unchanged but for an optional If-Match.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..db import begin

router = APIRouter(tags=["govern"])


def _main():
    from apps.api.app import main

    return main


def _etagged(request: Request, body: Any) -> Response:
    encoded = jsonable_encoder(body)
    etag = '"' + hashlib.sha256(json.dumps(encoded, sort_keys=True, default=str)
                                .encode()).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return JSONResponse(content=encoded, headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.get("/v1/govern")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_govern(request: Request, project_id: str = Query(...)) -> Response:
    """The Limits board in one read. ETag'd: the console polls it every 15s while visible."""
    from boss_engine.services import govern_view

    main = _main()
    with begin(main.engine) as connection:
        main.require_project_reader(connection, project_id)
        body = govern_view.build(connection, main.settings, project_id)
    return _etagged(request, body)


@router.get("/v1/govern/spend")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_govern_spend(request: Request, project_id: str = Query(...),
                     window: str = Query("7d", pattern="^(24h|7d|30d)$")) -> Response:
    from boss_engine.services import govern_view

    main = _main()
    with begin(main.engine) as connection:
        main.require_project_reader(connection, project_id)
        try:
            body = govern_view.spend(connection, main.settings, project_id, window=window)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    return _etagged(request, body)


@router.get("/v1/govern/envelopes")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_govern_envelopes(request: Request, project_id: str = Query(...)) -> Response:
    from boss_engine.services import govern_view

    main = _main()
    with begin(main.engine) as connection:
        main.require_project_reader(connection, project_id)
        body = govern_view.envelopes(connection, main.engine, main.settings, project_id)
    return _etagged(request, body)


@router.get("/v1/budget/daily")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_budget_daily(project_id: str = Query(...),
                     days: int = Query(30, ge=1, le=90)) -> dict[str, Any]:
    from boss_engine.services import govern_view

    main = _main()
    with begin(main.engine) as connection:
        main.require_project_reader(connection, project_id)
        return {"project_id": project_id,
                "days": govern_view.daily_spend(connection, project_id, days=days)}


class CapPreviewBody(BaseModel):
    project_id: str
    period: str = Field(pattern="^(day|month)$")
    cap_usd: float = Field(gt=0, le=1_000_000)


@router.post("/v1/budget/cap/preview")
@permit(P.BUDGET_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def post_cap_preview(body: CapPreviewBody) -> dict[str, Any]:
    """Replays the last 30 days of real spend against a proposed cap. Writes nothing."""
    from boss_engine.services import govern_view

    main = _main()
    with begin(main.engine) as connection:
        main.require_project_reader(connection, body.project_id)
        try:
            return govern_view.cap_preview(connection, body.project_id, period=body.period,
                                           cap_usd_micros=int(round(body.cap_usd * 1_000_000)))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


class IntensityPreviewBody(BaseModel):
    intensity: int
    project_id: Optional[str] = None


@router.post("/v1/budget/intensity/preview")
@permit(P.BUDGET_READ, scope=Scope.org(), readonly_post=True)
def post_intensity_preview(body: IntensityPreviewBody) -> dict[str, Any]:
    """Exactly what `set_intensity` would write for this value (the same function), and — when
    a project is named — how many of its last 30 days the resulting daily cap would have
    stopped. Writes nothing."""
    from boss_engine.services import govern_view
    from boss_engine.services.budget import intensity_envelope

    envelope = intensity_envelope(body.intensity)
    if not body.project_id:
        return {"envelope": envelope, "preview": None}
    main = _main()
    with begin(main.engine) as connection:
        main.require_project_reader(connection, body.project_id)
        preview = govern_view.cap_preview(
            connection, body.project_id, period="day",
            cap_usd_micros=envelope["daily_cap_usd_micros"])
    return {"envelope": envelope, "preview": preview}


__all__ = ["router"]
