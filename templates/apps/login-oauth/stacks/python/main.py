"""A runnable example: a FastAPI app with login-oauth mounted. In an existing app, add the
session middleware and the router to the app's own FastAPI instance and protect routes with
`Depends(auth.require_user)` / `Depends(auth.require_role("admin"))`.

Behind a TLS-terminating proxy run uvicorn with `--proxy-headers` (trusting only that proxy),
so the app sees https and the Secure session cookie works.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from auth import create_auth, load_config
from auth.config import Config
from auth.routes import error_body
from auth.sessions import ServerSessionMiddleware


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or load_config()
    auth = create_auth(config)
    app = FastAPI()
    app.add_middleware(ServerSessionMiddleware, **auth.session_settings)
    app.include_router(auth.router)
    app.add_exception_handler(HTTPException, lambda request, exc: error_body(exc))

    @app.get("/", response_class=PlainTextResponse)
    def home(request: Request) -> str:
        user = auth.current_user(request)
        return f"Signed in as {user.email or user.name}" if user else "Signed out"

    app.state.auth = auth
    return app


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), proxy_headers=True)
