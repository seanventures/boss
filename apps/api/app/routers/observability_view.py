"""Observability's view endpoints (infra-observability spec §6.2).

Apps (the Pulse and one app's drawer), Models (the llm_calls ledger), System (the stability
work's machine, findings and actions, the queue and stalls) and Sources (the monitoring stack).
Every GET is ETag'd and polled through `usePolled`; an unchanged view is a 304.

Mounted from main.py with the auth helpers injected, so this module does not import main.
Stream S2 owns this file.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from boss_engine.db import begin


class ObsBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


def build_observability_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    reader_project: Callable[[str], str],
    viewer_of: Callable[[Optional[str]], Optional[str]],
    etagged: Callable[[Request, Response, dict], Any],
) -> APIRouter:
    from boss_engine.services import obs_view

    router = APIRouter(tags=["observability"])

    def _read(project_id: str, request: Request, response: Response, build: Callable[[Any], dict]):
        pid = reader_project(project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
            try:
                body = build(connection)
            except NotImplementedError as error:
                raise HTTPException(status_code=501, detail="Not built yet.") from error
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
        return etagged(request, response, jsonable_encoder(body))

    @router.get("/v1/observability/apps")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def obs_apps(project_id: str, request: Request, response: Response, win: str = "24h",
                 cells: Optional[int] = None):
        return _read(project_id, request, response, lambda c: obs_view.pulse(
            c, settings(), project_id.strip(), win=obs_view.window_of(win), cells=cells))

    @router.get("/v1/observability/apps/all")
    @authenticated(note="Every app of every project the caller may read: visible_project_rows "
                        "(project:read) narrowed to observability:read per project.")
    def obs_apps_all(request: Request, response: Response, win: str = "24h",
                     cells: Optional[int] = None):
        """Observability › Apps across the organisation (screen 15): one row per app, each with
        its project and a link to that project's Health tab."""
        from apps.api.app.main import visible_project_rows
        from boss_engine.identity import viewer as _viewer

        rows = visible_project_rows(request)
        projects = [{"id": str(r["id"]), "name": str(r["name"] or r["id"])} for r in rows
                    if _viewer.may(str(r["id"]), P.OBSERVABILITY_READ.value) is not False]
        with begin(engine) as connection:
            body = obs_view.apps_across(connection, settings(), projects,
                                        win=obs_view.window_of(win), cells=cells)
        return etagged(request, response, jsonable_encoder(body))

    @router.get("/v1/observability/apps/{target_id}")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def obs_app(target_id: str, project_id: str, request: Request, response: Response,
                win: str = "24h"):
        return _read(project_id, request, response, lambda c: obs_view.app_detail(
            c, settings(), project_id.strip(), target_id, win=obs_view.window_of(win)))

    @router.get("/v1/observability/models")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def obs_models(project_id: str, request: Request, response: Response, win: str = "24h",
                   group: str = "model", metric: str = "tokens"):
        return _read(project_id, request, response, lambda c: obs_view.models(
            c, settings(), project_id.strip(), win=obs_view.window_of(win), group=group,
            metric=metric))

    @router.get("/v1/observability/system")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def obs_system(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: obs_view.system(
            c, settings(), project_id.strip()))

    @router.get("/v1/observability/sources")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def obs_sources(project_id: str, request: Request, response: Response, fresh: bool = False):
        return _read(project_id, request, response, lambda c: obs_view.sources(
            c, settings(), project_id.strip(), fresh=fresh))

    @router.post("/v1/observability/apps/{target_id}/probe")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def obs_probe_app(target_id: str, payload: ObsBody):
        """`Probe now` in the app drawer: probes ONLY this app (operator decision §11.5), not
        the project sweep, records the sample and answers {word, tone, checked_at}. Gated like
        the sweep it replaces for this press: anyone who can read the project."""
        pid = reader_project(payload.project_id)
        with begin(engine) as connection:
            require_reader(connection, pid)
        try:
            return obs_view.probe_app(engine, settings(), pid, target_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    return router
