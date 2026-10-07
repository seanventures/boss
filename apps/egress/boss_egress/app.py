"""The egress proxy as an ASGI app. Run next to the API, and as a sidecar on remote worker hosts.

    /github/<owner>/<repo>.git/...   git smart-HTTP (clone, fetch, push)
    /github-api/...                  the GitHub REST API
    /_boss/register                  the run supervisor hands over a run's egress token
                                     (server to server, `X-Boss-Egress-Key`: BOSS_EGRESS_SERVICE_KEY)
    /_boss/healthz                   liveness

Every request is judged on its RAW path (scope["raw_path"]), canonicalised once by the proxy, and
on its source address (the sandbox's network identity, #16).

On start it LISTENs on `identity_revocation`. A notice's payload is only the id of an
identity_revocation_notices row (so it is never truncated); that row's runs have their GitHub
installation tokens revoked at GitHub immediately (the per-request live check already refuses
them). The listener reconnects after any database error and catches up on every notice written
while it was away.

connect.py serves the sandbox's only other route out, on its own port: an allowlisted HTTPS
CONNECT proxy for model providers and package registries.
"""
from __future__ import annotations

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


def process_notices(engine: Any, after_id: int) -> int:
    """Revoke the GitHub tokens of every notice newer than `after_id`; return the newest id."""
    from sqlalchemy import text

    with engine.begin() as db:
        rows = db.execute(text("SELECT id, runs, reason FROM identity_revocation_notices WHERE id > :a ORDER BY id"),
                          {"a": after_id}).all()
    for row in rows:
        try:
            github_app.revoke_runs(list(row[1] or []), str(row[2] or "cascade")[:200])
        except Exception:  # noqa: BLE001
            log.warning("could not revoke the GitHub tokens of revocation notice %s", row[0])
        after_id = max(after_id, int(row[0]))
    return after_id


def _latest_notice(engine: Any) -> int:
    from sqlalchemy import text

    with engine.begin() as db:
        return int(db.execute(text("SELECT coalesce(max(id), 0) FROM identity_revocation_notices")).scalar() or 0)


def listen_for_revocations(engine: Any, stop: threading.Event, *, backoff_max: float = 30.0,
                           start_after: Optional[int] = None) -> None:
    """LISTEN identity_revocation -> revoke the runs' GitHub installation tokens at GitHub.

    Survives the database going away: on any error it reconnects with backoff, and on every
    (re)connect it first catches up on the notices written while it was not listening."""
    last: Optional[int] = start_after
    delay = 0.5
    while not stop.is_set():
        raw = None
        try:
            if last is None:
                last = _latest_notice(engine)        # tokens minted before this process are not held here
            raw = engine.raw_connection()
            pg = raw.driver_connection               # psycopg 3
            pg.autocommit = True
            pg.execute(f"LISTEN {cascade.NOTIFY_CHANNEL}")
            last = process_notices(engine, last)     # catch up on anything missed while away
            delay = 0.5
            while not stop.is_set():
                for _note in pg.notifies(timeout=1.0, stop_after=1):
                    last = process_notices(engine, last)
        except Exception:  # noqa: BLE001 — the database went away: reconnect
            log.warning("revocation listener lost its connection; reconnecting in %.1fs", delay)
            stop.wait(delay)
            delay = min(delay * 2, backoff_max)
        finally:
            if raw is not None:
                try:
                    raw.close()
                except Exception:  # noqa: BLE001
                    pass


def _raw_path(request: Request) -> str:
    raw = request.scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).split(b"?", 1)[0].decode("latin-1")
    return request.url.path


def build_app(engine: Optional[Any] = None, *, upstream=None, listen: bool = False) -> Starlette:
    engine = engine or _engine()
    proxy = EgressProxy(engine, upstream=upstream)
    github_app.install_cascade_hook()

    async def forward(request: Request) -> Response:
        body = await request.body()
        req = EgressRequest(method=request.method, path=_raw_path(request), query=request.url.query,
                            headers={k.lower(): v for k, v in request.headers.items()}, body=body,
                            peer=(request.client.host if request.client else "unknown"))
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
