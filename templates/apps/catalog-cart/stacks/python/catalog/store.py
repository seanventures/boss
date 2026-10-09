"""Products, carts and the checkout contract (schema.sql has the tables). This in-memory store is
the reference; back the same methods with the app's database when adapting the template.

Money is integer cents everywhere. A cart line holds no price: totals and checkout always use the
product's price NOW, so a client can never choose what it pays."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

MAX_QUANTITY = 99


class CatalogError(Exception):
    def __init__(self, status: int, code: str, **extra) -> None:
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


def load_seed(path: Optional[Path] = None) -> list[dict]:
    path = path or Path(__file__).resolve().parents[1] / "seed" / "products.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _whole(value, low: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= MAX_QUANTITY


class CatalogStore:
    def __init__(self, products: Optional[list[dict]] = None, currency: str = "usd") -> None:
        self.currency = currency
        self.products = {p["id"]: dict(p) for p in (products if products is not None else load_seed())}
        self.carts: dict[str, dict] = {}       # cart id -> {id, user_id, items: {product_id: qty}}
        self.user_carts: dict[str, str] = {}   # user id -> cart id
        self.lock = threading.RLock()

    # ---- products ---------------------------------------------------------------------------
    def active_product(self, product_id: str) -> Optional[dict]:
        p = self.products.get(product_id)
        return p if p and p["active"] else None

    def search(self, q: str = "", page: int = 1, page_size: int = 12) -> dict:
        needle = q.strip().lower()
        hits = sorted((p for p in self.products.values() if p["active"] and (
            not needle or any(needle in p[f].lower() for f in ("name", "description", "sku")))),
            key=lambda p: (p["name"], p["id"]))
        start = (page - 1) * page_size
        return {"items": hits[start:start + page_size], "page": page, "page_size": page_size,
                "total": len(hits), "pages": max(1, -(-len(hits) // page_size))}

    # An operator's change (the app's admin screens call these).
    def set_price(self, product_id: str, price_cents: int) -> None:
        if not isinstance(price_cents, int) or price_cents < 0:
            raise CatalogError(400, "invalid_price")
        self.products[product_id]["price_cents"] = price_cents

    def set_active(self, product_id: str, active: bool) -> None:
        self.products[product_id]["active"] = bool(active)

    # ---- carts --------------------------------------------------------------------------------
    def new_cart(self, user_id: Optional[str] = None) -> dict:
        with self.lock:
            cart = {"id": str(uuid.uuid4()), "user_id": user_id, "items": {}}
            self.carts[cart["id"]] = cart
            if user_id:
                self.user_carts[user_id] = cart["id"]
            return cart

    def cart(self, cart_id: Optional[str]) -> Optional[dict]:
        return self.carts.get(cart_id) if cart_id else None

    def cart_for_user(self, user_id: str, create: bool = False) -> Optional[dict]:
        cart = self.cart(self.user_carts.get(user_id))
        return cart or (self.new_cart(user_id) if create else None)

    def _check_stock(self, product: dict, quantity: int) -> None:
        if quantity > product["stock"]:
            raise CatalogError(409, "insufficient_stock", available=product["stock"])

    def add_item(self, cart: dict, product_id: str, quantity) -> None:
        if not _whole(quantity, 1):
            raise CatalogError(400, "invalid_quantity")
        with self.lock:
            product = self.active_product(product_id)
            if product is None:
                raise CatalogError(404, "product_not_found")
            total = cart["items"].get(product_id, 0) + quantity
            if total > MAX_QUANTITY:
                raise CatalogError(400, "invalid_quantity")
            self._check_stock(product, total)
            cart["items"][product_id] = total

    def set_quantity(self, cart: dict, product_id: str, quantity) -> None:
        if not _whole(quantity, 0):
            raise CatalogError(400, "invalid_quantity")
        with self.lock:
            if product_id not in cart["items"]:
                raise CatalogError(404, "not_in_cart")
            if quantity == 0:
                del cart["items"][product_id]
                return
            product = self.active_product(product_id)
            if product is None:
                raise CatalogError(409, "product_unavailable", product_id=product_id)
            self._check_stock(product, quantity)
            cart["items"][product_id] = quantity

    def remove_item(self, cart: dict, product_id: str) -> None:
        with self.lock:
            if cart["items"].pop(product_id, None) is None:
                raise CatalogError(404, "not_in_cart")

    def merge_into(self, user_id: str, anon_cart_id: str) -> Optional[dict]:
        """Signing in: the anonymous cart's lines join the user's cart (quantities added, capped at
        stock and MAX_QUANTITY) and the anonymous cart is deleted."""
        with self.lock:
            anon = self.cart(anon_cart_id)
            if anon is None or anon["user_id"]:
                return self.cart_for_user(user_id)
            target = self.cart_for_user(user_id, create=True)
            for product_id, qty in anon["items"].items():
                product = self.active_product(product_id)
                if product is None:
                    continue
                merged = min(target["items"].get(product_id, 0) + qty, product["stock"], MAX_QUANTITY)
                if merged > 0:
                    target["items"][product_id] = merged
            del self.carts[anon["id"]]
            return target

    def view(self, cart: Optional[dict]) -> dict:
        items, subtotal, count = [], 0, 0
        for product_id, quantity in (cart["items"].items() if cart else []):
            p = self.products[product_id]
            line = p["price_cents"] * quantity
            items.append({"product_id": product_id, "sku": p["sku"], "name": p["name"],
                          "unit_price_cents": p["price_cents"], "quantity": quantity,
                          "line_total_cents": line,
                          "available": bool(p["active"] and p["stock"] >= quantity)})
            subtotal += line
            count += quantity
        return {"id": cart["id"] if cart else None, "currency": self.currency, "items": items,
                "item_count": count, "subtotal_cents": subtotal}

    def checkout_lines(self, cart_id: Optional[str]) -> dict:
        """THE CHECKOUT CONTRACT (payments-stripe, payments-paypal): a cart id in, the lines to
        charge out, every price read from the product now. Refuses an empty cart, a product no
        longer on sale and a quantity above stock, so a payment template never charges for what
        cannot ship."""
        with self.lock:
            cart = self.cart(cart_id)
            if not cart or not cart["items"]:
                raise CatalogError(409, "cart_empty")
            lines, total = [], 0
            for product_id, quantity in cart["items"].items():
                p = self.active_product(product_id)
                if p is None:
                    raise CatalogError(409, "product_unavailable", product_id=product_id)
                self._check_stock(p, quantity)
                line = p["price_cents"] * quantity
                lines.append({"product_id": p["id"], "sku": p["sku"], "name": p["name"],
                              "unit_amount_cents": p["price_cents"], "quantity": quantity,
                              "line_total_cents": line})
                total += line
            return {"cart_id": cart["id"], "currency": self.currency, "lines": lines, "total_cents": total}
