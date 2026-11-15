"""The model gateway as an ASGI app (Starlette, streaming).

    /<provider>/<path>      the model route: boss-hostd forwards a guest's call here with the
                            HOST's identity (HOST_API.md §4.6); the answer streams back chunk
                            by chunk
    /_boss/handles          POST, server to server (`X-Boss-Egress-Key`): issue a handle for a
                            run (the local lane's thin client, BOSS_MODEL_PROXY_VIA_GATEWAY)
    /_boss/healthz          liveness

Everything that decides lives in boss_engine.services.streaming_model_gateway.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from boss_engine.identity import tokens
from boss_engine.services import streaming_model_gateway as smg

log = logging.getLogger("boss.model_gateway")


def _engine():
    from sqlalchemy import create_engine

    url = os.environ.get("DATABASE_URL") or os.environ.get("BOSS_DATABASE_URL")
    if not url:
        raise RuntimeError("boss-model-gateway needs DATABASE_URL")
    return create_engine(url.replace("postgresql://", "postgresql+psycopg://"), pool_size=8,
                         max_overflow=16, pool_pre_ping=True)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def build_app(engine: Optional[Any] = None, *, settings: Any = None,
              gateway: Optional[smg.ModelGateway] = None, **gateway_kw: Any) -> Starlette:
    engine = engine or _engine()
    settings = settings or _settings()
    gateway = gateway or smg.ModelGateway(engine, settings, **gateway_kw)

    async def forward(request: Request) -> Response:
        return await gateway.handle(request)

    async def issue(request: Request) -> Response:
        if not tokens.egress_service_key_ok(request.headers.get("x-boss-egress-key")):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        try:
            data = await request.json()
            from sqlalchemy import text

            from starlette.concurrency import run_in_threadpool

            def mint() -> str:
                with engine.begin() as connection:
                    org = connection.execute(text(
                        "SELECT w.org_id FROM projects p JOIN workspaces w "
                        "ON w.id = p.workspace_id WHERE p.id = :p"),
                        {"p": str(data["project_id"])}).scalar()
                    if not org:
                        raise KeyError("project")
                    # The run's CURRENT epoch, read here (never the caller's word); never an
                    # upstream of the caller's choosing (the provider's own API, or the
                    # operator's gateway through the credential reference).
                    run_id = str(data["run_id"])
                    epoch = smg.current_epoch(connection, run_id)
                    return smg.issue_handle(
                        connection, org_id=str(org), project_id=str(data["project_id"]),
                        run_id=run_id, epoch=0 if epoch is None else epoch,
                        vm_id=None, provider=str(data["provider"]),
                        credential_ref=str(data["credential_ref"]),
                        models=[str(m) for m in (data.get("models") or [])],
                        ttl_seconds=int(data.get("ttl_seconds") or smg.HANDLE_TTL_SECONDS))

            raw = await run_in_threadpool(mint)
        except (KeyError, ValueError, TypeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        return JSONResponse({"handle": raw})

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True, "pending_audit": gateway.audit.pending()})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        gateway.audit.start()       # the audit batch writer, never on the request path
        try:
            yield
        finally:
            gateway.audit.stop()
            await smg.close_client()

    app = Starlette(routes=[
        Route("/_boss/healthz", healthz, methods=["GET"]),
        Route("/_boss/handles", issue, methods=["POST"]),
        Route("/{provider}/{path:path}", forward, methods=["GET", "POST"]),
    ], lifespan=lifespan)
    app.state.gateway = gateway
    return app
