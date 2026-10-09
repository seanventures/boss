"""The `paypal-checkout` template's own tests, against its HTTP routes (not internals), with PayPal
replaced by an in-process fake. No test reaches the network or uses a real credential."""
from __future__ import annotations

import json
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient

from fake_paypal import FakePayPal, new_credentials, sign_paypal_event
from main import create_app
from payments.catalog import DEFAULT_PRODUCTS, StaticCatalog
from payments.config import HOSTS, ConfigError, load_paypal_config
from payments.paypal_checkout import from_value, to_value
from payments.paypal_client import PayPalClient


class Shop:
    def __init__(self, *, env=None, catalog=None, is_admin=None, clock=None):
        self.creds = new_credentials()
        self.fake = FakePayPal(**self.creds)
        full_env = {"PAYPAL_CLIENT_ID": self.creds["client_id"], "PAYPAL_CLIENT_SECRET": self.creds["client_secret"],
                    "PAYPAL_WEBHOOK_ID": self.creds["webhook_id"], "APP_BASE_URL": "http://shop.test", **(env or {})}
        kwargs = {"now": clock} if clock else {}
        self.paypal = PayPalClient(api_base=self.fake.url, client_id=self.creds["client_id"],
                                   client_secret=self.creds["client_secret"], **kwargs)
        self.paid: list[str] = []
        self.app = create_app(env=full_env, client=self.paypal, catalog=catalog, is_admin=is_admin,
                              on_order_paid=lambda o: self.paid.append(o["id"]))
        self.store = self.app.state.store
        self.http = TestClient(self.app, headers={"Accept": "application/json"})

    def create(self, items=None) -> dict:
        r = self.http.post("/api/paypal/orders", json={"items": items or [{"product_id": "starter-pack", "quantity": 2}]})
        assert r.status_code == 201, r.text
        return r.json()

    def webhook(self, event, *, headers=None, secret=None, webhook_id=None, body=None):
        h = headers if headers is not None else sign_paypal_event(
            event, secret=secret or self.fake.signing_secret, webhook_id=webhook_id or self.creds["webhook_id"])
        return self.http.post("/webhooks/paypal", content=body or json.dumps(event),
                              headers={"Content-Type": "application/json", **h})

    def status(self, order_id) -> str:
        return self.store.get_order(order_id)["status"]

    def close(self):
        self.fake.close()


@pytest.fixture
def make():
    shops = []

    def _make(**kw):
        shop = Shop(**kw)
        shops.append(shop)
        return shop

    yield _make
    for s in shops:
        s.close()


def capture_event(kind, paypal_order_id, order_id, amount):
    return {"id": "WH-" + str(uuid.uuid4()), "event_type": kind, "resource_type": "capture",
            "resource": {"id": "CAP" + secrets.token_hex(6).upper(),
                         "status": "COMPLETED" if kind == "PAYMENT.CAPTURE.COMPLETED" else "DECLINED",
                         "amount": amount, "custom_id": order_id,
                         "supplementary_data": {"related_ids": {"order_id": paypal_order_id}}}}


# ---- configuration: sandbox by default -----------------------------------------------------------

def test_the_sandbox_is_the_default_and_live_needs_paypal_env_live_and_app_env_production():
    creds = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "x" * 8}
    cfg = load_paypal_config(creds)
    assert cfg.env == "sandbox" and cfg.api_base == HOSTS["sandbox"] == "https://api-m.sandbox.paypal.com"
    with pytest.raises(ConfigError, match="only allowed when APP_ENV=production"):
        load_paypal_config({**creds, "PAYPAL_ENV": "live"})
    with pytest.raises(ConfigError, match="APP_ENV=production"):
        load_paypal_config({**creds, "PAYPAL_ENV": "live", "APP_ENV": "preview"})
    assert load_paypal_config({**creds, "PAYPAL_ENV": "live", "APP_ENV": "production"}).api_base == "https://api-m.paypal.com"
    with pytest.raises(ConfigError, match="PAYPAL_ENV must be"):
        load_paypal_config({**creds, "PAYPAL_ENV": "https://evil.example"})
    with pytest.raises(ConfigError, match="PAYPAL_CLIENT_SECRET is not set"):
        load_paypal_config({"PAYPAL_CLIENT_ID": "id"})
    assert "x" * 8 not in repr(cfg)


def test_money_is_converted_in_integer_minor_units_never_through_floats():
    assert to_value(3000, "usd") == "30.00" and to_value(1, "usd") == "0.01" and to_value(1999, "eur") == "19.99"
    assert to_value(500, "jpy") == "500"
    assert from_value("30.00", "usd") == 3000 and from_value("30.1", "usd") == 3010 and from_value("0.29", "usd") == 29
    assert from_value("1e3", "usd") is None and from_value("-1.00", "usd") is None


def test_the_config_route_gives_the_js_sdk_the_client_id_and_env_never_the_secret(make):
    shop = make()
    body = shop.http.get("/api/paypal/config").json()
    assert body == {"env": "sandbox", "client_id": shop.creds["client_id"], "currency": "USD"}
    assert shop.creds["client_secret"] not in json.dumps(body)


# ---- prices come from the server -----------------------------------------------------------------

def test_creating_an_order_prices_it_on_the_server_with_an_idempotency_key(make):
    shop = make()
    out = shop.create([{"product_id": "starter-pack", "quantity": 2}])
    pp = shop.fake.orders[out["paypal_order_id"]]
    unit = pp["purchase_units"][0]
    assert pp["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "30.00",
                              "breakdown": {"item_total": {"currency_code": "USD", "value": "30.00"}}}
    assert unit["items"] == [{"name": "Starter pack", "sku": "starter-pack", "quantity": "2",
                              "unit_amount": {"currency_code": "USD", "value": "15.00"}}]
    assert unit["custom_id"] == out["order_id"]
    assert pp["request_id"] == f"create-{out['order_id']}"
    assert pp["payment_source"]["paypal"]["experience_context"]["return_url"] == "http://shop.test/paypal/return"
    assert out["approve_url"] == f"https://www.sandbox.paypal.com/checkoutnow?token={out['paypal_order_id']}"
    assert shop.http.get(f"/api/orders/{out['order_id']}").json()["status"] == "pending"


def test_a_tampered_client_price_amount_or_currency_is_ignored(make):
    shop = make()
    r = shop.http.post("/api/paypal/orders", json={"amount": "0.01", "currency_code": "JPY", "items": [
        {"product_id": "pro-pack", "quantity": 1, "price": 1, "unit_amount": {"value": "0.01"}, "currency": "jpy"}]})
    assert r.status_code == 201
    unit = shop.fake.orders[r.json()["paypal_order_id"]]["purchase_units"][0]
    assert unit["amount"]["value"] == "49.00" and unit["amount"]["currency_code"] == "USD"


@pytest.mark.parametrize("body", [
    {"items": [{"product_id": "nope", "quantity": 1}]}, {"items": []}, {}, [],
    {"items": [{"product_id": "starter-pack", "quantity": 0}]},
    {"items": [{"product_id": "starter-pack", "quantity": 2.5}]},
    {"items": [{"product_id": "starter-pack", "quantity": "1"}]},
    {"items": [{"product_id": "starter-pack", "quantity": 100}]},
])
def test_unknown_products_empty_orders_and_bad_quantities_are_refused_before_paypal_is_called(make, body):
    shop = make()
    assert shop.http.post("/api/paypal/orders", json=body).status_code == 400
    assert shop.fake.orders == {}


def test_an_order_from_a_catalog_cart_cart_uses_the_carts_server_prices(make):
    class CartCatalog(StaticCatalog):
        def cart_line_items(self, cart_id, request=None):
            if cart_id != "cart-abc":
                return None
            return [{"product_id": "tee-1", "name": "T-shirt", "unit_amount_cents": 2599, "currency": "usd", "quantity": 3}]

    shop = make(catalog=CartCatalog(DEFAULT_PRODUCTS))
    r = shop.http.post("/api/paypal/orders", json={"cart_id": "cart-abc", "total": "0.01"})
    assert r.status_code == 201
    assert shop.fake.orders[r.json()["paypal_order_id"]]["purchase_units"][0]["amount"]["value"] == "77.97"
    assert shop.http.post("/api/paypal/orders", json={"cart_id": "someone-elses"}).status_code == 400


class CatalogError(Exception):
    """Declared here exactly as catalog-cart declares it (the payment template never imports it)."""

    def __init__(self, status: int, code: str, **extra) -> None:
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


class RefusingCatalog(StaticCatalog):
    """catalog-cart's provider refusing a cart it cannot sell now."""

    def __init__(self) -> None:
        super().__init__(DEFAULT_PRODUCTS)
        self.failure: Exception = CatalogError(409, "product_unavailable", product_id="tee-1")

    def cart_line_items(self, cart_id, request=None):
        raise self.failure

    def get_product(self, product_id):
        if product_id == "gone":
            raise CatalogError(404, "product_not_found")
        return super().get_product(product_id)


def test_a_cart_catalog_cart_cannot_sell_is_a_400_with_its_code_before_paypal_is_called(make):
    """The payment template maps catalog-cart's CatalogError itself (by name and shape): a 400 with
    its code, never a 500, and no order or PayPal call. Anything else stays the app's own error."""
    catalog = RefusingCatalog()
    shop = make(catalog=catalog)
    before = len(shop.fake.requests)
    for payload, code in (({"cart_id": "cart-abc"}, "product_unavailable"),
                          ({"items": [{"product_id": "gone", "quantity": 1}]}, "product_not_found")):
        r = shop.http.post("/api/paypal/orders", json=payload)
        assert r.status_code == 400 and r.json() == {"error": code}, r.text
    catalog.failure = CatalogError(409, "cart_empty")
    assert shop.http.post("/api/paypal/orders", json={"cart_id": "cart-abc"}).json() == {"error": "cart_empty"}
    loose = TestClient(shop.app, raise_server_exceptions=False)
    for failure in (RuntimeError("database is down"), CatalogError(503, "catalog_down"),
                    type("Lookalike", (Exception,), {"status": 409, "code": "product_unavailable"})()):
        catalog.failure = failure
        assert loose.post("/api/paypal/orders", json={"cart_id": "cart-abc"}).status_code == 500, failure
    assert len(shop.fake.requests) == before, "PayPal must not be called"


# ---- capture: only ours, only once ---------------------------------------------------------------

def test_capture_is_refused_for_a_paypal_order_this_app_did_not_create_without_calling_paypal(make):
    shop = make()
    before = len(shop.fake.requests)
    assert shop.http.post("/api/paypal/orders/5O190127TN364715T/capture").status_code == 404
    assert shop.http.post("/api/paypal/orders/not-an-id/capture").status_code == 400
    assert len(shop.fake.requests) == before


def test_capture_pays_the_order_once_and_a_double_capture_ships_once(make):
    shop = make()
    out = shop.create()
    shop.fake.approve(out["paypal_order_id"])
    first = shop.http.post(f"/api/paypal/orders/{out['paypal_order_id']}/capture")
    assert first.json() == {"order_id": out["order_id"], "status": "paid"}
    calls = lambda: sum(1 for r in shop.fake.requests if r["path"].endswith("/capture"))  # noqa: E731
    n = calls()
    second = shop.http.post(f"/api/paypal/orders/{out['paypal_order_id']}/capture")
    assert second.json() == {"order_id": out["order_id"], "status": "paid"}
    assert calls() == n, "an already-paid order is not captured again"
    assert shop.paid == [out["order_id"]]
    req = next(r for r in shop.fake.requests if r["path"].endswith("/capture"))
    assert req["headers"]["paypal-request-id"] == f"capture-{out['order_id']}"


def test_a_capture_for_a_different_amount_is_held_for_review(make):
    shop = make()
    out = shop.create()
    shop.fake.approve(out["paypal_order_id"])
    shop.fake.capture_amount_override = {"currency_code": "USD", "value": "0.01"}
    assert shop.http.post(f"/api/paypal/orders/{out['paypal_order_id']}/capture").json()["status"] == "needs_review"
    assert shop.paid == []


def test_a_declined_instrument_answers_402_restart_and_leaves_the_order_open(make):
    shop = make()
    out = shop.create()
    shop.fake.approve(out["paypal_order_id"])
    shop.fake.decline_next = True
    r = shop.http.post(f"/api/paypal/orders/{out['paypal_order_id']}/capture")
    assert r.status_code == 402 and r.json()["restart"] is True
    assert shop.status(out["order_id"]) == "pending"


def test_the_return_page_captures_server_side_and_reconciles_an_order_captured_elsewhere(make):
    shop = make()
    out = shop.create()
    shop.fake.approve(out["paypal_order_id"])
    shop.paypal.capture_order(out["paypal_order_id"], "elsewhere")  # another tab, another request id
    r = shop.http.get(f"/paypal/return?token={out['paypal_order_id']}&PayerID=PAYER1")
    assert r.status_code == 200 and r.json()["status"] == "paid"
    page = shop.http.get(f"/paypal/return?token={out['paypal_order_id']}", headers={"Accept": "text/html"})
    assert "Thank you" in page.text
    assert shop.paid == [out["order_id"]]


# ---- webhooks: verified by PayPal, idempotent ----------------------------------------------------

def test_a_webhook_that_fails_paypals_verification_is_refused_and_changes_nothing(make):
    shop = make()
    out = shop.create()
    ev = capture_event("PAYMENT.CAPTURE.COMPLETED", out["paypal_order_id"], out["order_id"],
                       {"currency_code": "USD", "value": "30.00"})
    assert shop.webhook(ev, secret=secrets.token_hex(32)).status_code == 400  # forged
    assert shop.webhook(ev, webhook_id="OTHERWEBHOOK").status_code == 400  # another endpoint's event
    headers = sign_paypal_event(ev, secret=shop.fake.signing_secret, webhook_id=shop.creds["webhook_id"])
    edited = json.dumps({**ev, "resource": {**ev["resource"], "amount": {"currency_code": "USD", "value": "0.01"}}})
    assert shop.webhook(ev, headers=headers, body=edited).status_code == 400  # signed, then edited
    asked = len(shop.fake.verifications)
    assert shop.webhook(ev, headers={}).status_code == 400  # no PayPal headers: PayPal is not even asked
    assert len(shop.fake.verifications) == asked
    assert shop.status(out["order_id"]) == "pending"
    v = shop.fake.verifications[0]
    assert v["webhook_id"] == shop.creds["webhook_id"] and v["auth_algo"] == "SHA256withRSA"


def test_a_verified_capture_completed_completes_a_pending_capture_once_and_a_replay_is_ignored(make):
    shop = make()
    out = shop.create()
    shop.fake.approve(out["paypal_order_id"])
    shop.fake.pending_next = True
    assert shop.http.post(f"/api/paypal/orders/{out['paypal_order_id']}/capture").json()["status"] == "pending"
    ev = capture_event("PAYMENT.CAPTURE.COMPLETED", out["paypal_order_id"], out["order_id"],
                       {"currency_code": "USD", "value": "30.00"})
    first = shop.webhook(ev)
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert shop.status(out["order_id"]) == "paid"
    assert shop.webhook(ev).json()["duplicate"] is True
    assert shop.paid == [out["order_id"]]


def test_a_verified_event_for_an_order_this_app_did_not_create_fulfils_nothing(make):
    shop = make()
    out = shop.create()
    amount = {"currency_code": "USD", "value": "30.00"}
    assert shop.webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", "9XY12345AB6789012", out["order_id"], amount)).status_code == 200
    assert shop.webhook(capture_event("PAYMENT.CAPTURE.COMPLETED", out["paypal_order_id"], str(uuid.uuid4()), amount)).status_code == 200
    assert shop.status(out["order_id"]) == "pending"


# ---- the access token ----------------------------------------------------------------------------

def test_the_access_token_is_cached_refreshed_before_it_expires_and_once_on_a_401(make):
    now = [1000.0]
    shop = make(clock=lambda: now[0])
    shop.create()
    shop.create()
    assert shop.fake.token_requests == 1
    now[0] += 32400 - 30  # inside the last minute of the token's life
    shop.create()
    assert shop.fake.token_requests == 2
    shop.fake.revoke_tokens()
    shop.create()
    assert shop.fake.token_requests == 3


# ---- refunds (optional) and outages --------------------------------------------------------------

def test_refunds_are_off_by_default_need_an_admin_and_refund_the_recorded_capture_amount(make):
    off = make()
    assert off.http.post(f"/api/orders/{uuid.uuid4()}/refund").status_code == 404
    shop = make(env={"PAYMENTS_REFUNDS_ENABLED": "true"},
                is_admin=lambda request: request.headers.get("x-test-admin") == "yes")
    out = shop.create()
    admin = {"x-test-admin": "yes"}
    assert shop.http.post(f"/api/orders/{out['order_id']}/refund", headers=admin).status_code == 409
    shop.fake.approve(out["paypal_order_id"])
    shop.http.post(f"/api/paypal/orders/{out['paypal_order_id']}/capture")
    assert shop.http.post(f"/api/orders/{out['order_id']}/refund", json={"amount": "9999.00"}).status_code == 403
    r = shop.http.post(f"/api/orders/{out['order_id']}/refund", json={"amount": "9999.00"}, headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    refund = shop.fake.refunds[-1]
    assert refund["amount"] == {"currency_code": "USD", "value": "30.00"}
    assert refund["capture_id"] == shop.store.get_order(out["order_id"])["payment_ref"]
    assert refund["request_id"] == f"refund-{out['order_id']}"


def test_when_paypal_is_unavailable_creating_an_order_answers_502_and_the_order_is_cancelled(make):
    shop = make()
    shop.fake.down = True
    r = shop.http.post("/api/paypal/orders", json={"items": [{"product_id": "starter-pack", "quantity": 1}]})
    shop.fake.down = False
    assert r.status_code == 502
    orders = shop.store.all_orders()
    assert len(orders) == 1 and orders[0]["status"] == "cancelled"
