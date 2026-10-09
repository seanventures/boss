"""How a FastAPI app mounts the `paypal-checkout` template. In an existing app, include the router
from `create_paypal_router(...)` in the app's own FastAPI instance."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse

from payments.catalog import DEFAULT_PRODUCTS, StaticCatalog
from payments.config import load_paypal_config
from payments.paypal_checkout import create_paypal_router
from payments.paypal_client import PayPalClient
from payments.store import SqliteOrderStore


def create_app(*, env: Optional[Mapping[str, str]] = None, client: Optional[PayPalClient] = None,
               store: Optional[SqliteOrderStore] = None, catalog: Any = None,
               is_admin: Optional[Callable] = None, on_order_paid: Optional[Callable] = None) -> FastAPI:
    config = load_paypal_config(env)  # sandbox unless PAYPAL_ENV=live AND APP_ENV=production
    app = FastAPI()
    app.state.store = store or SqliteOrderStore(os.environ.get("ORDERS_DB", ":memory:"))
    app.include_router(create_paypal_router(
        client=client or PayPalClient(api_base=config.api_base, client_id=config.client_id,
                                      client_secret=config.client_secret),
        config=config,
        store=app.state.store,
        catalog=catalog or StaticCatalog([{**p, "currency": config.currency} for p in DEFAULT_PRODUCTS]),
        # Wire to the app's auth (e.g. login-oauth + a role); default: nobody may refund.
        is_admin=is_admin or (lambda request: False),
        # Ship the goods here; called exactly once per paid order.
        on_order_paid=on_order_paid or (lambda order: None),
    ))

    @app.get("/checkout")
    def checkout_page():
        return FileResponse(Path(__file__).with_name("static") / "checkout.html")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
