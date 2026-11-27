from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from .route_ownership import is_enterprise_path


community_router = APIRouter()


def mount_edition_routers(
    app: FastAPI, edition: str, *, paid_router: APIRouter | None = None
) -> None:
    """Mount all registered control-plane HTTP endpoints unconditionally regardless of edition.

    Starlette system routes and WebSockets remain untouched.
    """
    _ = edition
    http_routes = [
        route for route in app.router.routes if isinstance(route, APIRoute)
    ]
    app.router.routes = [
        route for route in app.router.routes if not isinstance(route, APIRoute)
    ]

    community_router.routes.clear()
    if paid_router is not None:
        paid_router.routes.clear()
    for route in http_routes:
        if paid_router is not None and is_enterprise_path(route.path):
            paid_router.routes.append(route)
        else:
            community_router.routes.append(route)

    app.include_router(community_router)
    if paid_router is not None:
        app.include_router(paid_router)
