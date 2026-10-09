// Products, carts and the checkout contract (schema.sql has the tables). This in-memory store
// is the reference; back the same methods with the app's database when adapting the template.
// Money is integer cents everywhere. A cart line holds no price: totals and checkout always use
// the product's price NOW, so a client can never choose what it pays.
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

export const MAX_QUANTITY = 99;

export class CatalogError extends Error {
  constructor(status, code, extra = {}) {
    super(code);
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

export function loadSeed(url = new URL("../seed/products.json", import.meta.url)) {
  return JSON.parse(readFileSync(url, "utf8"));
}

export class CatalogStore {
  constructor({ products = loadSeed(), currency = "usd" } = {}) {
    this.currency = currency;
    this.products = new Map(products.map((p) => [p.id, { ...p }]));
    this.carts = new Map(); // cart id -> {id, userId, items: Map(productId -> quantity)}
    this.userCarts = new Map(); // user id -> cart id
  }

  // ---- products ---------------------------------------------------------------------------
  activeProduct(id) {
    const p = this.products.get(id);
    return p && p.active ? p : null;
  }

  search({ q = "", page = 1, pageSize = 12 } = {}) {
    const needle = q.trim().toLowerCase();
    const hits = [...this.products.values()]
      .filter((p) => p.active)
      .filter((p) => !needle || [p.name, p.description, p.sku].some((f) => f.toLowerCase().includes(needle)))
      .sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
    const start = (page - 1) * pageSize;
    return {
      items: hits.slice(start, start + pageSize),
      page,
      page_size: pageSize,
      total: hits.length,
      pages: Math.max(1, Math.ceil(hits.length / pageSize)),
    };
  }

  // An operator's change (the app's admin screens call these).
  setPrice(id, priceCents) {
    if (!Number.isSafeInteger(priceCents) || priceCents < 0) throw new CatalogError(400, "invalid_price");
    this.products.get(id).price_cents = priceCents;
  }

  setActive(id, active) {
    this.products.get(id).active = Boolean(active);
  }

  // ---- carts --------------------------------------------------------------------------------
  newCart(userId = null) {
    const cart = { id: randomUUID(), userId, items: new Map() };
    this.carts.set(cart.id, cart);
    if (userId) this.userCarts.set(userId, cart.id);
    return cart;
  }

  cart(id) {
    return (id && this.carts.get(id)) || null;
  }

  cartForUser(userId, { create = false } = {}) {
    const cart = this.cart(this.userCarts.get(userId));
    return cart || (create ? this.newCart(userId) : null);
  }

  #checkQuantity(quantity, { allowZero = false } = {}) {
    if (!Number.isInteger(quantity) || quantity < (allowZero ? 0 : 1) || quantity > MAX_QUANTITY) {
      throw new CatalogError(400, "invalid_quantity");
    }
  }

  #checkStock(product, quantity) {
    if (quantity > product.stock) throw new CatalogError(409, "insufficient_stock", { available: product.stock });
  }

  addItem(cart, productId, quantity) {
    this.#checkQuantity(quantity);
    const product = this.activeProduct(productId);
    if (!product) throw new CatalogError(404, "product_not_found");
    const next = (cart.items.get(productId) || 0) + quantity;
    if (next > MAX_QUANTITY) throw new CatalogError(400, "invalid_quantity");
    this.#checkStock(product, next);
    cart.items.set(productId, next);
  }

  setQuantity(cart, productId, quantity) {
    this.#checkQuantity(quantity, { allowZero: true });
    if (!cart.items.has(productId)) throw new CatalogError(404, "not_in_cart");
    if (quantity === 0) {
      cart.items.delete(productId);
      return;
    }
    const product = this.activeProduct(productId);
    if (!product) throw new CatalogError(409, "product_unavailable", { product_id: productId });
    this.#checkStock(product, quantity);
    cart.items.set(productId, quantity);
  }

  removeItem(cart, productId) {
    if (!cart.items.delete(productId)) throw new CatalogError(404, "not_in_cart");
  }

  // Signing in: the anonymous cart's lines join the user's cart (quantities added, capped at
  // stock and MAX_QUANTITY) and the anonymous cart is deleted.
  mergeInto(userId, anonCartId) {
    const anon = this.cart(anonCartId);
    if (!anon || anon.userId) return this.cartForUser(userId);
    const target = this.cartForUser(userId, { create: true });
    for (const [productId, qty] of anon.items) {
      const product = this.activeProduct(productId);
      if (!product) continue;
      const merged = Math.min((target.items.get(productId) || 0) + qty, product.stock, MAX_QUANTITY);
      if (merged > 0) target.items.set(productId, merged);
    }
    this.carts.delete(anon.id);
    return target;
  }

  view(cart) {
    const items = [];
    let subtotal = 0;
    let count = 0;
    for (const [productId, quantity] of cart ? cart.items : []) {
      const p = this.products.get(productId);
      const available = Boolean(p && p.active && p.stock >= quantity);
      const line = p.price_cents * quantity;
      items.push({
        product_id: productId,
        sku: p.sku,
        name: p.name,
        unit_price_cents: p.price_cents,
        quantity,
        line_total_cents: line,
        available,
      });
      subtotal += line;
      count += quantity;
    }
    return { id: cart ? cart.id : null, currency: this.currency, items, item_count: count, subtotal_cents: subtotal };
  }

  // The cart priced for checkout (GET /api/cart/checkout; routes.mjs's cartLineItems, the
  // payments contract, is built on it): a cart id in, the lines to charge out, every price read
  // from the product now. Refuses an empty cart, a product no longer on
  // sale and a quantity above stock, so a payment template never charges for what cannot ship.
  checkoutLines(cartId) {
    const cart = this.cart(cartId);
    if (!cart || cart.items.size === 0) throw new CatalogError(409, "cart_empty");
    const lines = [];
    let total = 0;
    for (const [productId, quantity] of cart.items) {
      const p = this.activeProduct(productId);
      if (!p) throw new CatalogError(409, "product_unavailable", { product_id: productId });
      this.#checkStock(p, quantity);
      const line = p.price_cents * quantity;
      if (!Number.isSafeInteger(line)) throw new CatalogError(409, "total_out_of_range");
      lines.push({ product_id: p.id, sku: p.sku, name: p.name, unit_amount_cents: p.price_cents, quantity, line_total_cents: line });
      total += line;
    }
    return { cart_id: cart.id, currency: this.currency, lines, total_cents: total };
  }
}
