from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from .route_ownership import ENTERPRISE_EDITIONS, is_enterprise_path


community_router = APIRouter()


def mount_edition_routers(
    app: FastAPI, edition: str, *, paid_router: APIRouter | None = None
) -> None:
    """Partition registered endpoints and mount the edition-appropriate routers.

    Handler definitions remain colocated while the large legacy ``main.py`` is
    decomposed, but route ownership and runtime exposure are now explicit:
    every non-enterprise HTTP endpoint is mounted through ``community_router``;
    paid endpoints are mounted through the caller-supplied router only in a paid
    edition. The Community route module has no paid-module dependency. Starlette
    system routes and WebSockets remain untouched.
    """

    paid = str(edition).strip().lower() in ENTERPRISE_EDITIONS
    if paid and paid_router is None:
        raise ValueError("paid edition requires an explicit paid router")

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
        if is_enterprise_path(route.path):
            if paid:
                paid_router.routes.append(route)
        else:
            community_router.routes.append(route)

    app.include_router(community_router)
    if paid:
        app.include_router(paid_router)
