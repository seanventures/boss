// The catalog-cart template's own tests, against its HTTP routes (not internals). The signed-in
// user comes from the app's login (login-oauth's session when applied); here a test-only
// getUserId reads a header, so merge-on-login is tested without a provider.
// Run: npm run test:catalog-cart
import { after, beforeEach, test } from "node:test";
import assert from "node:assert/strict";

import { CatalogStore, loadSeed } from "../../catalog/store.mjs";
import express from "express";

import { createCatalog } from "../../catalog/routes.mjs";

let server;
let origin;
let store;

beforeEach(async () => {
  if (server) {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  }
  store = new CatalogStore({ products: loadSeed() });
  const app = express();
  app.use(createCatalog(
    { cartSecret: "fake-cart-secret-for-the-template-tests-only", secureCookies: true, currency: "usd" },
    { store, getUserId: (req) => req.get("x-test-user") || null },
  ).router);
  server = app.listen(0, "127.0.0.1");
  await new Promise((r) => server.once("listening", r));
  origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server.closeAllConnections();
  await new Promise((r) => server.close(r));
});

class Browser {
  constructor(user = null) {
    this.jar = new Map();
    this.setCookies = [];
    this.user = user;
  }

  async request(method, path, body, headers = {}) {
    const h = { ...headers };
    if (this.user) h["X-Test-User"] = this.user;
    if (this.jar.size) h.Cookie = [...this.jar].map(([k, v]) => `${k}=${v}`).join("; ");
    if (body !== undefined && !h["Content-Type"]) h["Content-Type"] = "application/json";
    const res = await fetch(origin + path, {
      method,
      headers: h,
      body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
    });
    for (const raw of res.headers.getSetCookie()) {
      this.setCookies.push(raw);
      const [pair] = raw.split(";");
      const name = pair.slice(0, pair.indexOf("=")).trim();
      const value = pair.slice(pair.indexOf("=") + 1);
      if (!value || /expires=Thu, 01 Jan 1970/i.test(raw)) this.jar.delete(name);
      else this.jar.set(name, value);
    }
    const text = await res.text();
    return { status: res.status, body: text ? JSON.parse(text) : null };
  }

  get(path) {
    return this.request("GET", path);
  }

  add(productId, quantity) {
    return this.request("POST", "/api/cart/items", quantity === undefined ? { product_id: productId } : { product_id: productId, quantity });
  }
}

test("the list shows active products only, 12 to a page, with no stock counts", async () => {
  const { status, body } = await new Browser().get("/api/products");
  assert.equal(status, 200);
  assert.equal(body.total, 18);
  assert.equal(body.items.length, 12);
  assert.equal(body.pages, 2);
  assert.ok(body.items.every((p) => !("stock" in p) && typeof p.in_stock === "boolean"));
  const all = (await new Browser().get("/api/products?page_size=50")).body.items.map((p) => p.id);
  assert.ok(!all.includes("p19") && !all.includes("p20"), "inactive products are hidden");
});

test("search matches name, description and SKU, case-insensitively", async () => {
  const b = new Browser();
  assert.deepEqual((await b.get("/api/products?q=MUG")).body.items.map((p) => p.id), ["p02"]);
  assert.deepEqual((await b.get("/api/products?q=sku-004")).body.items.map((p) => p.id), ["p04"]);
  assert.deepEqual((await b.get("/api/products?q=dishwasher")).body.items.map((p) => p.id), ["p02"]);
  const none = (await b.get("/api/products?q=withdrawn")).body;
  assert.equal(none.total, 0, "an inactive product is not found by search either");
});

test("pagination bounds are enforced", async () => {
  const b = new Browser();
  for (const q of ["page=0", "page=-1", "page=abc", "page=1.5", "page_size=0", "page_size=51"]) {
    assert.equal((await b.get(`/api/products?${q}`)).status, 400, q);
  }
  assert.equal((await b.get(`/api/products?q=${"x".repeat(101)}`)).status, 400);
  const p1 = (await b.get("/api/products?page=1&page_size=10")).body.items.map((p) => p.id);
  const p2 = (await b.get("/api/products?page=2&page_size=10")).body.items.map((p) => p.id);
  assert.equal(p1.length, 10);
  assert.equal(p2.length, 8);
  assert.equal(new Set([...p1, ...p2]).size, 18);
  assert.deepEqual((await b.get("/api/products?page=9")).body.items, []);
});

test("detail answers an active product and 404 for an inactive or unknown one", async () => {
  const b = new Browser();
  const one = await b.get("/api/products/p05");
  assert.equal(one.status, 200);
  assert.equal(one.body.price_cents, 3850);
  assert.equal((await b.get("/api/products/p19")).status, 404);
  assert.equal((await b.get("/api/products/nope")).status, 404);
});

test("cart totals are server-computed in integer cents", async () => {
  const b = new Browser();
  assert.equal((await b.add("p01", 3)).status, 201); // 1999
  const { body } = await b.add("p09", 2); // 999
  assert.equal(body.subtotal_cents, 3 * 1999 + 2 * 999);
  assert.equal(body.item_count, 5);
  assert.ok(Number.isInteger(body.subtotal_cents));
  assert.deepEqual(body.items.map((i) => [i.product_id, i.line_total_cents]), [["p01", 5997], ["p09", 1998]]);
  const cheat = await b.request("POST", "/api/cart/items", { product_id: "p02", quantity: 1, price_cents: 1 });
  assert.equal(cheat.body.items.find((i) => i.product_id === "p02").unit_price_cents, 1450, "a client price is ignored");
});

test("quantity bounds are enforced", async () => {
  const b = new Browser();
  for (const q of [0, -1, 100, 1.5, "2", null]) {
    assert.equal((await b.add("p01", q)).status, 400, String(q));
  }
  await b.add("p12", 90);
  assert.equal((await b.add("p12", 10)).status, 400, "the line total is bounded too");
});

test("the stock limit is enforced", async () => {
  const b = new Browser();
  assert.equal((await b.add("p03", 4)).status, 409); // stock 3
  assert.equal((await b.add("p03", 3)).status, 201);
  const more = await b.add("p03", 1);
  assert.equal(more.status, 409);
  assert.equal(more.body.error, "insufficient_stock");
  assert.equal((await b.add("p17", 1)).status, 409, "out of stock");
  assert.equal((await b.add("p19", 1)).status, 404, "an inactive product cannot be added");
});

test("changing and removing lines", async () => {
  const b = new Browser();
  await b.add("p01", 1);
  await b.add("p02", 1);
  assert.equal((await b.request("PATCH", "/api/cart/items/p01", { quantity: 4 })).body.subtotal_cents, 4 * 1999 + 1450);
  assert.equal((await b.request("PATCH", "/api/cart/items/p03", { quantity: 4 })).status, 404, "not in the cart");
  assert.equal((await b.request("PATCH", "/api/cart/items/p01", { quantity: 0 })).body.items.length, 1);
  assert.equal((await b.request("DELETE", "/api/cart/items/p02")).body.items.length, 0);
  assert.equal((await b.request("DELETE", "/api/cart/items/p02")).status, 404);
});

test("carts are isolated between sessions and a forged cookie opens nothing", async () => {
  const a = new Browser();
  await a.add("p01", 2);
  const b = new Browser();
  assert.deepEqual((await b.get("/api/cart")).body.items, []);
  const [id] = a.jar.get("cart").split(".");
  const forger = new Browser();
  forger.jar.set("cart", `${id}.forged-signature`);
  assert.deepEqual((await forger.get("/api/cart")).body.items, []);
  forger.jar.set("cart", id);
  assert.deepEqual((await forger.get("/api/cart")).body.items, []);
  assert.equal((await a.get("/api/cart")).body.item_count, 2);
});

test("the anonymous cart merges into the user's cart on sign-in", async () => {
  const before = new Browser("user-1");
  await before.add("p01", 1);
  await before.add("p02", 1);
  const b = new Browser();
  await b.add("p01", 2);
  await b.add("p03", 3);
  b.user = "user-1"; // signs in
  const merged = await b.get("/api/cart");
  assert.deepEqual(
    merged.body.items.map((i) => [i.product_id, i.quantity]).sort(),
    [["p01", 3], ["p02", 1], ["p03", 3]],
  );
  assert.ok(!b.jar.has("cart"), "the anonymous cart cookie is cleared");
  assert.equal((await new Browser("user-1").get("/api/cart")).body.item_count, 7, "the user's cart, on any device");
  b.user = null;
  assert.deepEqual((await b.get("/api/cart")).body.items, [], "signed out, the old cookie opens nothing");
});

test("checkout uses the price at checkout time and refuses what cannot be sold", async () => {
  const b = new Browser();
  assert.equal((await b.get("/api/cart/checkout")).body.error, "cart_empty");
  await b.add("p01", 2);
  await b.add("p02", 1);
  store.setPrice("p01", 2499);
  const { status, body } = await b.get("/api/cart/checkout");
  assert.equal(status, 200);
  assert.equal(body.cart_id, b.jar.get("cart").split(".")[0]);
  assert.equal(body.currency, "usd");
  assert.deepEqual(body.lines.map((l) => [l.product_id, l.unit_amount_cents, l.quantity, l.line_total_cents]),
    [["p01", 2499, 2, 4998], ["p02", 1450, 1, 1450]]);
  assert.equal(body.total_cents, 6448);
  assert.equal((await b.get("/api/cart")).body.subtotal_cents, 6448, "the cart shows the new price too");
  store.setActive("p02", false);
  const refused = await b.get("/api/cart/checkout");
  assert.equal(refused.status, 409);
  assert.deepEqual(refused.body, { error: "product_unavailable", product_id: "p02" });
});

test("cart changes need JSON (CSRF) and the cart cookie is HttpOnly, Secure, SameSite=Lax", async () => {
  const b = new Browser();
  const form = await b.request("POST", "/api/cart/items", "product_id=p01&quantity=1", { "Content-Type": "application/x-www-form-urlencoded" });
  assert.equal(form.status, 415);
  assert.equal((await b.request("POST", "/api/cart/items", "{not json")).status, 400);
  await b.add("p01", 1);
  const cookie = b.setCookies.find((c) => c.startsWith("cart="));
  assert.match(cookie, /;\s*HttpOnly/i);
  assert.match(cookie, /;\s*Secure/i);
  assert.match(cookie, /;\s*SameSite=Lax/i);
});
