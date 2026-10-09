// The `paypal-checkout` template's own tests, against its HTTP routes (not internals), with PayPal
// replaced by an in-process fake. No test reaches the network or uses a real credential.
import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { startFakePayPal, signPayPalEvent, newCredentials } from "./fake_paypal.mjs";

const require = createRequire(import.meta.url);
const { createApp } = require("../../server.js");
const { loadPayPalConfig, HOSTS } = require("../../payments/config.js");
const { PayPalClient } = require("../../payments/paypal_client.js");
const { staticCatalog, DEFAULT_PRODUCTS } = require("../../payments/catalog.js");
const { toValue, fromValue } = require("../../payments/paypal.js");

// One app and one fake PayPal per test.
async function boot({ env = {}, catalog, isAdmin, clock } = {}) {
  const creds = newCredentials();
  const fake = await startFakePayPal(creds);
  const fullEnv = {
    PAYPAL_CLIENT_ID: creds.clientId,
    PAYPAL_CLIENT_SECRET: creds.clientSecret,
    PAYPAL_WEBHOOK_ID: creds.webhookId,
    APP_BASE_URL: "http://shop.test",
    ...env,
  };
  const paypal = new PayPalClient({ apiBase: fake.url, clientId: creds.clientId, clientSecret: creds.clientSecret, now: clock });
  const paid = [];
  const { app, store } = createApp({ env: fullEnv, paypal, catalog, isAdmin, onOrderPaid: (o) => paid.push(o.id) });
  const server = await new Promise((resolve) => {
    const s = app.listen(0, "127.0.0.1", () => resolve(s));
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const ctx = {
    fake, store, paid, base, creds, paypal,
    async post(path, body, headers = {}) {
      const r = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body || {}) });
      return { status: r.status, body: await r.json() };
    },
    async get(path) {
      const r = await fetch(base + path, { headers: { Accept: "application/json" } });
      return { status: r.status, body: await r.json() };
    },
    async create(items = [{ product_id: "starter-pack", quantity: 2 }]) {
      const r = await ctx.post("/api/paypal/orders", { items });
      assert.equal(r.status, 201, JSON.stringify(r.body));
      return r.body;
    },
    async webhook(event, { headers, secret = fake.signingSecret, webhookId = creds.webhookId, body } = {}) {
      const h = headers !== undefined ? headers : signPayPalEvent(event, { secret, webhookId });
      const r = await fetch(base + "/webhooks/paypal", { method: "POST", headers: { "Content-Type": "application/json", ...h }, body: body || JSON.stringify(event) });
      return { status: r.status, body: await r.json() };
    },
    async close() {
      await new Promise((r) => server.close(r));
      await fake.close();
    },
  };
  return ctx;
}

function captureEvent(type, paypalOrderId, orderId, amount, captureId = "CAP" + crypto.randomBytes(6).toString("hex").toUpperCase()) {
  return {
    id: "WH-" + crypto.randomUUID(), event_type: type, resource_type: "capture",
    resource: { id: captureId, status: type === "PAYMENT.CAPTURE.COMPLETED" ? "COMPLETED" : "DECLINED", amount, custom_id: orderId,
      supplementary_data: { related_ids: { order_id: paypalOrderId } } },
  };
}

// ---- configuration: sandbox by default -----------------------------------------------------------

test("the sandbox is the default and live needs PAYPAL_ENV=live AND APP_ENV=production", () => {
  const creds = { PAYPAL_CLIENT_ID: "id", PAYPAL_CLIENT_SECRET: "x".repeat(8) };
  const cfg = loadPayPalConfig(creds);
  assert.equal(cfg.env, "sandbox");
  assert.equal(cfg.apiBase, HOSTS.sandbox);
  assert.equal(cfg.apiBase, "https://api-m.sandbox.paypal.com");
  assert.throws(() => loadPayPalConfig({ ...creds, PAYPAL_ENV: "live" }), /only allowed when APP_ENV=production/);
  assert.throws(() => loadPayPalConfig({ ...creds, PAYPAL_ENV: "live", APP_ENV: "preview" }), /APP_ENV=production/);
  assert.equal(loadPayPalConfig({ ...creds, PAYPAL_ENV: "live", APP_ENV: "production" }).apiBase, "https://api-m.paypal.com");
  assert.throws(() => loadPayPalConfig({ ...creds, PAYPAL_ENV: "https://evil.example" }), /PAYPAL_ENV must be/);
  assert.throws(() => loadPayPalConfig({ PAYPAL_CLIENT_ID: "id" }), (err) => /PAYPAL_CLIENT_SECRET is not set/.test(err.message));
});

test("money is converted in integer minor units, never through floats", () => {
  assert.equal(toValue(3000, "usd"), "30.00");
  assert.equal(toValue(1, "usd"), "0.01");
  assert.equal(toValue(1999, "eur"), "19.99");
  assert.equal(toValue(500, "jpy"), "500");
  assert.equal(fromValue("30.00", "usd"), 3000);
  assert.equal(fromValue("30.1", "usd"), 3010);
  assert.equal(fromValue("0.29", "usd"), 29);
  assert.equal(fromValue("1e3", "usd"), null);
  assert.equal(fromValue("-1.00", "usd"), null);
});

test("the config route gives the JS SDK the client id and env, never the secret", async () => {
  const app = await boot();
  try {
    const r = await app.get("/api/paypal/config");
    assert.deepEqual(r.body, { env: "sandbox", client_id: app.creds.clientId, currency: "USD" });
    assert.ok(!JSON.stringify(r.body).includes(app.creds.clientSecret));
  } finally {
    await app.close();
  }
});

// ---- prices come from the server -----------------------------------------------------------------

test("creating an order prices it on the server, with an idempotency key and the order id attached", async () => {
  const app = await boot();
  try {
    const out = await app.create([{ product_id: "starter-pack", quantity: 2 }]);
    const pp = app.fake.state.orders.get(out.paypal_order_id);
    const unit = pp.purchase_units[0];
    assert.equal(pp.intent, "CAPTURE");
    assert.deepEqual(unit.amount, { currency_code: "USD", value: "30.00", breakdown: { item_total: { currency_code: "USD", value: "30.00" } } });
    assert.deepEqual(unit.items, [{ name: "Starter pack", sku: "starter-pack", quantity: "2", unit_amount: { currency_code: "USD", value: "15.00" } }]);
    assert.equal(unit.custom_id, out.order_id);
    assert.equal(pp.request_id, `create-${out.order_id}`);
    assert.equal(pp.payment_source.paypal.experience_context.return_url, "http://shop.test/paypal/return");
    assert.equal(out.approve_url, `https://www.sandbox.paypal.com/checkoutnow?token=${out.paypal_order_id}`);
    assert.equal((await app.get(`/api/orders/${out.order_id}`)).body.status, "pending");
  } finally {
    await app.close();
  }
});

test("a tampered client price, amount or currency is ignored", async () => {
  const app = await boot();
  try {
    const r = await app.post("/api/paypal/orders", { amount: "0.01", currency_code: "JPY",
      items: [{ product_id: "pro-pack", quantity: 1, price: 1, unit_amount: { value: "0.01" }, unit_amount_cents: 1, currency: "jpy" }] });
    assert.equal(r.status, 201);
    const unit = app.fake.state.orders.get(r.body.paypal_order_id).purchase_units[0];
    assert.equal(unit.amount.value, "49.00");
    assert.equal(unit.amount.currency_code, "USD");
  } finally {
    await app.close();
  }
});

test("unknown products, empty orders and bad quantities are refused before PayPal is called", async () => {
  const app = await boot();
  try {
    for (const body of [
      { items: [{ product_id: "nope", quantity: 1 }] }, { items: [] }, {},
      { items: [{ product_id: "starter-pack", quantity: 0 }] },
      { items: [{ product_id: "starter-pack", quantity: 2.5 }] },
      { items: [{ product_id: "starter-pack", quantity: "1" }] },
      { items: [{ product_id: "starter-pack", quantity: 100 }] },
    ]) {
      assert.equal((await app.post("/api/paypal/orders", body)).status, 400, JSON.stringify(body));
    }
    assert.equal(app.fake.state.orders.size, 0);
  } finally {
    await app.close();
  }
});

test("an order from a catalog-cart cart uses the cart's server prices", async () => {
  const catalog = {
    ...staticCatalog(DEFAULT_PRODUCTS),
    cartLineItems: (cartId) => (cartId === "cart-abc" ? [{ product_id: "tee-1", name: "T-shirt", unit_amount_cents: 2599, currency: "usd", quantity: 3 }] : null),
  };
  const app = await boot({ catalog });
  try {
    const r = await app.post("/api/paypal/orders", { cart_id: "cart-abc", total: "0.01" });
    assert.equal(r.status, 201);
    assert.equal(app.fake.state.orders.get(r.body.paypal_order_id).purchase_units[0].amount.value, "77.97");
    assert.equal((await app.post("/api/paypal/orders", { cart_id: "someone-elses" })).status, 400);
  } finally {
    await app.close();
  }
});

// catalog-cart's provider refuses a cart it cannot sell now with its CatalogError (4xx + code). The
// payment template maps it itself (by name and shape, no import): a 400, never a 500, and no order
// or PayPal call. Declared here exactly as catalog-cart declares it.
class CatalogError extends Error {
  constructor(status, code, extra = {}) {
    super(code);
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

test("a cart catalog-cart cannot sell (its CatalogError) is a 400 with its code before PayPal is called", async () => {
  let failure = new CatalogError(409, "product_unavailable", { product_id: "tee-1" });
  const catalog = {
    ...staticCatalog(DEFAULT_PRODUCTS),
    cartLineItems() {
      throw failure;
    },
    getProduct(id) {
      if (id === "gone") throw new CatalogError(404, "product_not_found");
      return staticCatalog(DEFAULT_PRODUCTS).getProduct(id);
    },
  };
  const app = await boot({ catalog });
  try {
    for (const [body, code] of [
      [{ cart_id: "cart-abc" }, "product_unavailable"],
      [{ items: [{ product_id: "gone", quantity: 1 }] }, "product_not_found"],
    ]) {
      const r = await app.post("/api/paypal/orders", body);
      assert.equal(r.status, 400, JSON.stringify(r.body));
      assert.deepEqual(r.body, { error: code });
    }
    failure = new CatalogError(409, "cart_empty");
    assert.deepEqual((await app.post("/api/paypal/orders", { cart_id: "cart-abc" })).body, { error: "cart_empty" });
    // Anything else from the catalog is the app's own error, not the client's: never the 400.
    const status = async () => (await fetch(app.base + "/api/paypal/orders", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cart_id: "cart-abc" }),
    })).status;
    failure = new TypeError("database is down");
    assert.equal(await status(), 500);
    // A look-alike that is not a CatalogError, and a CatalogError that is not a refusal (5xx), are
    // left to the app's error handler (Express answers with the error's own status), not mapped.
    failure = Object.assign(new Error("look-alike"), { status: 409, code: "product_unavailable" });
    assert.equal(await status(), 409);
    failure = new CatalogError(503, "catalog_down");
    assert.equal(await status(), 503);
    assert.equal(app.fake.state.requests.length, 0, "PayPal must not be called");
    assert.equal(app.fake.state.orders.size, 0);
  } finally {
    await app.close();
  }
});

// ---- capture: only ours, only once ---------------------------------------------------------------

test("capture is refused for a PayPal order this app did not create, without calling PayPal", async () => {
  const app = await boot();
  try {
    const before = app.fake.state.requests.length;
    assert.equal((await app.post("/api/paypal/orders/5O190127TN364715T/capture")).status, 404);
    assert.equal((await app.post("/api/paypal/orders/..%2F..%2Fv1/capture")).status, 400);
    assert.equal(app.fake.state.requests.length, before);
  } finally {
    await app.close();
  }
});

test("capture pays the order once: a double capture is idempotent and ships once", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    app.fake.approve(out.paypal_order_id);
    const first = await app.post(`/api/paypal/orders/${out.paypal_order_id}/capture`);
    assert.deepEqual(first.body, { order_id: out.order_id, status: "paid" });
    const captureCalls = () => app.fake.state.requests.filter((r) => r.url.endsWith("/capture")).length;
    const n = captureCalls();
    const second = await app.post(`/api/paypal/orders/${out.paypal_order_id}/capture`);
    assert.deepEqual(second.body, { order_id: out.order_id, status: "paid" });
    assert.equal(captureCalls(), n, "an already-paid order is not captured again");
    assert.deepEqual(app.paid, [out.order_id]);
    const req = app.fake.state.requests.find((r) => r.url.endsWith("/capture"));
    assert.equal(req.headers["paypal-request-id"], `capture-${out.order_id}`);
  } finally {
    await app.close();
  }
});

test("a capture for a different amount than the server priced is held for review", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    app.fake.approve(out.paypal_order_id);
    app.fake.state.captureAmountOverride = { currency_code: "USD", value: "0.01" };
    const r = await app.post(`/api/paypal/orders/${out.paypal_order_id}/capture`);
    assert.equal(r.body.status, "needs_review");
    assert.deepEqual(app.paid, []);
  } finally {
    await app.close();
  }
});

test("a declined instrument answers 402 restart and leaves the order open for another try", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    app.fake.approve(out.paypal_order_id);
    app.fake.state.declineNext = true;
    const r = await app.post(`/api/paypal/orders/${out.paypal_order_id}/capture`);
    assert.equal(r.status, 402);
    assert.equal(r.body.restart, true);
    assert.equal(app.store.getOrder(out.order_id).status, "pending");
  } finally {
    await app.close();
  }
});

test("the return page captures server-side (private preview) and reconciles an order captured elsewhere", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    app.fake.approve(out.paypal_order_id);
    // Captured by another path first (another tab) with a different request id.
    await app.paypal.captureOrder(out.paypal_order_id, "elsewhere");
    const r = await app.get(`/paypal/return?token=${out.paypal_order_id}&PayerID=PAYER1`);
    assert.equal(r.status, 200);
    assert.equal(r.body.status, "paid");
    const page = await fetch(`${app.base}/paypal/return?token=${out.paypal_order_id}`, { headers: { Accept: "text/html" } });
    assert.match(await page.text(), /Thank you/);
    assert.deepEqual(app.paid, [out.order_id]);
  } finally {
    await app.close();
  }
});

// ---- webhooks: verified by PayPal, idempotent ----------------------------------------------------

test("a webhook that fails PayPal's verification is refused with 400 and changes nothing", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    const ev = captureEvent("PAYMENT.CAPTURE.COMPLETED", out.paypal_order_id, out.order_id, { currency_code: "USD", value: "30.00" });
    // Forged: signed with another key.
    assert.equal((await app.webhook(ev, { secret: crypto.randomBytes(32).toString("hex") })).status, 400);
    // Signed for another webhook id (another app's endpoint).
    assert.equal((await app.webhook(ev, { webhookId: "OTHERWEBHOOK" })).status, 400);
    // Signed, then edited.
    const headers = signPayPalEvent(ev, { secret: app.fake.signingSecret, webhookId: app.creds.webhookId });
    const edited = JSON.stringify({ ...ev, resource: { ...ev.resource, amount: { currency_code: "USD", value: "0.01" } } });
    assert.equal((await app.webhook(ev, { headers, body: edited })).status, 400);
    // No PayPal headers: refused without asking PayPal.
    const asked = app.fake.state.verifications.length;
    assert.equal((await app.webhook(ev, { headers: {} })).status, 400);
    assert.equal(app.fake.state.verifications.length, asked);
    assert.equal(app.store.getOrder(out.order_id).status, "pending");
    // The verify call carried this app's webhook id and the transmission headers.
    const v = app.fake.state.verifications[0];
    assert.equal(v.webhook_id, app.creds.webhookId);
    assert.equal(v.auth_algo, "SHA256withRSA");
  } finally {
    await app.close();
  }
});

test("a verified PAYMENT.CAPTURE.COMPLETED completes a pending capture once; a replay is ignored", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    app.fake.approve(out.paypal_order_id);
    app.fake.state.pendingNext = true; // e.g. an eCheck: PayPal captures later
    assert.equal((await app.post(`/api/paypal/orders/${out.paypal_order_id}/capture`)).body.status, "pending");
    const ev = captureEvent("PAYMENT.CAPTURE.COMPLETED", out.paypal_order_id, out.order_id, { currency_code: "USD", value: "30.00" });
    const first = await app.webhook(ev);
    assert.equal(first.status, 200);
    assert.equal(first.body.duplicate, false);
    assert.equal(app.store.getOrder(out.order_id).status, "paid");
    assert.equal((await app.webhook(ev)).body.duplicate, true);
    assert.deepEqual(app.paid, [out.order_id]);
  } finally {
    await app.close();
  }
});

test("a verified event for a capture of an order this app did not create fulfils nothing", async () => {
  const app = await boot();
  try {
    const out = await app.create();
    const wrongOrder = captureEvent("PAYMENT.CAPTURE.COMPLETED", "9XY12345AB6789012", out.order_id, { currency_code: "USD", value: "30.00" });
    assert.equal((await app.webhook(wrongOrder)).status, 200);
    const otherCustom = captureEvent("PAYMENT.CAPTURE.COMPLETED", out.paypal_order_id, crypto.randomUUID(), { currency_code: "USD", value: "30.00" });
    assert.equal((await app.webhook(otherCustom)).status, 200);
    assert.equal(app.store.getOrder(out.order_id).status, "pending");
  } finally {
    await app.close();
  }
});

// ---- the access token ----------------------------------------------------------------------------

test("the access token is cached, refreshed before it expires, and refreshed once on a 401", async () => {
  let now = 1_000_000;
  const app = await boot({ clock: () => now });
  try {
    await app.create();
    await app.create();
    assert.equal(app.fake.state.tokenRequests, 1, "one token for many calls");
    now += (32400 - 30) * 1000; // inside the last minute of the token's life
    await app.create();
    assert.equal(app.fake.state.tokenRequests, 2, "refreshed early");
    app.fake.revokeTokens();
    await app.create();
    assert.equal(app.fake.state.tokenRequests, 3, "refreshed once after a 401");
  } finally {
    await app.close();
  }
});

// ---- refunds (optional) and outages --------------------------------------------------------------

test("refunds are off by default, need an admin when on, and refund the recorded capture amount", async () => {
  const off = await boot();
  try {
    assert.equal((await off.post(`/api/orders/${crypto.randomUUID()}/refund`)).status, 404);
  } finally {
    await off.close();
  }
  const app = await boot({ env: { PAYMENTS_REFUNDS_ENABLED: "true" }, isAdmin: (req) => req.get("x-test-admin") === "yes" });
  try {
    const out = await app.create();
    const admin = { "x-test-admin": "yes" };
    assert.equal((await app.post(`/api/orders/${out.order_id}/refund`, {}, admin)).status, 409);
    app.fake.approve(out.paypal_order_id);
    await app.post(`/api/paypal/orders/${out.paypal_order_id}/capture`);
    assert.equal((await app.post(`/api/orders/${out.order_id}/refund`, { amount: "9999.00" })).status, 403);
    const r = await app.post(`/api/orders/${out.order_id}/refund`, { amount: "9999.00" }, admin);
    assert.equal(r.status, 200);
    assert.equal(r.body.status, "refunded");
    const refund = app.fake.state.refunds.at(-1);
    assert.deepEqual(refund.amount, { currency_code: "USD", value: "30.00" });
    assert.equal(refund.capture_id, app.store.getOrder(out.order_id).payment_ref);
    assert.equal(refund.request_id, `refund-${out.order_id}`);
  } finally {
    await app.close();
  }
});

test("when PayPal is unavailable creating an order answers 502 and the order is cancelled", async () => {
  const app = await boot();
  app.fake.state.down = true;
  try {
    const r = await app.post("/api/paypal/orders", { items: [{ product_id: "starter-pack", quantity: 1 }] });
    assert.equal(r.status, 502);
    const orders = [...app.store.orders.values()];
    assert.equal(orders.length, 1);
    assert.equal(orders[0].status, "cancelled");
  } finally {
    app.fake.state.down = false;
    await app.close();
  }
});
