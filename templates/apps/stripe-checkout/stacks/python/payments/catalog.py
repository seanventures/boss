"""Where prices come from: ALWAYS the server. The client sends product ids and quantities (or a
cart id); any price, amount or currency it sends is ignored.

The checkout contract (shared with the `catalog-cart` template, spec §3.4):
  catalog.get_product(id)            -> {id, name, unit_amount_cents, currency, active} | None
  catalog.cart_line_items(cart_id, request)
                                     -> [{product_id, name, unit_amount_cents, currency, quantity}]
                                        | None when there is no such cart (for this session)
When `catalog-cart` is applied, back these with its `products` table and its cart's checkout
function (same shapes). Without it, the app sells DEFAULT_PRODUCTS with a single buy button.

A cart that cannot be sold now (a product gone, the cart empty, stock): catalog-cart's provider
raises its `CatalogError` (a 4xx status + a code). price_line_items turns that into a
CheckoutInputError (the checkout route's 400, before any order or payment-provider call), so a
shop combining both templates never answers a 500 for it and needs no glue for it. It is matched by
its class name and shape, not imported: this template works without catalog-cart.
"""
from __future__ import annotations

from typing import Any, Optional

DEFAULT_PRODUCTS = [
    {"id": "starter-pack", "name": "Starter pack", "unit_amount_cents": 1500, "currency": "usd", "active": True},
    {"id": "pro-pack", "name": "Pro pack", "unit_amount_cents": 4900, "currency": "usd", "active": True},
]
MAX_QUANTITY = 99
MAX_LINES = 50


class CheckoutInputError(ValueError):
    pass


def _is_catalog_refusal(exc: BaseException) -> bool:
    """catalog-cart's CatalogError (or a subclass), refusing the request with a 4xx and a code."""
    code, status = getattr(exc, "code", None), getattr(exc, "status", None)
    return (any(c.__name__ == "CatalogError" for c in type(exc).__mro__)
            and isinstance(code, str) and bool(code)
            and isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 499)


def _from_catalog(call):
    """Call the catalog; its refusal of the cart is the client's input error, anything else stays
    an error of the app (a 500 that must be fixed, not hidden)."""
    try:
        return call()
    except Exception as exc:
        if _is_catalog_refusal(exc):
            raise CheckoutInputError(exc.code) from exc
        raise


class StaticCatalog:
    def __init__(self, products: Optional[list[dict]] = None) -> None:
        self._by_id = {p["id"]: dict(p) for p in (products if products is not None else DEFAULT_PRODUCTS)}

    def get_product(self, product_id: Any) -> Optional[dict]:
        p = self._by_id.get(str(product_id))
        return dict(p) if p else None

    def cart_line_items(self, cart_id: str, request: Any = None) -> Optional[list[dict]]:
        return None  # no carts without catalog-cart


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def price_line_items(catalog: Any, body: Any, request: Any = None) -> dict:
    """Server-priced line items for the client's request: {lines, currency, amount_cents}."""
    body = body if isinstance(body, dict) else {}
    if body.get("cart_id") is not None:
        cart = _from_catalog(lambda: catalog.cart_line_items(str(body["cart_id"]), request))
        if cart is None:
            raise CheckoutInputError("no such cart")
        lines = [{"product_id": str(l["product_id"]), "name": str(l["name"]),
                  "unit_amount_cents": l["unit_amount_cents"], "currency": str(l["currency"]).lower(),
                  "quantity": l["quantity"]} for l in cart]
    else:
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise CheckoutInputError("send cart_id or items")
        if len(items) > MAX_LINES:
            raise CheckoutInputError("too many items")
        lines = []
        for item in items:
            item = item if isinstance(item, dict) else {}
            product = _from_catalog(lambda: catalog.get_product(item.get("product_id")))
            if not product or not product.get("active"):
                raise CheckoutInputError("unknown product")
            # Only the id and quantity are read from the client; a price or amount is ignored.
            lines.append({"product_id": product["id"], "name": product["name"],
                          "unit_amount_cents": product["unit_amount_cents"],
                          "currency": str(product["currency"]).lower(), "quantity": item.get("quantity")})
    if not lines:
        raise CheckoutInputError("the cart is empty")
    for l in lines:
        if not _is_int(l["quantity"]) or not 1 <= l["quantity"] <= MAX_QUANTITY:
            raise CheckoutInputError(f"quantity must be a whole number from 1 to {MAX_QUANTITY}")
        if not _is_int(l["unit_amount_cents"]) or l["unit_amount_cents"] < 1:
            raise CheckoutInputError("a product has no valid price")
    currencies = {l["currency"] for l in lines}
    if len(currencies) != 1:
        raise CheckoutInputError("one currency per checkout")
    return {"lines": lines, "currency": currencies.pop(),
            "amount_cents": sum(l["unit_amount_cents"] * l["quantity"] for l in lines)}
