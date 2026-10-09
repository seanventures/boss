"""A runnable example: a FastAPI app with catalog-cart mounted. In an existing app, include the
router in the app's own FastAPI instance (with login-oauth applied, after its session
middleware, so the session's user is known and the anonymous cart merges into theirs)."""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import FastAPI, Request

from catalog import CatalogStore, create_catalog, load_catalog_config
from catalog.routes import CatalogConfig, session_user


def create_app(config: Optional[CatalogConfig] = None, *, store: Optional[CatalogStore] = None,
               get_user_id: Callable[[Request], Optional[str]] = session_user) -> FastAPI:
    config = config or load_catalog_config()
    catalog = create_catalog(config, store=store, get_user_id=get_user_id)
    app = FastAPI()
    app.include_router(catalog.router)
    app.state.catalog = catalog
    return app


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), proxy_headers=True)
