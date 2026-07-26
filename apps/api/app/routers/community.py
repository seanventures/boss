from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from .enterprise import (
    ENTERPRISE_EDITIONS,
    enterprise_router,
    is_enterprise_path,
)


community_router = APIRouter()


def mount_edition_routers(app: FastAPI, edition: str) -> None:
    """Partition registered endpoints and mount the edition-appropriate routers.

    Handler definitions remain colocated while the large legacy ``main.py`` is
    decomposed, but route ownership and runtime exposure are now explicit:
    every non-enterprise HTTP endpoint is mounted through ``community_router``;
    paid endpoints are mounted through ``enterprise_router`` only in a paid
    edition. Starlette system routes and WebSockets remain untouched.
    """

    http_routes = [
        route for route in app.router.routes if isinstance(route, APIRoute)
    ]
    app.router.routes = [
        route for route in app.router.routes if not isinstance(route, APIRoute)
    ]

    community_router.routes.clear()
    enterprise_router.routes.clear()
    for route in http_routes:
        target = enterprise_router if is_enterprise_path(route.path) else community_router
        target.routes.append(route)

    app.include_router(community_router)
    if str(edition).strip().lower() in ENTERPRISE_EDITIONS:
        app.include_router(enterprise_router)
