// The `stripe-checkout` template's own tests, against its HTTP routes (not internals), with Stripe
// replaced by an in-process fake. No test reaches the network or uses a real key.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { startFakeStripe, signStripePayload, newWebhookSecret, newTestKeys } from "./fake_stripe.mjs";

const require = createRequire(import.meta.url);
const Stripe = require("stripe");
const { createApp } = require("../../server.js");
const { loadStripeConfig } = require("../../payments/config.js");
const { staticCatalog, DEFAULT_PRODUCTS } = require("../../payments/catalog.js");

let fake;
before(async () => {
  fake = await startFakeStripe();
});
after(async () => {
  await fake.close();
});

// One app per test: its own store, keys and webhook signing secret.
async function boot({ env = {}, catalog, isAdmin } = {}) {
  const keys = newTestKeys();
  const webhookSecret = newWebhookSecret();
  const fullEnv = {
    STRIPE_SECRET_KEY: keys.secretKey,
    STRIPE_PUBLISHABLE_KEY: keys.publishableKey,
    STRIPE_WEBHOOK_SECRET: webhookSecret,
    APP_BASE_URL: "http://shop.test",
    ...env,
  };
  const stripe = new Stripe(fullEnv.STRIPE_SECRET_KEY, {
    host: "127.0.0.1", port: fake.port, protocol: "http", maxNetworkRetries: 0, telemetry: false,
  });
  const paid = [];
  const { app, store } = createApp({ env: fullEnv, stripe, catalog, isAdmin, onOrderPaid: (o) => paid.push(o.id) });
  const server = await new Promise((resolve) => {
    const s = app.listen(0, "127.0.0.1", () => resolve(s));
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const ctx = {
    store, paid, webhookSecret, base,
    async post(path, body, headers = {}) {
      const r = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
      return { status: r.status, body: await r.json() };
    },
    async get(path) {
      const r = await fetch(base + path, { headers: { Accept: "application/json" } });
      return { status: r.status, body: await r.json() };
    },
    async webhook(event, { secret = webhookSecret, timestamp, header } = {}) {
      const payload = JSON.stringify(event);
      const sig = header !== undefined ? header : signStripePayload(payload, secret, timestamp);
      const headers = { "Content-Type": "application/json" };
      if (sig !== null) headers["Stripe-Signature"] = sig;
      const r = await fetch(base + "/webhooks/stripe", { method: "POST", headers, body: payload });
      return { status: r.status, body: await r.json() };
    },
    async checkout(items = [{ product_id: "starter-pack", quantity: 2 }]) {
      const r = await ctx.post("/api/checkout", { items });
      assert.equal(r.status, 201, JSON.stringify(r.body));
      return r.body;
    },
    close: () => new Promise((r) => server.close(r)),
  };
  return ctx;
}

function sessionEvent(type, session, id = "evt_" + crypto.randomBytes(10).toString("hex")) {
  return { id, object: "event", type, created: Math.floor(Date.now() / 1000), data: { object: session } };
}

// ---- configuration: test mode by default ---------------------------------------------------------

test("a live key is refused in test mode, a test key in live mode, and the error never echoes the key", () => {
  const live = "sk_" + "live_" + crypto.randomBytes(16).toString("hex");
  assert.throws(() => loadStripeConfig({ STRIPE_SECRET_KEY: live, STRIPE_WEBHOOK_SECRET: newWebhookSecret() }), (err) => {
    assert.match(err.message, /not a test-mode secret key/);
    assert.ok(!err.message.includes(live));
    return true;
  });
  const { secretKey } = newTestKeys();
  assert.throws(() => loadStripeConfig({ STRIPE_MODE: "live", STRIPE_SECRET_KEY: secretKey, STRIPE_WEBHOOK_SECRET: "x" }), /not a live-mode secret key/);
  const livePub = "pk_" + "live_" + crypto.randomBytes(16).toString("hex");
  assert.throws(() => loadStripeConfig({ STRIPE_SECRET_KEY: secretKey, STRIPE_PUBLISHABLE_KEY: livePub, STRIPE_WEBHOOK_SECRET: "x" }), /publishable/);
  assert.throws(() => loadStripeConfig({ STRIPE_SECRET_KEY: secretKey }), /STRIPE_WEBHOOK_SECRET/);
  assert.equal(loadStripeConfig({ STRIPE_SECRET_KEY: secretKey, STRIPE_WEBHOOK_SECRET: "x" }).mode, "test");
});

test("the UI config route says test mode and never exposes the secret key", async () => {
  const app = await boot();
  try {
    const r = await app.get("/api/checkout/config");
    assert.equal(r.status, 200);
    assert.equal(r.body.mode, "test");
    assert.match(r.body.publishable_key, /^pk_test_/);
    assert.ok(!JSON.stringify(r.body).includes("sk_"));
  } finally {
    await app.close();
  }
});

// ---- prices come from the server -----------------------------------------------------------------

test("checkout creates a Checkout Session priced by the server", async () => {
  const app = await boot();
  try {
    const out = await app.checkout([{ product_id: "starter-pack", quantity: 2 }]);
    const session = fake.state.sessions.get(out.session_id);
    assert.deepEqual(session.line_items_sent, [{ quantity: 2, unit_amount: 1500, currency: "usd", name: "Starter pack" }]);
    assert.equal(session.amount_total, 3000);
    assert.equal(session.metadata.order_id, out.order_id);
    assert.equal(session.success_url, "http://shop.test/checkout/success?session_id={CHECKOUT_SESSION_ID}");
    assert.equal(out.checkout_url, session.url);
    const order = (await app.get(`/api/orders/${out.order_id}`)).body;
    assert.equal(order.status, "pending");
    assert.equal(order.amount_cents, 3000);
    const req = fake.state.requests.find((r) => r.path === "/v1/checkout/sessions" && r.method === "POST" && r.form.get("metadata[order_id]") === out.order_id);
    assert.equal(req.headers["idempotency-key"], `checkout-${out.order_id}`);
  } finally {
    await app.close();
  }
});

test("a tampered client price, amount or currency is ignored", async () => {
  const app = await boot();
  try {
    const r = await app.post("/api/checkout", {
      amount: 1, currency: "jpy",
      items: [{ product_id: "pro-pack", quantity: 1, price: 1, unit_amount: 1, unit_amount_cents: 1, amount: 1, currency: "jpy", name: "Free" }],
    });
    assert.equal(r.status, 201);
    const session = fake.state.sessions.get(r.body.session_id);
    assert.deepEqual(session.line_items_sent, [{ quantity: 1, unit_amount: 4900, currency: "usd", name: "Pro pack" }]);
    assert.equal(app.store.getOrder(r.body.order_id).amount_cents, 4900);
  } finally {
    await app.close();
  }
});

test("unknown products, empty orders and bad quantities are refused before Stripe is called", async () => {
  const app = await boot();
  try {
    const before = fake.state.sessions.size;
    for (const body of [
      { items: [{ product_id: "no-such-thing", quantity: 1 }] },
      { items: [] },
      {},
      { items: [{ product_id: "starter-pack", quantity: 0 }] },
      { items: [{ product_id: "starter-pack", quantity: 1.5 }] },
      { items: [{ product_id: "starter-pack", quantity: "2" }] },
      { items: [{ product_id: "starter-pack", quantity: 100 }] },
      { items: [{ product_id: "starter-pack", quantity: -1 }] },
    ]) {
      const r = await app.post("/api/checkout", body);
      assert.equal(r.status, 400, JSON.stringify(body));
    }
    assert.equal(fake.state.sessions.size, before);
  } finally {
    await app.close();
  }
});

test("checkout from a catalog-cart cart uses the cart's server prices, and a price change is reflected", async () => {
  const prices = { "tee-1": 2500 };
  const catalog = {
    ...staticCatalog(DEFAULT_PRODUCTS),
    cartLineItems(cartId) {
      if (cartId !== "cart-abc") return null;
      return [{ product_id: "tee-1", name: "T-shirt", unit_amount_cents: prices["tee-1"], currency: "usd", quantity: 3 }];
    },
  };
  const app = await boot({ catalog });
  try {
    const first = await app.post("/api/checkout", { cart_id: "cart-abc", total: 1 });
    assert.equal(first.status, 201);
    assert.equal(fake.state.sessions.get(first.body.session_id).amount_total, 7500);
    prices["tee-1"] = 2700;
    const second = await app.post("/api/checkout", { cart_id: "cart-abc" });
    assert.equal(fake.state.sessions.get(second.body.session_id).amount_total, 8100);
    assert.equal((await app.post("/api/checkout", { cart_id: "someone-elses" })).status, 400);
  } finally {
    await app.close();
  }
});

// ---- webhooks: signed, fresh, idempotent ---------------------------------------------------------

test("a webhook with a bad, forged or missing signature is refused with 400 and changes nothing", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const session = fake.pay(out.session_id);
    const event = sessionEvent("checkout.session.completed", session);
    assert.equal((await app.webhook(event, { secret: newWebhookSecret() })).status, 400); // forged: another secret
    assert.equal((await app.webhook(event, { header: "t=1,v1=deadbeef" })).status, 400);
    assert.equal((await app.webhook(event, { header: null })).status, 400);
    // A valid signature over a DIFFERENT body (the attacker edits the amount after signing).
    const payload = JSON.stringify(event);
    const sig = signStripePayload(payload, app.webhookSecret);
    const tampered = JSON.stringify({ ...event, data: { object: { ...session, amount_total: 1 } } });
    const r = await fetch(app.base + "/webhooks/stripe", { method: "POST", headers: { "Content-Type": "application/json", "Stripe-Signature": sig }, body: tampered });
    assert.equal(r.status, 400);
    assert.equal(app.store.getOrder(out.order_id).status, "pending");
    assert.deepEqual(app.paid, []);
  } finally {
    await app.close();
  }
});

test("a webhook signed more than five minutes ago is refused (replay window)", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const event = sessionEvent("checkout.session.completed", fake.pay(out.session_id));
    const stale = Math.floor(Date.now() / 1000) - 600;
    assert.equal((await app.webhook(event, { timestamp: stale })).status, 400);
    assert.equal(app.store.getOrder(out.order_id).status, "pending");
  } finally {
    await app.close();
  }
});

test("a signed checkout.session.completed fulfils the order once; a replayed event is processed once", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const event = sessionEvent("checkout.session.completed", fake.pay(out.session_id));
    const first = await app.webhook(event);
    assert.equal(first.status, 200);
    assert.equal(first.body.duplicate, false);
    assert.equal(app.store.getOrder(out.order_id).status, "paid");
    const again = await app.webhook(event);
    assert.equal(again.status, 200);
    assert.equal(again.body.duplicate, true);
    // Same object, a NEW event id (Stripe can send a second event for one session): still once.
    assert.equal((await app.webhook(sessionEvent("checkout.session.completed", event.data.object))).status, 200);
    assert.deepEqual(app.paid, [out.order_id]);
  } finally {
    await app.close();
  }
});

test("the success page fulfils from Stripe (private preview), idempotent with the webhook", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const pending = await app.get(`/checkout/success?session_id=${out.session_id}`);
    assert.equal(pending.status, 200);
    assert.equal(pending.body.status, "pending"); // not paid yet: no fulfilment
    const session = fake.pay(out.session_id);
    const r = await app.get(`/checkout/success?session_id=${out.session_id}`);
    assert.equal(r.status, 200);
    assert.equal(r.body.status, "paid");
    assert.equal((await app.get(`/checkout/success?session_id=${out.session_id}`)).body.status, "paid");
    assert.equal((await app.webhook(sessionEvent("checkout.session.completed", session))).status, 200);
    assert.deepEqual(app.paid, [out.order_id]);
    const page = await fetch(`${app.base}/checkout/success?session_id=${out.session_id}`, { headers: { Accept: "text/html" } });
    assert.match(await page.text(), /Thank you/);
  } finally {
    await app.close();
  }
});

test("the success page refuses a malformed or unknown session id", async () => {
  const app = await boot();
  try {
    assert.equal((await app.get("/checkout/success?session_id=../../v1/refunds")).status, 400);
    assert.equal((await app.get("/checkout/success")).status, 400);
    assert.equal((await app.get("/checkout/success?session_id=cs_test_nope")).status, 404);
  } finally {
    await app.close();
  }
});

test("a paid session for a different amount than the server priced is held for review, not fulfilled", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const session = fake.pay(out.session_id, { amount_total: 100 });
    assert.equal((await app.webhook(sessionEvent("checkout.session.completed", session))).status, 200);
    assert.equal(app.store.getOrder(out.order_id).status, "needs_review");
    assert.deepEqual(app.paid, []);
  } finally {
    await app.close();
  }
});

test("a signed event naming an order with a session this app did not create for it fulfils nothing", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const other = { id: "cs_test_" + crypto.randomBytes(8).toString("hex"), object: "checkout.session",
      payment_status: "paid", amount_total: 3000, currency: "usd", metadata: { order_id: out.order_id } };
    assert.equal((await app.webhook(sessionEvent("checkout.session.completed", other))).status, 200);
    assert.equal(app.store.getOrder(out.order_id).status, "pending");
  } finally {
    await app.close();
  }
});

test("order state machine: failed can still be paid, paid is never failed again, expired cancels, paid-after-cancel is held", async () => {
  const app = await boot();
  try {
    const out = await app.checkout();
    const pi = { id: fake.state.sessions.get(out.session_id).payment_intent, object: "payment_intent", metadata: { order_id: out.order_id } };
    await app.webhook(sessionEvent("payment_intent.payment_failed", pi));
    assert.equal(app.store.getOrder(out.order_id).status, "failed");
    await app.webhook(sessionEvent("checkout.session.completed", fake.pay(out.session_id)));
    assert.equal(app.store.getOrder(out.order_id).status, "paid");
    const late = await app.webhook(sessionEvent("payment_intent.payment_failed", pi));
    assert.equal(late.status, 200);
    assert.equal(app.store.getOrder(out.order_id).status, "paid");

    const second = await app.checkout();
    await app.webhook(sessionEvent("checkout.session.expired", fake.state.sessions.get(second.session_id)));
    assert.equal(app.store.getOrder(second.order_id).status, "cancelled");
    // Paid after it was cancelled: held for a person, never shipped silently.
    await app.webhook(sessionEvent("checkout.session.completed", fake.pay(second.session_id)));
    assert.equal(app.store.getOrder(second.order_id).status, "needs_review");
    assert.deepEqual(app.paid, [out.order_id]);
  } finally {
    await app.close();
  }
});

// ---- refunds (optional) --------------------------------------------------------------------------

test("refunds are off by default, need an admin when on, and refund the server-recorded amount", async () => {
  const off = await boot();
  try {
    assert.equal((await off.post(`/api/orders/${crypto.randomUUID()}/refund`, {})).status, 404);
  } finally {
    await off.close();
  }
  const app = await boot({ env: { PAYMENTS_REFUNDS_ENABLED: "true" }, isAdmin: (req) => req.get("x-test-admin") === "yes" });
  try {
    const out = await app.checkout();
    assert.equal((await app.post(`/api/orders/${out.order_id}/refund`, {}, { "x-test-admin": "yes" })).status, 409); // not paid
    const session = fake.pay(out.session_id);
    await app.webhook(sessionEvent("checkout.session.completed", session));
    assert.equal((await app.post(`/api/orders/${out.order_id}/refund`, { amount: 999999 })).status, 403);
    const r = await app.post(`/api/orders/${out.order_id}/refund`, { amount: 999999 }, { "x-test-admin": "yes" });
    assert.equal(r.status, 200);
    assert.equal(r.body.status, "refunded");
    const refund = fake.state.refunds.at(-1);
    assert.equal(refund.amount, 3000);
    assert.equal(refund.payment_intent, session.payment_intent);
    assert.equal(refund.idempotency_key, `refund-${out.order_id}`);
    // Stripe's charge.refunded webhook afterwards is a no-op.
    const charge = { id: "ch_1", object: "charge", refunded: true, payment_intent: session.payment_intent, metadata: {} };
    assert.equal((await app.webhook(sessionEvent("charge.refunded", charge))).status, 200);
    assert.equal(app.store.getOrder(out.order_id).status, "refunded");
  } finally {
    await app.close();
  }
});

test("when Stripe is unavailable checkout answers 502 and the order is cancelled, not left pending", async () => {
  const app = await boot();
  fake.state.down = true;
  try {
    const r = await app.post("/api/checkout", { items: [{ product_id: "starter-pack", quantity: 1 }] });
    assert.equal(r.status, 502);
    const orders = [...app.store.orders.values()];
    assert.equal(orders.length, 1);
    assert.equal(orders[0].status, "cancelled");
  } finally {
    fake.state.down = false;
    await app.close();
  }
});
