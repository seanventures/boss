"""The egress proxy as an ASGI app. Run next to the API, and as a sidecar on remote worker hosts.

    /github/<owner>/<repo>.git/...   git smart-HTTP (clone, fetch, push)
    /github-api/...                  the GitHub REST API
    /_boss/register                  the run supervisor hands over a run's egress token
                                     (server to server, `X-Boss-Egress-Key`: BOSS_EGRESS_SERVICE_KEY)
    /_boss/healthz                   liveness

On start it LISTENs on `identity_revocation`: a cascade's runs have their GitHub installation tokens
revoked at GitHub immediately (the per-request live check already refuses them).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from boss_engine.identity import cascade, github_app, runs, tokens

from .proxy import EgressProxy, EgressRequest

log = logging.getLogger("boss.egress")


def _engine():
    from sqlalchemy import create_engine

    url = os.environ.get("DATABASE_URL") or os.environ.get("BOSS_DATABASE_URL")
    if not url:
        raise RuntimeError("boss-egress needs DATABASE_URL")
    return create_engine(url.replace("postgresql://", "postgresql+psycopg://"), pool_size=5, max_overflow=5)


def listen_for_revocations(engine: Any, stop: threading.Event) -> None:
    """LISTEN identity_revocation -> revoke the runs' GitHub installation tokens at GitHub."""
    raw = engine.raw_connection()
    try:
        pg = raw.driver_connection          # psycopg 3
        pg.autocommit = True
        pg.execute(f"LISTEN {cascade.NOTIFY_CHANNEL}")
        while not stop.is_set():
            for note in pg.notifies(timeout=1.0, stop_after=1):
                try:
                    payload = json.loads(note.payload)
                    github_app.revoke_runs(list(payload.get("runs") or []), "cascade")
                except Exception:  # noqa: BLE001
                    log.warning("could not process a revocation notice")
    finally:
        raw.close()


def build_app(engine: Optional[Any] = None, *, upstream=None, listen: bool = False) -> Starlette:
    engine = engine or _engine()
    proxy = EgressProxy(engine, upstream=upstream)
    github_app.install_cascade_hook()

    async def forward(request: Request) -> Response:
        body = await request.body()
        req = EgressRequest(method=request.method, path=request.url.path, query=request.url.query,
                            headers={k.lower(): v for k, v in request.headers.items()}, body=body)
        from starlette.concurrency import run_in_threadpool

        resp = await run_in_threadpool(proxy.handle, req)
        return Response(resp.body, status_code=resp.status, headers=dict(resp.headers))

    async def register(request: Request) -> Response:
        if not tokens.egress_service_key_ok(request.headers.get("x-boss-egress-key")):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        data = await request.json()
        try:
            with engine.begin() as db:
                runs.register_egress(raw_hash=str(data["credential_hash"]), token=str(data["token"]), db=db)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        return JSONResponse({"ok": True})

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/_boss/healthz", healthz, methods=["GET"]),
        Route("/_boss/register", register, methods=["POST"]),
        Route("/github/{path:path}", forward, methods=["GET", "POST"]),
        Route("/github-api/{path:path}", forward, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ])
    if listen:
        stop = threading.Event()
        threading.Thread(target=listen_for_revocations, args=(engine, stop), daemon=True,
                         name="egress-revocations").start()
        app.state.stop_listening = stop
    return app
