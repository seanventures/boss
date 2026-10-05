"""Observability's view endpoints (infra-observability spec §6.2).

Apps (the Pulse and one app's drawer), Models (the llm_calls ledger), System (the stability
work's machine, findings and actions, the queue and stalls) and Sources (the monitoring stack).
Every GET is ETag'd and polled through `usePolled`; an unchanged view is a 304.

Mounted from main.py with the auth helpers injected, so this module does not import main.
Stream S2 owns this file.
"""
from __future__ import annotations

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
    def obs_apps(project_id: str, request: Request, response: Response, win: str = "24h",
                 cells: Optional[int] = None):
        return _read(project_id, request, response, lambda c: obs_view.pulse(
            c, settings(), project_id.strip(), win=obs_view.window_of(win), cells=cells))

    @router.get("/v1/observability/apps/{target_id}")
    def obs_app(target_id: str, project_id: str, request: Request, response: Response,
                win: str = "24h"):
        return _read(project_id, request, response, lambda c: obs_view.app_detail(
            c, settings(), project_id.strip(), target_id, win=obs_view.window_of(win)))

    @router.get("/v1/observability/models")
    def obs_models(project_id: str, request: Request, response: Response, win: str = "24h",
                   group: str = "model", metric: str = "tokens"):
        return _read(project_id, request, response, lambda c: obs_view.models(
            c, settings(), project_id.strip(), win=obs_view.window_of(win), group=group,
            metric=metric))

    @router.get("/v1/observability/system")
    def obs_system(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: obs_view.system(
            c, settings(), project_id.strip()))

    @router.get("/v1/observability/sources")
    def obs_sources(project_id: str, request: Request, response: Response, fresh: bool = False):
        return _read(project_id, request, response, lambda c: obs_view.sources(
            c, settings(), project_id.strip(), fresh=fresh))

    # Writes (S2): POST /v1/observability/apps/{target_id}/probe — probes ONLY that app
    # (operator decision §11.5), returns {word, tone, checked_at}.

    return router
