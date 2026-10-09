"""The `stripe-checkout` template's own tests, against its HTTP routes (not internals), with Stripe
replaced by an in-process fake. No test reaches the network or uses a real key."""
from __future__ import annotations

import json
import secrets
import time
import uuid

import pytest
import stripe
from fastapi.testclient import TestClient

from fake_stripe import FakeStripe, new_test_keys, new_webhook_secret, sign_stripe_payload
from main import create_app
from payments.catalog import DEFAULT_PRODUCTS, StaticCatalog
from payments.config import ConfigError, load_stripe_config


@pytest.fixture(scope="module")
def fake():
    f = FakeStripe()
    yield f
    f.close()


class Shop:
    def __init__(self, fake: FakeStripe, *, env=None, catalog=None, is_admin=None):
        secret_key, publishable_key = new_test_keys()
        self.webhook_secret = new_webhook_secret()
        full_env = {"STRIPE_SECRET_KEY": secret_key, "STRIPE_PUBLISHABLE_KEY": publishable_key,
                    "STRIPE_WEBHOOK_SECRET": self.webhook_secret, "APP_BASE_URL": "http://shop.test", **(env or {})}
        client = stripe.StripeClient(secret_key, base_addresses={"api": fake.url}, max_network_retries=0)
        self.paid: list[str] = []
        self.app = create_app(env=full_env, client=client, catalog=catalog, is_admin=is_admin,
                              on_order_paid=lambda o: self.paid.append(o["id"]))
        self.store = self.app.state.store
        self.http = TestClient(self.app, headers={"Accept": "application/json"})
        self.fake = fake

    def checkout(self, items=None) -> dict:
        r = self.http.post("/api/checkout", json={"items": items or [{"product_id": "starter-pack", "quantity": 2}]})
        assert r.status_code == 201, r.text
        return r.json()

    def webhook(self, event: dict, *, secret=None, timestamp=None, header="__sign__"):
        payload = json.dumps(event)
        headers = {"Content-Type": "application/json"}
        if header == "__sign__":
            headers["Stripe-Signature"] = sign_stripe_payload(payload, secret or self.webhook_secret, timestamp)
        elif header is not None:
            headers["Stripe-Signature"] = header
        return self.http.post("/webhooks/stripe", content=payload, headers=headers)

    def status(self, order_id: str) -> str:
        return self.store.get_order(order_id)["status"]


def event(kind: str, obj: dict, event_id: str | None = None) -> dict:
    return {"id": event_id or "evt_" + secrets.token_hex(10), "object": "event", "type": kind,
            "created": int(time.time()), "data": {"object": obj}}


# ---- configuration: test mode by default ---------------------------------------------------------

def test_a_live_key_is_refused_in_test_mode_a_test_key_in_live_mode_and_errors_never_echo_the_key():
    live = "sk_" + "live_" + secrets.token_hex(16)
    with pytest.raises(ConfigError) as err:
        load_stripe_config({"STRIPE_SECRET_KEY": live, "STRIPE_WEBHOOK_SECRET": new_webhook_secret()})
    assert "not a test-mode secret key" in str(err.value) and live not in str(err.value)
    test_key, _ = new_test_keys()
    with pytest.raises(ConfigError, match="not a live-mode secret key"):
        load_stripe_config({"STRIPE_MODE": "live", "STRIPE_SECRET_KEY": test_key, "STRIPE_WEBHOOK_SECRET": "x"})
    with pytest.raises(ConfigError, match="publishable"):
        load_stripe_config({"STRIPE_SECRET_KEY": test_key, "STRIPE_WEBHOOK_SECRET": "x",
                            "STRIPE_PUBLISHABLE_KEY": "pk_" + "live_" + secrets.token_hex(16)})
    with pytest.raises(ConfigError, match="STRIPE_WEBHOOK_SECRET"):
        load_stripe_config({"STRIPE_SECRET_KEY": test_key})
    cfg = load_stripe_config({"STRIPE_SECRET_KEY": test_key, "STRIPE_WEBHOOK_SECRET": "x"})
    assert cfg.mode == "test" and test_key not in repr(cfg)


def test_the_ui_config_route_says_test_mode_and_never_exposes_the_secret_key(fake):
    shop = Shop(fake)
    body = shop.http.get("/api/checkout/config").json()
    assert body["mode"] == "test" and body["publishable_key"].startswith("pk_test_")
    assert "sk_" not in json.dumps(body)


# ---- prices come from the server -----------------------------------------------------------------

def test_checkout_creates_a_checkout_session_priced_by_the_server(fake):
    shop = Shop(fake)
    out = shop.checkout([{"product_id": "starter-pack", "quantity": 2}])
    session = fake.sessions[out["session_id"]]
    assert session["line_items_sent"] == [{"quantity": 2, "unit_amount": 1500, "currency": "usd", "name": "Starter pack"}]
    assert session["amount_total"] == 3000
    assert session["metadata"]["order_id"] == out["order_id"]
    assert session["success_url"] == "http://shop.test/checkout/success?session_id={CHECKOUT_SESSION_ID}"
    assert out["checkout_url"] == session["url"]
    order = shop.http.get(f"/api/orders/{out['order_id']}").json()
    assert order["status"] == "pending" and order["amount_cents"] == 3000
    req = next(r for r in fake.requests if r["path"] == "/v1/checkout/sessions"
               and r["form"].get("metadata[order_id]") == out["order_id"])
    assert req["headers"]["idempotency-key"] == f"checkout-{out['order_id']}"


def test_a_tampered_client_price_amount_or_currency_is_ignored(fake):
    shop = Shop(fake)
    r = shop.http.post("/api/checkout", json={"amount": 1, "currency": "jpy", "items": [
        {"product_id": "pro-pack", "quantity": 1, "price": 1, "unit_amount": 1, "unit_amount_cents": 1,
         "amount": 1, "currency": "jpy", "name": "Free"}]})
    assert r.status_code == 201
    session = fake.sessions[r.json()["session_id"]]
    assert session["line_items_sent"] == [{"quantity": 1, "unit_amount": 4900, "currency": "usd", "name": "Pro pack"}]
    assert shop.store.get_order(r.json()["order_id"])["amount_cents"] == 4900


@pytest.mark.parametrize("body", [
    {"items": [{"product_id": "no-such-thing", "quantity": 1}]},
    {"items": []}, {}, [],
    {"items": [{"product_id": "starter-pack", "quantity": 0}]},
    {"items": [{"product_id": "starter-pack", "quantity": 1.5}]},
    {"items": [{"product_id": "starter-pack", "quantity": "2"}]},
    {"items": [{"product_id": "starter-pack", "quantity": True}]},
    {"items": [{"product_id": "starter-pack", "quantity": 100}]},
    {"items": [{"product_id": "starter-pack", "quantity": -1}]},
])
def test_unknown_products_empty_orders_and_bad_quantities_are_refused_before_stripe_is_called(fake, body):
    shop = Shop(fake)
    before = len(fake.sessions)
    assert shop.http.post("/api/checkout", json=body).status_code == 400
    assert len(fake.sessions) == before


def test_checkout_from_a_catalog_cart_cart_uses_server_prices_and_a_price_change_is_reflected(fake):
    prices = {"tee-1": 2500}

    class CartCatalog(StaticCatalog):
        def cart_line_items(self, cart_id, request=None):
            if cart_id != "cart-abc":
                return None
            return [{"product_id": "tee-1", "name": "T-shirt", "unit_amount_cents": prices["tee-1"],
                     "currency": "usd", "quantity": 3}]

    shop = Shop(fake, catalog=CartCatalog(DEFAULT_PRODUCTS))
    first = shop.http.post("/api/checkout", json={"cart_id": "cart-abc", "total": 1})
    assert first.status_code == 201
    assert fake.sessions[first.json()["session_id"]]["amount_total"] == 7500
    prices["tee-1"] = 2700
    second = shop.http.post("/api/checkout", json={"cart_id": "cart-abc"})
    assert fake.sessions[second.json()["session_id"]]["amount_total"] == 8100
    assert shop.http.post("/api/checkout", json={"cart_id": "someone-elses"}).status_code == 400


# ---- webhooks: signed, fresh, idempotent ---------------------------------------------------------

def test_a_webhook_with_a_bad_forged_or_missing_signature_is_refused_and_changes_nothing(fake):
    shop = Shop(fake)
    out = shop.checkout()
    session = fake.pay(out["session_id"])
    ev = event("checkout.session.completed", session)
    assert shop.webhook(ev, secret=new_webhook_secret()).status_code == 400  # forged: another secret
    assert shop.webhook(ev, header="t=1,v1=deadbeef").status_code == 400
    assert shop.webhook(ev, header=None).status_code == 400
    # A valid signature over a DIFFERENT body (the attacker edits the amount after signing).
    sig = sign_stripe_payload(json.dumps(ev), shop.webhook_secret)
    tampered = json.dumps({**ev, "data": {"object": {**session, "amount_total": 1}}})
    r = shop.http.post("/webhooks/stripe", content=tampered,
                       headers={"Content-Type": "application/json", "Stripe-Signature": sig})
    assert r.status_code == 400
    assert shop.status(out["order_id"]) == "pending" and shop.paid == []


def test_a_webhook_signed_more_than_five_minutes_ago_is_refused(fake):
    shop = Shop(fake)
    out = shop.checkout()
    ev = event("checkout.session.completed", fake.pay(out["session_id"]))
    assert shop.webhook(ev, timestamp=int(time.time()) - 600).status_code == 400
    assert shop.status(out["order_id"]) == "pending"


def test_a_signed_completed_event_fulfils_once_and_a_replayed_event_is_processed_once(fake):
    shop = Shop(fake)
    out = shop.checkout()
    ev = event("checkout.session.completed", fake.pay(out["session_id"]))
    first = shop.webhook(ev)
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert shop.status(out["order_id"]) == "paid"
    again = shop.webhook(ev)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    # Same object, a NEW event id: still fulfilled once.
    assert shop.webhook(event("checkout.session.completed", ev["data"]["object"])).status_code == 200
    assert shop.paid == [out["order_id"]]


def test_a_failing_handler_records_nothing_so_stripes_retry_is_processed(fake, monkeypatch):
    shop = Shop(fake)
    out = shop.checkout()
    ev = event("checkout.session.completed", fake.pay(out["session_id"]))
    calls = {"n": 0}
    original = shop.store.transition

    def flaky(oid, to):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return original(oid, to)

    monkeypatch.setattr(shop.store, "transition", flaky)
    assert shop.webhook(ev).status_code == 500
    assert shop.status(out["order_id"]) == "pending"
    retry = shop.webhook(ev)
    assert retry.status_code == 200 and retry.json()["duplicate"] is False
    assert shop.status(out["order_id"]) == "paid"


def test_the_success_page_fulfils_from_stripe_idempotent_with_the_webhook(fake):
    shop = Shop(fake)
    out = shop.checkout()
    pending = shop.http.get(f"/checkout/success?session_id={out['session_id']}")
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    session = fake.pay(out["session_id"])
    assert shop.http.get(f"/checkout/success?session_id={out['session_id']}").json()["status"] == "paid"
    assert shop.http.get(f"/checkout/success?session_id={out['session_id']}").json()["status"] == "paid"
    assert shop.webhook(event("checkout.session.completed", session)).status_code == 200
    assert shop.paid == [out["order_id"]]
    page = shop.http.get(f"/checkout/success?session_id={out['session_id']}", headers={"Accept": "text/html"})
    assert "Thank you" in page.text


def test_the_success_page_refuses_a_malformed_or_unknown_session_id(fake):
    shop = Shop(fake)
    assert shop.http.get("/checkout/success?session_id=../../v1/refunds").status_code == 400
    assert shop.http.get("/checkout/success").status_code == 400
    assert shop.http.get("/checkout/success?session_id=cs_test_nope").status_code == 404


def test_a_paid_session_for_a_different_amount_is_held_for_review(fake):
    shop = Shop(fake)
    out = shop.checkout()
    assert shop.webhook(event("checkout.session.completed", fake.pay(out["session_id"], amount_total=100))).status_code == 200
    assert shop.status(out["order_id"]) == "needs_review" and shop.paid == []


def test_an_event_naming_an_order_with_a_session_this_app_did_not_create_fulfils_nothing(fake):
    shop = Shop(fake)
    out = shop.checkout()
    other = {"id": "cs_test_" + secrets.token_hex(8), "object": "checkout.session", "payment_status": "paid",
             "amount_total": 3000, "currency": "usd", "metadata": {"order_id": out["order_id"]}}
    assert shop.webhook(event("checkout.session.completed", other)).status_code == 200
    assert shop.status(out["order_id"]) == "pending"


def test_order_state_machine(fake):
    shop = Shop(fake)
    out = shop.checkout()
    pi = {"id": fake.sessions[out["session_id"]]["payment_intent"], "object": "payment_intent",
          "metadata": {"order_id": out["order_id"]}}
    shop.webhook(event("payment_intent.payment_failed", pi))
    assert shop.status(out["order_id"]) == "failed"
    shop.webhook(event("checkout.session.completed", fake.pay(out["session_id"])))
    assert shop.status(out["order_id"]) == "paid"
    assert shop.webhook(event("payment_intent.payment_failed", pi)).status_code == 200
    assert shop.status(out["order_id"]) == "paid"

    second = shop.checkout()
    shop.webhook(event("checkout.session.expired", fake.sessions[second["session_id"]]))
    assert shop.status(second["order_id"]) == "cancelled"
    # Paid after it was cancelled: held for a person, never shipped silently.
    shop.webhook(event("checkout.session.completed", fake.pay(second["session_id"])))
    assert shop.status(second["order_id"]) == "needs_review"
    assert shop.paid == [out["order_id"]]


# ---- refunds (optional) --------------------------------------------------------------------------

def test_refunds_are_off_by_default_need_an_admin_and_refund_the_recorded_amount(fake):
    off = Shop(fake)
    assert off.http.post(f"/api/orders/{uuid.uuid4()}/refund", json={}).status_code == 404
    shop = Shop(fake, env={"PAYMENTS_REFUNDS_ENABLED": "true"},
                is_admin=lambda request: request.headers.get("x-test-admin") == "yes")
    out = shop.checkout()
    admin = {"x-test-admin": "yes"}
    assert shop.http.post(f"/api/orders/{out['order_id']}/refund", json={}, headers=admin).status_code == 409
    session = fake.pay(out["session_id"])
    shop.webhook(event("checkout.session.completed", session))
    assert shop.http.post(f"/api/orders/{out['order_id']}/refund", json={"amount": 999999}).status_code == 403
    r = shop.http.post(f"/api/orders/{out['order_id']}/refund", json={"amount": 999999}, headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    refund = fake.refunds[-1]
    assert refund["amount"] == 3000 and refund["payment_intent"] == session["payment_intent"]
    assert refund["idempotency_key"] == f"refund-{out['order_id']}"
    charge = {"id": "ch_1", "object": "charge", "refunded": True, "payment_intent": session["payment_intent"]}
    assert shop.webhook(event("charge.refunded", charge)).status_code == 200
    assert shop.status(out["order_id"]) == "refunded"


def test_when_stripe_is_unavailable_checkout_answers_502_and_the_order_is_cancelled(fake):
    shop = Shop(fake)
    fake.down = True
    try:
        r = shop.http.post("/api/checkout", json={"items": [{"product_id": "starter-pack", "quantity": 1}]})
    finally:
        fake.down = False
    assert r.status_code == 502
    orders = shop.store.all_orders()
    assert len(orders) == 1 and orders[0]["status"] == "cancelled"
