"""Govern's reads: one view for the Limits board, and one per tab.

GET  /v1/govern                    meter + every control (effective value, source, enforcer,
                                   evidence, version) + thresholds + token guides
GET  /v1/govern/spend              where money went, over one window
GET  /v1/govern/envelopes          allocations, FinOps, reconciliations, initiative rule
GET  /v1/budget/daily              money per UTC day (the caps' own definition)
POST /v1/budget/cap/preview        "would have stopped N days" for a proposed cap (read-only)
POST /v1/budget/intensity/preview  exactly what Pace would write (read-only)
GET  /v1/govern/project-limits     each readable project's monthly limit and month spend
POST /v1/govern/limits             the caps (month, day, burst) and the at-limit choice, one save
POST /v1/govern/project-limit      a project's monthly limit (a project budget envelope)

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


def _budget_projects(request: Request, project_id: str) -> list[dict[str, Any]]:
    """The projects whose spend this viewer may read (BOSSAPP-807 "By project"): the projects
    GET /v1/projects lists for them, narrowed to those where they hold budget:read. Always
    includes the project the request names (the route's own guard already admitted it)."""
    main = _main()
    try:
        items = list(main.list_projects(request).items)
    except Exception:  # noqa: BLE001 — a list that cannot be read is this project alone
        items = []
    rows = [{"id": str(item.id), "name": str(item.name or item.id)} for item in items]
    try:
        from boss_engine.identity import authz
        from boss_engine.identity.scope import ResolvedScope

        from .identity_access import request_context

        ctx = request_context(request)
        org = ctx.principal.org_id
        rows = [r for r in rows if P.BUDGET_READ in authz.effective_permissions(
            ctx, ResolvedScope.in_project(org, r["id"])).permissions]
    except Exception:  # noqa: BLE001 — no identity yet (shadow mode): the list's own filter
        pass
    if not any(r["id"] == project_id for r in rows):
        rows.insert(0, {"id": project_id, "name": project_id})
    return rows


@router.get("/v1/govern/spend")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_govern_spend(request: Request, project_id: str = Query(...),
                     window: str = Query("7d", pattern="^(24h|7d|30d|day|month)$")) -> Response:
    from boss_engine.services import govern_view

    main = _main()
    projects = _budget_projects(request, project_id)
    with begin(main.engine) as connection:
        main.require_project_reader(connection, project_id)
        try:
            body = govern_view.spend(connection, main.settings, project_id, window=window,
                                     projects=projects)
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


@router.get("/v1/govern/project-limits")
@permit(P.BUDGET_READ, scope=Scope.project(query="project_id"))
def get_project_limits(request: Request, project_id: str = Query(...)) -> Response:
    """Govern › Limits › Per project: each readable project's monthly limit (None = no limit)
    and this month's money."""
    from boss_engine.services import spend_limits

    main = _main()
    projects = _budget_projects(request, project_id)
    with begin(main.engine) as connection:
        main.require_project_reader(connection, project_id)
        rows = spend_limits.project_limits(connection, projects)
    return _etagged(request, {"project_id": project_id, "projects": rows})


class LimitsBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    monthly_cap_usd: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    daily_cap_usd: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    burst_usd_per_min: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    at_limit: Optional[str] = Field(default=None, pattern="^(pause|cheaper|stop)$")
    #: BOSSAPP-869: spend BOSS cannot price — estimate (default) or block past the allowance.
    unpriced_policy: Optional[str] = Field(default=None, pattern="^(estimate|block)$")
    unpriced_allowance_usd: Optional[float] = Field(default=None, ge=0, le=1_000_000)


@router.post("/v1/govern/limits")
@permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
def post_limits(body: LimitsBody, request: Request) -> Response:
    """Govern › Limits' one Save: the monthly cap (the same write as Setup's Budget row), the
    daily cap and burst (written directly; they were reachable only through Pace) and the
    at-limit choice (project setting `budget_at_limit`). Only the fields sent change. An
    `If-Match` with the budget policy's version refuses a stale edit (412)."""
    from boss_engine.services import govern_view, setup_actions, spend_limits
    from boss_engine.services import project_settings as ps
    from boss_engine.services.budget import policy_version

    from ..govern_guard import govern_actor, require_fresh

    main = _main()
    principal = None
    try:
        principal = main.current_api_principal.get()
    except Exception:  # noqa: BLE001
        principal = None
    actor = govern_actor(body.actor, principal)
    micros = lambda usd: int(round(float(usd) * 1_000_000))  # noqa: E731
    with begin(main.engine) as connection:
        main.require_project_actor(connection, body.project_id, actor, "budget_manage")
        require_fresh(request, *policy_version(connection, body.project_id))
        try:
            if body.monthly_cap_usd is not None:
                setup_actions.set_budget(connection, main.settings, body.project_id,
                                         body.monthly_cap_usd, actor=actor, via="govern")
            spend_limits.set_caps(
                connection, body.project_id,
                daily_cap_usd_micros=(micros(body.daily_cap_usd)
                                      if body.daily_cap_usd is not None else None),
                burst_cap_usd_micros_per_min=(micros(body.burst_usd_per_min)
                                              if body.burst_usd_per_min is not None else None),
                actor=actor)
            if body.at_limit is not None:
                ps.set_one(connection, body.project_id, spend_limits.AT_LIMIT_KNOB,
                           body.at_limit, actor, scope="project")
            from boss_engine.services import unpriced_policy

            if body.unpriced_policy is not None:
                ps.set_one(connection, body.project_id, unpriced_policy.POLICY_KNOB,
                           body.unpriced_policy, actor, scope="project")
            if body.unpriced_allowance_usd is not None:
                ps.set_one(connection, body.project_id, unpriced_policy.ALLOWANCE_KNOB,
                           micros(body.unpriced_allowance_usd), actor, scope="project")
        except setup_actions.SetupActionError as error:
            said = getattr(error, "detail", {}) or {}
            raise HTTPException(status_code=422, detail=" ".join(
                str(said.get(k) or "") for k in ("headline", "fix")).strip() or str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        view = govern_view.build(connection, main.settings, body.project_id)
    return JSONResponse(content=jsonable_encoder(view))


class ProjectLimitBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    #: None removes the limit ("No limit").
    monthly_limit_usd: Optional[float] = Field(default=None, gt=0, le=1_000_000)


@router.post("/v1/govern/project-limit")
@permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
def post_project_limit(body: ProjectLimitBody) -> dict[str, Any]:
    """A project's monthly limit: creates or updates its project budget envelope (scope_type
    project), or closes it for no limit. BudgetGovernor.reserve enforces it like a cap."""
    from boss_engine.services import spend_limits

    from ..govern_guard import govern_actor

    main = _main()
    try:
        principal = main.current_api_principal.get()
    except Exception:  # noqa: BLE001
        principal = None
    actor = govern_actor(body.actor, principal)
    with begin(main.engine) as connection:
        main.require_project_actor(connection, body.project_id, actor, "budget_manage")
        try:
            return spend_limits.set_project_limit(
                connection, body.project_id,
                (int(round(body.monthly_limit_usd * 1_000_000))
                 if body.monthly_limit_usd is not None else None),
                actor=actor)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


__all__ = ["router"]
