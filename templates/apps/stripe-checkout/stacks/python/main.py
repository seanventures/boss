"""How a FastAPI app mounts the `stripe-checkout` template. In an existing app, include the router
from `create_stripe_router(...)` in the app's own FastAPI instance."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import stripe
from fastapi import FastAPI
from fastapi.responses import FileResponse

from payments.catalog import DEFAULT_PRODUCTS, StaticCatalog
from payments.config import load_stripe_config
from payments.store import SqliteOrderStore
from payments.stripe_checkout import create_stripe_router


def create_app(*, env: Optional[Mapping[str, str]] = None, client: Optional[stripe.StripeClient] = None,
               store: Optional[SqliteOrderStore] = None, catalog: Any = None,
               is_admin: Optional[Callable] = None, on_order_paid: Optional[Callable] = None) -> FastAPI:
    config = load_stripe_config(env)  # refuses a live key in test mode, and the reverse
    app = FastAPI()
    app.state.store = store or SqliteOrderStore(os.environ.get("ORDERS_DB", ":memory:"))
    app.include_router(create_stripe_router(
        client=client or stripe.StripeClient(config.secret_key, max_network_retries=2),
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
