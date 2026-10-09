"""The catalog-cart routes for a FastAPI app:

    GET    /api/products?q=&page=&page_size=  search the active products (page_size <= 50)
    GET    /api/products/{id}                 one active product (404 otherwise)
    GET    /api/cart                          the caller's cart with server-computed totals
    POST   /api/cart/items                    {product_id, quantity} adds to the cart
    PATCH  /api/cart/items/{product_id}       {quantity} sets it (0 removes)
    DELETE /api/cart/items/{product_id}       removes the line
    GET    /api/cart/checkout                 the checkout contract for the caller's cart

An anonymous cart is found by a signed `cart` cookie; a signed-in user's (get_user_id) by user,
and the anonymous cart merges into it on the first request after sign-in.

    catalog = create_catalog(load_catalog_config())
    app.include_router(catalog.router)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .store import CatalogError, CatalogStore

COOKIE = "cart"
MAX_PAGE_SIZE = 50
MAX_QUERY = 100
MAX_BODY = 10 * 1024


@dataclass(frozen=True)
class CatalogConfig:
    cart_secret: str
    secure_cookies: bool
    currency: str = "usd"


def load_catalog_config(env: Optional[Mapping[str, str]] = None) -> CatalogConfig:
    env = os.environ if env is None else env
    base = str(env.get("APP_BASE_URL") or "http://localhost:3000").rstrip("/")
    secret = str(env.get("CART_SECRET") or "").strip()
    if len(secret) < 32:
        raise RuntimeError("CART_SECRET must be set to at least 32 characters (BOSS generates one)")
    return CatalogConfig(cart_secret=secret, secure_cookies=base.startswith("https://"),
                         currency=str(env.get("CATALOG_CURRENCY") or "usd").lower())


def session_user(request: Request) -> Optional[str]:
    """With login-oauth: the session's user. Without it: nobody (every cart is anonymous)."""
    return request.session.get("user_id") if "session" in request.scope else None


def public_product(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k not in ("stock", "active")}
    out["in_stock"] = p["stock"] > 0
    return out


def _int_param(raw: Optional[str], default: int) -> Optional[int]:
    if raw is None:
        return default
    return int(raw) if raw.isdigit() else None


@dataclass
class Catalog:
    router: APIRouter
    store: CatalogStore


def create_catalog(config: CatalogConfig, *, store: Optional[CatalogStore] = None,
                   get_user_id: Callable[[Request], Optional[str]] = session_user) -> Catalog:
    store = store or CatalogStore(currency=config.currency)

    def sign(cart_id: str) -> str:
        mac = hmac.new(config.cart_secret.encode(), cart_id.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def read_cookie(request: Request) -> Optional[str]:
        raw = request.cookies.get(COOKIE) or ""
        cart_id, _, given = raw.rpartition(".")
        if not cart_id or not hmac.compare_digest(given.encode(), sign(cart_id).encode()):
            return None
        return cart_id

    cookie_flags = {"httponly": True, "samesite": "lax", "secure": config.secure_cookies, "path": "/"}

    def resolve(request: Request, cookies: list, create: bool = False) -> Optional[dict]:
        user_id = get_user_id(request)
        anon_id = read_cookie(request)
        if user_id:
            cart = None
            if anon_id:
                cart = store.merge_into(user_id, anon_id)  # the first request after signing in
                cookies.append(("delete", None))
            return cart or store.cart_for_user(user_id, create=create)
        cart = store.cart(anon_id)
        if cart and cart["user_id"]:
            cart = None  # a cookie never opens a signed-in user's cart
        if cart is None and create:
            cart = store.new_cart()
            cookies.append(("set", f"{cart['id']}.{sign(cart['id'])}"))
        return cart

    def respond(body: Any, status: int, cookies: list) -> Response:
        res = JSONResponse(body, status_code=status) if body is not None else Response(status_code=status)
        for op, value in cookies:
            if op == "set":
                res.set_cookie(COOKIE, value, max_age=30 * 24 * 3600, **cookie_flags)
            else:
                res.delete_cookie(COOKIE, **cookie_flags)
        return res

    async def handle(request: Request, fn, *, needs_json: bool = False) -> Response:
        cookies: list = []
        try:
            payload = None
            if needs_json:
                if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    return respond({"error": "json_required"}, 415, cookies)
                raw = await request.body()
                try:
                    payload = json.loads(raw) if len(raw) <= MAX_BODY else None
                except ValueError:
                    payload = None
                if not isinstance(payload, dict):
                    return respond({"error": "invalid_body"}, 400, cookies)
            status, body = fn(cookies, payload)
            return respond(body, status, cookies)
        except CatalogError as exc:
            return respond({"error": exc.code, **exc.extra}, exc.status, cookies)

    router = APIRouter()

    @router.get("/api/products")
    def products(request: Request) -> Response:
        qp = request.query_params
        page = _int_param(qp.get("page"), 1)
        size = _int_param(qp.get("page_size"), 12)
        q = qp.get("q") or ""
        if page is None or page < 1 or size is None or not 1 <= size <= MAX_PAGE_SIZE:
            return JSONResponse({"error": "invalid_page"}, status_code=400)
        if len(q) > MAX_QUERY:
            return JSONResponse({"error": "query_too_long"}, status_code=400)
        result = store.search(q, page, size)
        return JSONResponse({**result, "items": [public_product(p) for p in result["items"]]})

    @router.get("/api/products/{product_id}")
    def product(product_id: str) -> Response:
        p = store.active_product(product_id)
        if p is None:
            return JSONResponse({"error": "product_not_found"}, status_code=404)
        return JSONResponse(public_product(p))

    @router.get("/api/cart")
    async def cart(request: Request) -> Response:
        return await handle(request, lambda cookies, _: (200, store.view(resolve(request, cookies))))

    @router.post("/api/cart/items")
    async def add_item(request: Request) -> Response:
        def run(cookies, body):
            product_id = body.get("product_id")
            if not isinstance(product_id, str):
                raise CatalogError(400, "invalid_product")
            c = resolve(request, cookies, create=True)
            store.add_item(c, product_id, body.get("quantity", 1))
            return 201, store.view(c)
        return await handle(request, run, needs_json=True)

    @router.patch("/api/cart/items/{product_id}")
    async def set_quantity(product_id: str, request: Request) -> Response:
        def run(cookies, body):
            c = resolve(request, cookies)
            if c is None:
                raise CatalogError(404, "not_in_cart")
            store.set_quantity(c, product_id, body.get("quantity"))
            return 200, store.view(c)
        return await handle(request, run, needs_json=True)

    @router.delete("/api/cart/items/{product_id}")
    async def remove_item(product_id: str, request: Request) -> Response:
        def run(cookies, _):
            c = resolve(request, cookies)
            if c is None:
                raise CatalogError(404, "not_in_cart")
            store.remove_item(c, product_id)
            return 200, store.view(c)
        return await handle(request, run)

    @router.get("/api/cart/checkout")
    async def checkout(request: Request) -> Response:
        def run(cookies, _):
            c = resolve(request, cookies)
            return 200, store.checkout_lines(c["id"] if c else None)
        return await handle(request, run)

    return Catalog(router=router, store=store)
