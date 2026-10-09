"""PayPal Checkout (Orders v2) for a FastAPI app (template `paypal-checkout`).

  POST /api/paypal/orders              body {cart_id} or {items:[{product_id, quantity}]}
                                       -> {order_id, paypal_order_id, approve_url}; server prices
  POST /api/paypal/orders/{id}/capture {id} = the PayPal order id from the JS SDK's onApprove;
                                       captures only an order THIS app created, once
  GET  /paypal/return                  ?token=<PayPal order id>: captures server-side (private preview)
  GET  /paypal/cancel                  the buyer came back without paying
  POST /webhooks/paypal                verified with PayPal's verify-webhook-signature API, idempotent
  GET  /api/orders/{id}                the order's status
  POST /api/orders/{id}/refund         optional; PAYMENTS_REFUNDS_ENABLED=true AND is_admin(request)
  GET  /api/paypal/config              client id + env + currency for the JS SDK (never the secret)
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .catalog import CheckoutInputError, price_line_items
from .config import PayPalConfig
from .paypal_client import PayPalClient, PayPalError
from .store import TransitionError

log = logging.getLogger("payments.paypal")
ZERO_DECIMAL = {"jpy", "huf", "twd"}  # currencies PayPal takes without decimals
_PAYPAL_ORDER_ID = re.compile(r"^[A-Z0-9]{10,40}$")
_SIGNATURE_HEADERS = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                      "paypal-transmission-sig", "paypal-transmission-time")


def to_value(minor: int, currency: str) -> str:
    """Integer minor units -> PayPal's decimal string, without floating point."""
    if currency in ZERO_DECIMAL:
        return str(minor)
    return f"{minor // 100}.{minor % 100:02d}"


def from_value(value: Any, currency: str) -> Optional[int]:
    """PayPal's decimal string -> integer minor units, or None if it is not a plain amount."""
    s = str(value or "")
    if currency in ZERO_DECIMAL:
        return int(s) if re.fullmatch(r"\d+", s) else None
    m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", s)
    return int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0")) if m else None


def money(minor: int, currency: str) -> dict:
    return {"currency_code": currency.upper(), "value": to_value(minor, currency)}


def _first_capture(paypal_order: dict) -> Optional[dict]:
    units = paypal_order.get("purchase_units") or [{}]
    captures = ((units[0] or {}).get("payments") or {}).get("captures") or []
    return captures[0] if captures else None


def create_paypal_router(*, client: PayPalClient, config: PayPalConfig, store: Any, catalog: Any,
                         is_admin: Callable[[Request], bool] = lambda request: False,
                         on_order_paid: Callable[[dict], None] = lambda order: None) -> APIRouter:
    router = APIRouter()

    def safe_transition(order: Optional[dict], to: str) -> bool:
        if not order:
            return False
        try:
            return store.transition(order["id"], to)
        except TransitionError:
            return False  # e.g. a late "denied" after "paid"

    def apply_capture(order: Optional[dict], capture: Optional[dict]) -> Optional[dict]:
        """Pays the order only for a COMPLETED capture of exactly the priced amount and currency."""
        if not order or not capture:
            return order
        if capture.get("status") in ("DECLINED", "FAILED"):
            safe_transition(order, "failed")
            return store.get_order(order["id"])
        if capture.get("status") != "COMPLETED":
            return store.get_order(order["id"])  # PENDING: the webhook finishes it
        amount = capture.get("amount") or {}
        currency = str(amount.get("currency_code") or "").lower()
        if currency != order["currency"] or from_value(amount.get("value"), currency) != order["amount_cents"]:
            if safe_transition(order, "needs_review"):
                log.warning("paypal: order %s amount mismatch; needs review", order["id"])
            return store.get_order(order["id"])
        current = store.get_order(order["id"])
        if current["status"] in ("paid", "refunded", "needs_review"):
            return current
        if capture.get("id"):
            store.set_payment_ref(order["id"], str(capture["id"]))
        if current["status"] == "cancelled":
            safe_transition(current, "needs_review")  # money for an order we gave up on: a person decides
            return store.get_order(order["id"])
        if safe_transition(current, "paid"):
            on_order_paid(store.get_order(order["id"]))
        return store.get_order(order["id"])

    def capture(order: dict) -> tuple[dict, bool]:
        """Capture our order's PayPal order once. Returns (order, declined)."""
        if order["status"] not in ("pending", "failed"):
            return order, False  # already paid (or held): no second call to PayPal
        try:
            result = client.capture_order(order["provider_ref"], f"capture-{order['id']}")
            return apply_capture(order, _first_capture(result)), False
        except PayPalError as exc:
            if "ORDER_ALREADY_CAPTURED" in exc.issues():
                # Captured elsewhere (another tab, the return page): read the truth back from PayPal.
                return apply_capture(order, _first_capture(client.get_order(order["provider_ref"]))), False
            if "INSTRUMENT_DECLINED" in exc.issues():
                # PayPal's guidance: let the buyer choose another funding source; the order stays open.
                return store.get_order(order["id"]), True
            raise

    def capture_by_paypal_id(paypal_order_id: str) -> tuple[int, dict]:
        if not _PAYPAL_ORDER_ID.match(paypal_order_id):
            return 400, {"error": "bad order id"}
        order = store.find_by_provider_ref("paypal", paypal_order_id)
        if not order:
            return 404, {"error": "unknown order"}  # never capture someone else's order
        try:
            out, declined = capture(order)
        except (PayPalError, httpx.HTTPError) as exc:
            log.error("paypal: capture failed: %s", type(exc).__name__)
            return 502, {"error": "the payment provider is unavailable; try again"}
        if declined:
            return 402, {"order_id": order["id"], "status": out["status"], "error": "the payment was declined",
                         "restart": True}
        return 200, {"order_id": order["id"], "status": out["status"]}

    # ---- webhook: verified by PayPal before anything acts on it -------------------------------
    @router.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        raw = await request.body()
        headers = {h: request.headers.get(h) for h in _SIGNATURE_HEADERS}
        try:
            if not config.webhook_id or not all(headers.values()) or len(raw) > 1_000_000:
                raise ValueError("unsigned")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an event")
            ok = await run_in_threadpool(client.verify_webhook, headers=headers, event=event,
                                         webhook_id=config.webhook_id)
            if not ok:
                raise ValueError("verification failed")
        except (ValueError, PayPalError, httpx.HTTPError):
            return JSONResponse({"error": "invalid signature"}, status_code=400)  # do not say which check

        def apply() -> Any:
            r = event.get("resource") or {}
            related = ((r.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
            # The capture must belong to a PayPal order THIS app created, for the order it names.
            order = store.find_by_provider_ref("paypal", str(related)) if related else None
            owned = order if order and (not r.get("custom_id") or r.get("custom_id") == order["id"]) else None
            kind = event.get("event_type")
            if kind == "PAYMENT.CAPTURE.COMPLETED":
                return apply_capture(owned, {**r, "status": "COMPLETED"})
            if kind in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
                return safe_transition(owned, "failed")
            if kind == "PAYMENT.CAPTURE.REFUNDED":
                up = next((l for l in r.get("links") or [] if l.get("rel") == "up"), None)
                capture_id = re.split(r"[/?]", str(up["href"]).split("/captures/")[1])[0] \
                    if up and "/captures/" in str(up.get("href")) else None
                return safe_transition(store.find_by_payment_ref("paypal", capture_id), "refunded") if capture_id else None
            return None  # acknowledged, not used

        try:
            out = store.process_event_once(str(event.get("id")), "paypal", str(event.get("event_type")), apply)
        except Exception as exc:  # noqa: BLE001 — PayPal retries a 5xx
            log.error("paypal: webhook %s failed: %s", event.get("event_type"), type(exc).__name__)
            return JSONResponse({"error": "processing failed"}, status_code=500)
        return {"received": True, "duplicate": out["duplicate"]}

    @router.get("/api/paypal/config")
    def paypal_config():
        return {"env": config.env, "client_id": config.client_id, "currency": config.currency.upper()}

    @router.post("/api/paypal/orders", status_code=201)
    def create_order(request: Request, body: Any = Body(None)):
        try:
            priced = price_line_items(catalog, body, request)
        except CheckoutInputError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        cart_id = body.get("cart_id") if isinstance(body, dict) else None
        order = store.create_order(provider="paypal", items=priced["lines"], amount_cents=priced["amount_cents"],
                                   currency=priced["currency"], cart_id=None if cart_id is None else str(cart_id))
        base = config.base_url or str(request.base_url).rstrip("/")
        total = money(priced["amount_cents"], priced["currency"])
        try:
            created = client.create_order({
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": order["id"], "custom_id": order["id"],
                    "amount": {**total, "breakdown": {"item_total": total}},
                    "items": [{"name": l["name"][:127], "sku": l["product_id"][:127], "quantity": str(l["quantity"]),
                               "unit_amount": money(l["unit_amount_cents"], l["currency"])} for l in priced["lines"]],
                }],
                "payment_source": {"paypal": {"experience_context": {
                    "return_url": f"{base}/paypal/return", "cancel_url": f"{base}/paypal/cancel",
                    "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"}}},
            }, f"create-{order['id']}")
        except (PayPalError, httpx.HTTPError) as exc:
            safe_transition(order, "cancelled")
            log.error("paypal: could not create an order: %s", type(exc).__name__)
            return JSONResponse({"error": "the payment provider is unavailable; try again"}, status_code=502)
        store.set_provider_ref(order["id"], str(created["id"]))
        approve = next((l for l in created.get("links") or [] if l.get("rel") in ("payer-action", "approve")), None)
        return {"order_id": order["id"], "paypal_order_id": created["id"], "approve_url": approve["href"] if approve else None}

    @router.post("/api/paypal/orders/{paypal_order_id}/capture")
    def capture_order(paypal_order_id: str):
        status, body = capture_by_paypal_id(paypal_order_id)
        return JSONResponse(body, status_code=status)

    @router.get("/paypal/return")
    def paypal_return(request: Request, token: str = ""):
        status, body = capture_by_paypal_id(token)
        return _reply(request, status, body)

    @router.get("/paypal/cancel")
    def paypal_cancel(request: Request, token: str = ""):
        order = store.find_by_provider_ref("paypal", token) if token else None
        return _reply(request, 200, {"order_id": order["id"] if order else None,
                                     "status": order["status"] if order else None, "cancelled": True})

    @router.get("/api/orders/{order_id}")
    def get_order(order_id: str):
        order = store.get_order(order_id)
        if not order or order["provider"] != "paypal":
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
        if not order or order["provider"] != "paypal":
            return JSONResponse({"error": "not found"}, status_code=404)
        if order["status"] != "paid":
            return JSONResponse({"error": f"order is {order['status']}"}, status_code=409)
        if not order["payment_ref"]:
            return JSONResponse({"error": "order has no recorded payment"}, status_code=409)
        try:
            # Full refund of the server-recorded amount; the client never names an amount.
            client.refund_capture(order["payment_ref"], money(order["amount_cents"], order["currency"]),
                                  f"refund-{order['id']}")
        except (PayPalError, httpx.HTTPError) as exc:
            log.error("paypal: refund failed: %s", type(exc).__name__)
            return JSONResponse({"error": "the payment provider refused the refund"}, status_code=502)
        safe_transition(order, "refunded")
        return {"id": order["id"], "status": store.get_order(order["id"])["status"]}

    return router


def _reply(request: Request, status: int, body: dict):
    """JSON for API clients, a small page for a browser coming back from PayPal."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        if body.get("error"):
            title, detail = "Payment problem", body["error"]
        elif body.get("cancelled"):
            title, detail = "Payment cancelled", "You have not been charged."
        elif body.get("status") == "paid":
            title, detail = "Thank you", "Your order is confirmed."
        else:
            title, detail = "Payment processing", "We are confirming your payment."
        order = f"<p>Order <code>{html.escape(body['order_id'])}</code></p>" if body.get("order_id") else ""
        return HTMLResponse(f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title><main>'
                            f"<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>{order}"
                            '<p><a href="/">Back to the shop</a></p></main>', status_code=status)
    return JSONResponse(body, status_code=status)
