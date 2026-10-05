"""Infra's view endpoints and routed actions (infra-observability spec §6.2, §6.5).

One ETag'd view per screen, polled by the console through `usePolled`; an unchanged view is a
304. Every write keeps its existing route; the few new ones here route by kind so the console
has one button where it had three (`Deploy`, `Roll back`).

Mounted from main.py with the auth helpers injected, so this module does not import main.
Stream S1 owns this file.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from boss_engine.db import begin


class InfraBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class TargetDeployBody(InfraBody):
    ref: Optional[str] = None
    #: registry-only extras (`More`): dollars, converted to micros server-side.
    expected_cost_usd: Optional[float] = None
    budget_reservation_id: Optional[str] = None
    release_packet: Optional[dict[str, Any]] = None


class TargetMoveBody(InfraBody):
    host_id: str


class AttentionActBody(InfraBody):
    id: str
    option: str


class HostRetireBody(InfraBody):
    confirm_name: str
    revoke_key: bool = False
    uninstall_docker: bool = False
    delete_record: bool = False
    detach_apps: bool = False


def build_infra_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
    reader_project: Callable[[str], str],
    viewer_of: Callable[[Optional[str]], Optional[str]],
    etagged: Callable[[Request, Response, dict], Any],
) -> APIRouter:
    from boss_engine.services import infra_view

    router = APIRouter(tags=["infra"])

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

    @router.get("/v1/infra/fleet")
    def infra_fleet(project_id: str, request: Request, response: Response,
                    viewer: Optional[str] = None):
        """Machines, what is placed on them, what is not placed, and what needs you (§4.3)."""
        return _read(project_id, request, response, lambda c: infra_view.fleet(
            c, settings(), project_id.strip(), viewer=viewer_of(viewer)))

    @router.get("/v1/infra/hosts/{host_id}")
    def infra_host(host_id: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.host_detail(
            c, settings(), project_id.strip(), host_id))

    @router.get("/v1/infra/targets/{target_id}")
    def infra_target(target_id: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.target_detail(
            c, settings(), project_id.strip(), target_id))

    @router.get("/v1/infra/deploys")
    def infra_deploys(project_id: str, request: Request, response: Response,
                      state: Optional[str] = None, app: Optional[str] = None,
                      env: Optional[str] = None, cursor: Optional[str] = None, limit: int = 30,
                      viewer: Optional[str] = None):
        return _read(project_id, request, response, lambda c: infra_view.deploy_rows(
            c, settings(), project_id.strip(), state=state, app=app, env=env, cursor=cursor,
            limit=max(1, min(int(limit), 100)), viewer=viewer_of(viewer)))

    @router.get("/v1/infra/deploys/{ref}")
    def infra_deploy(ref: str, project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.deploy_detail(
            c, settings(), project_id.strip(), ref))

    @router.get("/v1/infra/previews")
    def infra_previews(project_id: str, request: Request, response: Response):
        return _read(project_id, request, response, lambda c: infra_view.preview_rows(
            c, settings(), project_id.strip()))

    @router.get("/v1/infra/cloud")
    def infra_cloud(project_id: str, request: Request, response: Response,
                    view: str = "accounts", viewer: Optional[str] = None,
                    provider: Optional[str] = None, account: Optional[str] = None,
                    env: Optional[str] = None, region: Optional[str] = None,
                    service: Optional[str] = None):
        filters = {k: v for k, v in {"provider": provider, "account": account, "env": env,
                                      "region": region, "service": service}.items() if v}
        return _read(project_id, request, response, lambda c: infra_view.cloud(
            c, settings(), project_id.strip(), view, viewer=viewer_of(viewer), filters=filters))

    # Writes (S1): POST /v1/infra/targets/{id}/deploy, /v1/infra/targets/{id}/move,
    # /v1/infra/deploys/{ref}/rollback, /v1/infra/hosts/{id}/retire,
    # /v1/infra/previews/{id}/restart, /v1/infra/attention/act — bodies above.

    return router
