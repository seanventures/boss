"""The catalog-cart template's own tests, against its HTTP routes (not internals). The signed-in
user comes from the app's login (login-oauth's session when applied); here a test-only
get_user_id reads a header, so merge-on-login is tested without a provider.
Run: python3 -m pytest -q tests/template_catalog_cart"""
from __future__ import annotations

import json
from typing import Optional

from fastapi.testclient import TestClient

BASE_URL = "https://app.example.test"  # as in conftest.py: https, so cookies must be Secure


class Browser:
    def __init__(self, app, user: Optional[str] = None):
        self.client = TestClient(app, base_url=BASE_URL)
        self.user = user
        self.set_cookies: list[str] = []

    def request(self, method: str, path: str, body=None, headers=None):
        h = dict(headers or {})
        if self.user:
            h["X-Test-User"] = self.user
        kw = {}
        if body is not None:
            if isinstance(body, str):
                kw["content"] = body
            else:
                kw["content"] = json.dumps(body)
                h.setdefault("Content-Type", "application/json")
        res = self.client.request(method, path, headers=h, **kw)
        self.set_cookies += res.headers.get_list("set-cookie")
        return res

    def get(self, path: str):
        return self.request("GET", path)

    def add(self, product_id: str, quantity=None):
        body = {"product_id": product_id} if quantity is None else {"product_id": product_id, "quantity": quantity}
        return self.request("POST", "/api/cart/items", body)


def test_the_list_shows_active_products_only_12_to_a_page(app):
    res = Browser(app).get("/api/products")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 18 and len(body["items"]) == 12 and body["pages"] == 2
    assert all("stock" not in p and isinstance(p["in_stock"], bool) for p in body["items"])
    ids = [p["id"] for p in Browser(app).get("/api/products?page_size=50").json()["items"]]
    assert "p19" not in ids and "p20" not in ids


def test_search_matches_name_description_and_sku(app):
    b = Browser(app)
    assert [p["id"] for p in b.get("/api/products?q=MUG").json()["items"]] == ["p02"]
    assert [p["id"] for p in b.get("/api/products?q=sku-004").json()["items"]] == ["p04"]
    assert [p["id"] for p in b.get("/api/products?q=dishwasher").json()["items"]] == ["p02"]
    assert b.get("/api/products?q=withdrawn").json()["total"] == 0


def test_pagination_bounds_are_enforced(app):
    b = Browser(app)
    for q in ("page=0", "page=-1", "page=abc", "page=1.5", "page_size=0", "page_size=51"):
        assert b.get(f"/api/products?{q}").status_code == 400, q
    assert b.get("/api/products?q=" + "x" * 101).status_code == 400
    p1 = [p["id"] for p in b.get("/api/products?page=1&page_size=10").json()["items"]]
    p2 = [p["id"] for p in b.get("/api/products?page=2&page_size=10").json()["items"]]
    assert len(p1) == 10 and len(p2) == 8 and len(set(p1 + p2)) == 18
    assert b.get("/api/products?page=9").json()["items"] == []


def test_detail_answers_active_and_404_otherwise(app):
    b = Browser(app)
    one = b.get("/api/products/p05")
    assert one.status_code == 200 and one.json()["price_cents"] == 3850
    assert b.get("/api/products/p19").status_code == 404
    assert b.get("/api/products/nope").status_code == 404


def test_cart_totals_are_server_computed_in_integer_cents(app):
    b = Browser(app)
    assert b.add("p01", 3).status_code == 201
    body = b.add("p09", 2).json()
    assert body["subtotal_cents"] == 3 * 1999 + 2 * 999 and isinstance(body["subtotal_cents"], int)
    assert body["item_count"] == 5
    assert [(i["product_id"], i["line_total_cents"]) for i in body["items"]] == [("p01", 5997), ("p09", 1998)]
    cheat = b.request("POST", "/api/cart/items", {"product_id": "p02", "quantity": 1, "price_cents": 1}).json()
    assert next(i for i in cheat["items"] if i["product_id"] == "p02")["unit_price_cents"] == 1450


def test_quantity_bounds_are_enforced(app):
    b = Browser(app)
    for q in (0, -1, 100, 1.5, "2", None, True):
        res = b.request("POST", "/api/cart/items", {"product_id": "p01", "quantity": q})
        assert res.status_code == 400, q
    b.add("p12", 90)
    assert b.add("p12", 10).status_code == 400


def test_the_stock_limit_is_enforced(app):
    b = Browser(app)
    assert b.add("p03", 4).status_code == 409
    assert b.add("p03", 3).status_code == 201
    more = b.add("p03", 1)
    assert more.status_code == 409 and more.json()["error"] == "insufficient_stock"
    assert b.add("p17", 1).status_code == 409
    assert b.add("p19", 1).status_code == 404


def test_changing_and_removing_lines(app):
    b = Browser(app)
    b.add("p01", 1)
    b.add("p02", 1)
    assert b.request("PATCH", "/api/cart/items/p01", {"quantity": 4}).json()["subtotal_cents"] == 4 * 1999 + 1450
    assert b.request("PATCH", "/api/cart/items/p03", {"quantity": 4}).status_code == 404
    assert len(b.request("PATCH", "/api/cart/items/p01", {"quantity": 0}).json()["items"]) == 1
    assert b.request("DELETE", "/api/cart/items/p02").json()["items"] == []
    assert b.request("DELETE", "/api/cart/items/p02").status_code == 404


def test_carts_are_isolated_and_a_forged_cookie_opens_nothing(app):
    a = Browser(app)
    a.add("p01", 2)
    assert Browser(app).get("/api/cart").json()["items"] == []
    cart_id = a.client.cookies.get("cart").split(".")[0]
    forger = Browser(app)
    forger.client.cookies.set("cart", f"{cart_id}.forged-signature", domain="app.example.test")
    assert forger.get("/api/cart").json()["items"] == []
    forger.client.cookies.clear()
    forger.client.cookies.set("cart", cart_id, domain="app.example.test")
    assert forger.get("/api/cart").json()["items"] == []
    assert a.get("/api/cart").json()["item_count"] == 2


def test_the_anonymous_cart_merges_into_the_users_cart_on_sign_in(app):
    before = Browser(app, user="user-1")
    before.add("p01", 1)
    before.add("p02", 1)
    b = Browser(app)
    b.add("p01", 2)
    b.add("p03", 3)
    b.user = "user-1"  # signs in
    merged = b.get("/api/cart").json()
    assert sorted((i["product_id"], i["quantity"]) for i in merged["items"]) == [("p01", 3), ("p02", 1), ("p03", 3)]
    assert b.client.cookies.get("cart") is None, "the anonymous cart cookie is cleared"
    assert Browser(app, user="user-1").get("/api/cart").json()["item_count"] == 7
    b.user = None
    assert b.get("/api/cart").json()["items"] == []


def test_checkout_uses_the_price_now_and_refuses_what_cannot_be_sold(app, store):
    b = Browser(app)
    assert b.get("/api/cart/checkout").json()["error"] == "cart_empty"
    b.add("p01", 2)
    b.add("p02", 1)
    store.set_price("p01", 2499)
    res = b.get("/api/cart/checkout")
    assert res.status_code == 200
    body = res.json()
    assert body["cart_id"] == b.client.cookies.get("cart").split(".")[0]
    assert body["currency"] == "usd"
    assert [(l["product_id"], l["unit_amount_cents"], l["quantity"], l["line_total_cents"]) for l in body["lines"]] \
        == [("p01", 2499, 2, 4998), ("p02", 1450, 1, 1450)]
    assert body["total_cents"] == 6448
    assert b.get("/api/cart").json()["subtotal_cents"] == 6448
    store.set_active("p02", False)
    refused = b.get("/api/cart/checkout")
    assert refused.status_code == 409
    assert refused.json() == {"error": "product_unavailable", "product_id": "p02"}


def test_cart_changes_need_json_and_the_cookie_flags(app):
    b = Browser(app)
    form = b.request("POST", "/api/cart/items", "product_id=p01&quantity=1",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code == 415
    assert b.request("POST", "/api/cart/items", "{not json", headers={"Content-Type": "application/json"}).status_code == 400
    b.add("p01", 1)
    cookie = next(c for c in b.set_cookies if c.startswith("cart="))
    for flag in ("HttpOnly", "Secure", "SameSite=lax"):
        assert flag.lower() in cookie.lower(), cookie
