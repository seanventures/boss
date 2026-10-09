"""Stripe Checkout for a FastAPI app (template `stripe-checkout`).

  POST /api/checkout            body {cart_id} or {items:[{product_id, quantity}]}
                                -> {order_id, session_id, checkout_url}; prices from the server
  GET  /checkout/success        ?session_id=... retrieves the session FROM STRIPE and fulfils
  GET  /checkout/cancel         the buyer came back without paying
  POST /webhooks/stripe         signed (Stripe-Signature), idempotent by event id
  GET  /api/orders/{id}         the order's status (the UI polls it)
  POST /api/orders/{id}/refund  optional; PAYMENTS_REFUNDS_ENABLED=true AND is_admin(request)
  GET  /api/checkout/config     publishable key + mode (for the UI's test-mode banner)

Fulfilment has two paths and both are idempotent: the success page (works on a private preview,
where Stripe cannot reach the webhook) and the webhook (authoritative; also covers a buyer who
closes the tab before the redirect).
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Callable, Optional

import stripe
from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .catalog import CheckoutInputError, price_line_items
from .config import StripeConfig
from .store import TransitionError

log = logging.getLogger("payments.stripe")
_SESSION_ID = re.compile(r"^cs_[A-Za-z0-9_]+$")


def _plain(obj: Any) -> dict:
    """A Stripe API object (or an already-parsed dict) as a plain dict."""
    if isinstance(obj, dict):
        return obj
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def create_stripe_router(*, client: stripe.StripeClient, config: StripeConfig, store: Any, catalog: Any,
                         is_admin: Callable[[Request], bool] = lambda request: False,
                         on_order_paid: Callable[[dict], None] = lambda order: None) -> APIRouter:
    router = APIRouter()

    def safe_transition(order: Optional[dict], to: str) -> bool:
        if not order:
            return False
        try:
            return store.transition(order["id"], to)
        except TransitionError:
            return False  # e.g. a late "failed" after "paid": ignore

    def order_of_session(session: dict) -> Optional[dict]:
        order_id = (session.get("metadata") or {}).get("order_id")
        order = store.get_order(order_id) if order_id else None
        if not order or order["provider"] != "stripe" or order["provider_ref"] != session.get("id"):
            return None
        return order

    def fulfil_from_session(session: dict) -> Optional[dict]:
        """Never trusts the session blindly: it must be the session this app created for that
        order, paid, for the amount and currency the server priced."""
        order = order_of_session(session)
        if order is None:
            return None
        if session.get("payment_status") != "paid":
            return order
        if session.get("amount_total") != order["amount_cents"] or \
                str(session.get("currency") or "").lower() != order["currency"]:
            if safe_transition(order, "needs_review"):
                log.warning("stripe: order %s amount mismatch; needs review", order["id"])
            return store.get_order(order["id"])
        if order["status"] in ("paid", "refunded", "needs_review"):
            return order
        if session.get("payment_intent"):
            store.set_payment_ref(order["id"], str(session["payment_intent"]))
        if order["status"] == "cancelled":
            # Money arrived for an order we had given up on: hold it for a person, never ship silently.
            safe_transition(order, "needs_review")
            return store.get_order(order["id"])
        if safe_transition(order, "paid"):
            on_order_paid(store.get_order(order["id"]))
        return store.get_order(order["id"])

    def order_of_payment_intent(pi: dict) -> Optional[dict]:
        order_id = (pi.get("metadata") or {}).get("order_id")
        order = store.get_order(order_id) if order_id else None
        return order if order and order["provider"] == "stripe" else None

    # ---- webhook: the RAW body is verified, before anything parses it -------------------------
    @router.post("/webhooks/stripe")
    async def stripe_webhook(request: Request):
        payload = await request.body()
        signature = request.headers.get("stripe-signature")
        try:
            if not signature or len(payload) > 1_000_000:
                raise ValueError("missing signature or body too large")
            stripe.Webhook.construct_event(payload, signature, config.webhook_secret,
                                           tolerance=config.webhook_tolerance_seconds)
            event = json.loads(payload)  # verified above
        except (ValueError, stripe.SignatureVerificationError):
            # Bad signature, stale timestamp or malformed payload: refuse, and do not say which.
            return JSONResponse({"error": "invalid signature"}, status_code=400)

        def apply() -> Any:
            obj = (event.get("data") or {}).get("object") or {}
            kind = event.get("type")
            if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
                return fulfil_from_session(obj)
            if kind == "checkout.session.async_payment_failed":
                return safe_transition(order_of_session(obj), "failed")
            if kind == "checkout.session.expired":
                return safe_transition(order_of_session(obj), "cancelled")
            if kind == "payment_intent.payment_failed":
                return safe_transition(order_of_payment_intent(obj), "failed")
            if kind == "charge.refunded" and obj.get("refunded") and obj.get("payment_intent"):
                # Only a FULL refund marks the order refunded; a partial one stays paid.
                return safe_transition(store.find_by_payment_ref("stripe", str(obj["payment_intent"])), "refunded")
            return None  # acknowledged, not used

        try:
            out = store.process_event_once(str(event.get("id")), "stripe", str(event.get("type")), apply)
        except Exception as exc:  # noqa: BLE001 — Stripe retries a 5xx
            log.error("stripe: webhook %s failed: %s", event.get("type"), type(exc).__name__)
            return JSONResponse({"error": "processing failed"}, status_code=500)
        return {"received": True, "duplicate": out["duplicate"]}

    @router.get("/api/checkout/config")
    def checkout_config():
        return {"mode": config.mode, "publishable_key": config.publishable_key or None}

    @router.post("/api/checkout", status_code=201)
    def checkout(request: Request, body: Any = Body(None)):
        # A plain `def`: FastAPI runs it in a worker thread, so the blocking Stripe call does not
        # stall the event loop.
        try:
            priced = price_line_items(catalog, body, request)
        except CheckoutInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        cart_id = body.get("cart_id") if isinstance(body, dict) else None
        order = store.create_order(provider="stripe", items=priced["lines"], amount_cents=priced["amount_cents"],
                                   currency=priced["currency"], cart_id=None if cart_id is None else str(cart_id))
        base = config.base_url or str(request.base_url).rstrip("/")
        try:
            session = _plain(client.v1.checkout.sessions.create(params={
                "mode": "payment",
                "line_items": [{"quantity": l["quantity"],
                                "price_data": {"currency": l["currency"], "unit_amount": l["unit_amount_cents"],
                                               "product_data": {"name": l["name"]}}} for l in priced["lines"]],
                "client_reference_id": order["id"],
                "metadata": {"order_id": order["id"]},
                "payment_intent_data": {"metadata": {"order_id": order["id"]}},
                "success_url": f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url": f"{base}/checkout/cancel?order_id={order['id']}",
            }, options={"idempotency_key": f"checkout-{order['id']}"}))
        except stripe.StripeError as exc:
            safe_transition(order, "cancelled")
            log.error("stripe: could not create a Checkout Session: %s", type(exc).__name__)
            return JSONResponse({"error": "the payment provider is unavailable; try again"}, status_code=502)
        store.set_provider_ref(order["id"], session["id"])
        return {"order_id": order["id"], "session_id": session["id"], "checkout_url": session.get("url")}

    @router.get("/checkout/success")
    def checkout_success(request: Request, session_id: str = ""):
        if not _SESSION_ID.match(session_id):
            return _reply(request, 400, {"error": "missing session"})
        try:
            session = _plain(client.v1.checkout.sessions.retrieve(session_id))
        except stripe.StripeError:
            return _reply(request, 404, {"error": "unknown session"})
        order = fulfil_from_session(session)
        if order is None:
            return _reply(request, 404, {"error": "unknown order"})
        return _reply(request, 200, {"order_id": order["id"], "status": order["status"]})

    @router.get("/checkout/cancel")
    def checkout_cancel(request: Request, order_id: str = ""):
        order = store.get_order(order_id) if order_id else None
        return _reply(request, 200, {"order_id": order["id"] if order else None,
                                     "status": order["status"] if order else None, "cancelled": True})

    @router.get("/api/orders/{order_id}")
    def get_order(order_id: str):
        order = store.get_order(order_id)
        if not order or order["provider"] != "stripe":
            return JSONResponse({"error": "not found"}, status_code=404)
        keys = ("id", "status", "amount_cents", "currency", "items", "created_at", "paid_at", "refunded_at")
        return {k: order[k] for k in keys}

    @router.post("/api/orders/{order_id}/refund")
    def refund(order_id: str, request: Request):
        if not config.refunds_enabled:
            return JSONResponse({"error": "refunds are not enabled"}, status_code=404)
        if not is_admin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        order = store.get_order(order_id)
        if not order or order["provider"] != "stripe":
            return JSONResponse({"error": "not found"}, status_code=404)
        if order["status"] != "paid":
            return JSONResponse({"error": f"order is {order['status']}"}, status_code=409)
        if not order["payment_ref"]:
            return JSONResponse({"error": "order has no recorded payment"}, status_code=409)
        try:
            # Full refund of the server-recorded amount; the client never names an amount.
            client.v1.refunds.create(params={"payment_intent": order["payment_ref"], "amount": order["amount_cents"]},
                                     options={"idempotency_key": f"refund-{order['id']}"})
        except stripe.StripeError as exc:
            log.error("stripe: refund failed: %s", type(exc).__name__)
            return JSONResponse({"error": "the payment provider refused the refund"}, status_code=502)
        safe_transition(order, "refunded")
        return {"id": order["id"], "status": store.get_order(order["id"])["status"]}

    return router


def _reply(request: Request, status: int, body: dict):
    """JSON for API clients, a small page for a browser coming back from Checkout."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        if body.get("error"):
            title, detail = "Payment problem", body["error"]
        elif body.get("cancelled"):
            title, detail = "Payment cancelled", "You have not been charged."
        elif body.get("status") == "paid":
            title, detail = "Thank you", "Your order is confirmed."
        else:
            title, detail = "Payment processing", "We are confirming your payment; this page updates when it is done."
        order = f"<p>Order <code>{html.escape(body['order_id'])}</code></p>" if body.get("order_id") else ""
        return HTMLResponse(f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title><main>'
                            f"<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>{order}"
                            '<p><a href="/">Back to the shop</a></p></main>', status_code=status)
    return JSONResponse(body, status_code=status)
