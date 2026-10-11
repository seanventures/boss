// The catalog-cart routes for an Express app:
//   GET    /api/products?q=&page=&page_size=  search the active products (page_size <= 50)
//   GET    /api/products/:id                  one active product (404 otherwise)
//   GET    /api/cart                          the caller's cart with server-computed totals
//   POST   /api/cart/items                    {product_id, quantity} adds to the cart
//   PATCH  /api/cart/items/:product_id        {quantity} sets it (0 removes)
//   DELETE /api/cart/items/:product_id        removes the line
//   GET    /api/cart/checkout                 the caller's cart priced for checkout (checkoutLines)
// And, in code, the payments contract (spec §3.4) that stripe-checkout and paypal-checkout call:
//   catalog.getProduct(id)             -> {id, name, unit_amount_cents, currency, active} | null
//   catalog.cartLineItems(cartId, req) -> [{product_id, name, unit_amount_cents, currency, quantity}]
//                                         | null when `cartId` is not the requester's cart
// so the object createCatalog returns IS the payment template's `catalog`.
// An anonymous cart is found by a signed `cart` cookie; a signed-in user's (getUserId) by user,
// and the anonymous cart merges into it on the first request after sign-in.
import { createHmac, timingSafeEqual } from "node:crypto";
import express from "express";

import { CatalogError, CatalogStore } from "./store.mjs";

const COOKIE = "cart";
const MAX_PAGE_SIZE = 50;
const MAX_QUERY = 100;

function publicProduct(p) {
  const { stock, active, ...rest } = p;
  return { ...rest, in_stock: stock > 0 };
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.cookie || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

// With login-oauth: the session's user. Without it: nobody (every cart is anonymous).
const sessionUser = (req) => (req.session && req.session.userId) || null;

export function createCatalog(config, { store = new CatalogStore({ currency: config.currency }), getUserId = sessionUser } = {}) {
  const sign = (id) => createHmac("sha256", config.cartSecret).update(id).digest("base64url");

  function readCartCookie(req) {
    const raw = cookies(req)[COOKIE];
    if (!raw) return null;
    const i = raw.lastIndexOf(".");
    if (i < 1) return null;
    const id = raw.slice(0, i);
    const given = Buffer.from(raw.slice(i + 1));
    const want = Buffer.from(sign(id));
    return given.length === want.length && timingSafeEqual(given, want) ? id : null;
  }

  const cookieOptions = { httpOnly: true, sameSite: "lax", secure: config.secureCookies, path: "/" };

  function resolveCart(req, res, { create = false } = {}) {
    const userId = getUserId(req);
    const anonId = readCartCookie(req);
    if (userId) {
      let cart = null;
      if (anonId) {
        cart = store.mergeInto(userId, anonId); // the first request after signing in
        if (res) res.clearCookie(COOKIE, cookieOptions); // (none from cartLineItems: a stale cookie opens nothing)
      }
      return cart || store.cartForUser(userId, { create });
    }
    let cart = store.cart(anonId);
    if (cart && cart.userId) cart = null; // a cookie never opens a signed-in user's cart
    if (!cart && create) {
      cart = store.newCart();
      res.cookie(COOKIE, `${cart.id}.${sign(cart.id)}`, { ...cookieOptions, maxAge: 30 * 24 * 3600 * 1000 });
    }
    return cart;
  }

  // Cart changes are JSON only: a cross-site HTML form cannot send application/json, so with
  // the SameSite=Lax cookie this is the CSRF defence for the cart.
  function jsonOnly(req, res, next) {
    if (!req.is("application/json")) return res.status(415).json({ error: "json_required" });
    return next();
  }

  const router = express.Router();
  router.use("/api/cart", express.json({ limit: "10kb" }));

  router.get("/api/products", (req, res) => {
    const page = req.query.page === undefined ? 1 : Number(req.query.page);
    const pageSize = req.query.page_size === undefined ? 12 : Number(req.query.page_size);
    const q = typeof req.query.q === "string" ? req.query.q : "";
    if (!Number.isInteger(page) || page < 1 || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > MAX_PAGE_SIZE) {
      return res.status(400).json({ error: "invalid_page" });
    }
    if (q.length > MAX_QUERY) return res.status(400).json({ error: "query_too_long" });
    const result = store.search({ q, page, pageSize });
    return res.json({ ...result, items: result.items.map(publicProduct) });
  });

  router.get("/api/products/:id", (req, res) => {
    const p = store.activeProduct(req.params.id);
    return p ? res.json(publicProduct(p)) : res.status(404).json({ error: "product_not_found" });
  });

  router.get("/api/cart", (req, res) => res.json(store.view(resolveCart(req, res))));

  router.post("/api/cart/items", jsonOnly, (req, res) => {
    const { product_id: productId, quantity = 1 } = req.body || {};
    if (typeof productId !== "string") return res.status(400).json({ error: "invalid_product" });
    const cart = resolveCart(req, res, { create: true });
    store.addItem(cart, productId, quantity);
    return res.status(201).json(store.view(cart));
  });

  router.patch("/api/cart/items/:productId", jsonOnly, (req, res) => {
    const cart = resolveCart(req, res);
    if (!cart) return res.status(404).json({ error: "not_in_cart" });
    store.setQuantity(cart, req.params.productId, (req.body || {}).quantity);
    return res.json(store.view(cart));
  });

  router.delete("/api/cart/items/:productId", (req, res) => {
    const cart = resolveCart(req, res);
    if (!cart) return res.status(404).json({ error: "not_in_cart" });
    store.removeItem(cart, req.params.productId);
    return res.json(store.view(cart));
  });

  router.get("/api/cart/checkout", (req, res) => {
    const cart = resolveCart(req, res);
    return res.json(store.checkoutLines(cart ? cart.id : null));
  });

  // Errors: the store's are fixed codes; a malformed JSON body is a 400; nothing else leaks.
  router.use((err, req, res, next) => {
    if (err instanceof CatalogError) return res.status(err.status).json({ error: err.code, ...err.extra });
    if (err.type === "entity.parse.failed" || err.type === "entity.too.large") {
      return res.status(400).json({ error: "invalid_body" });
    }
    return next(err);
  });

  // ---- the payments contract (spec §3.4) -------------------------------------------------------
  function getProduct(id) {
    const p = store.products.get(String(id));
    return p ? { id: p.id, name: p.name, unit_amount_cents: p.price_cents, currency: store.currency, active: Boolean(p.active) } : null;
  }

  // The lines of cart `cartId`, priced now, ONLY when it is the requester's own cart, found from
  // the request exactly as the routes find it (signed cookie, or the signed-in user's cart);
  // null for any other id, so a guessed or leaked cart id buys nothing. [] for an empty cart.
  // A cart that cannot be sold now (a withdrawn product, a quantity above stock) throws the
  // store's CatalogError (409 + code), as GET /api/cart/checkout answers.
  function cartLineItems(cartId, req) {
    const cart = resolveCart(req, null);
    if (!cart || typeof cartId !== "string" || cart.id !== cartId) return null;
    if (cart.items.size === 0) return [];
    const { currency, lines } = store.checkoutLines(cart.id);
    return lines.map((l) => ({ product_id: l.product_id, name: l.name, unit_amount_cents: l.unit_amount_cents, currency, quantity: l.quantity }));
  }

  return { router, store, resolveCart, getProduct, cartLineItems };
}

export function loadCatalogConfig(env = process.env) {
  const baseUrl = (env.APP_BASE_URL || "http://localhost:3000").replace(/\/+$/, "");
  const cartSecret = (env.CART_SECRET || "").trim();
  if (cartSecret.length < 32) throw new Error("CART_SECRET must be set to at least 32 characters (BOSS generates one)");
  return { cartSecret, secureCookies: baseUrl.startsWith("https://"), currency: (env.CATALOG_CURRENCY || "usd").toLowerCase() };
}
