// PayPal Checkout (Orders v2) for an Express app (template `paypal-checkout`).
//
//   POST /api/paypal/orders              body {cart_id} or {items:[{product_id, quantity}]}
//                                        -> {order_id, paypal_order_id, approve_url}; server prices
//   POST /api/paypal/orders/:id/capture  :id = the PayPal order id from the JS SDK's onApprove;
//                                        captures only an order THIS app created, once
//   GET  /paypal/return                  ?token=<PayPal order id>: the redirect flow's return page;
//                                        captures server-side (works on a private preview)
//   GET  /paypal/cancel                  the buyer came back without paying
//   POST /webhooks/paypal                verified with PayPal's verify-webhook-signature API,
//                                        idempotent by event id
//   GET  /api/orders/:id                 the order's status
//   POST /api/orders/:id/refund          optional; PAYMENTS_REFUNDS_ENABLED=true AND isAdmin(req)
//   GET  /api/paypal/config              client id + env + currency for the JS SDK (never the secret)
//
// Fulfilment has two idempotent paths: the capture (from the buttons or the return page) and the
// PAYMENT.CAPTURE.COMPLETED webhook (which also completes a capture PayPal reported as PENDING).
const express = require("express");
const { priceLineItems, CheckoutInputError } = require("./catalog");
const { TransitionError } = require("./store");
const { PayPalError } = require("./paypal_client");

// Currencies PayPal takes without decimals.
const ZERO_DECIMAL = new Set(["jpy", "huf", "twd"]);
const PAYPAL_ORDER_ID = /^[A-Z0-9]{10,40}$/;

// Integer minor units -> PayPal's decimal string, without floating point.
function toValue(minor, currency) {
  if (ZERO_DECIMAL.has(currency)) return String(minor);
  return `${Math.floor(minor / 100)}.${String(minor % 100).padStart(2, "0")}`;
}

// PayPal's decimal string -> integer minor units, or null if it is not a plain amount.
function fromValue(value, currency) {
  const s = String(value || "");
  if (ZERO_DECIMAL.has(currency)) return /^\d+$/.test(s) ? Number(s) : null;
  const m = s.match(/^(\d+)(?:\.(\d{1,2}))?$/);
  return m ? Number(m[1]) * 100 + Number((m[2] || "0").padEnd(2, "0")) : null;
}

function money(minor, currency) {
  return { currency_code: currency.toUpperCase(), value: toValue(minor, currency) };
}

function createPayPalCheckout({ paypal, config, store, catalog, isAdmin = () => false, onOrderPaid = () => {}, log = console }) {
  const router = express.Router();

  function baseUrl(req) {
    return config.baseUrl || `${req.protocol}://${req.get("host")}`;
  }

  function safeTransition(order, to) {
    if (!order) return false;
    try {
      return store.transition(order.id, to);
    } catch (err) {
      if (err instanceof TransitionError) return false; // e.g. a late "denied" after "paid"
      throw err;
    }
  }

  // Record a capture PayPal reported. Pays the order only for a COMPLETED capture of exactly the
  // amount and currency the server priced; anything else is left pending or held for review.
  function applyCapture(order, capture) {
    if (!order || !capture) return order;
    const amount = capture.amount || {};
    const currency = String(amount.currency_code || "").toLowerCase();
    if (capture.status === "DECLINED" || capture.status === "FAILED") {
      safeTransition(order, "failed");
      return store.getOrder(order.id);
    }
    if (capture.status !== "COMPLETED") return store.getOrder(order.id); // PENDING: the webhook finishes it
    if (currency !== order.currency || fromValue(amount.value, currency) !== order.amount_cents) {
      if (safeTransition(order, "needs_review")) log.warn(`paypal: order ${order.id} amount mismatch; needs review`);
      return store.getOrder(order.id);
    }
    const current = store.getOrder(order.id);
    if (["paid", "refunded", "needs_review"].includes(current.status)) return current;
    if (capture.id) store.setPaymentRef(order.id, String(capture.id));
    if (current.status === "cancelled") {
      safeTransition(current, "needs_review"); // money for an order we gave up on: a person decides
      return store.getOrder(order.id);
    }
    if (safeTransition(current, "paid")) onOrderPaid(store.getOrder(order.id));
    return store.getOrder(order.id);
  }

  function firstCapture(paypalOrder) {
    const units = (paypalOrder && paypalOrder.purchase_units) || [];
    const captures = (units[0] && units[0].payments && units[0].payments.captures) || [];
    return captures[0] || null;
  }

  // Capture our order's PayPal order once. Already paid: no second call to PayPal.
  async function capture(order) {
    if (order.status !== "pending" && order.status !== "failed") return { order };
    try {
      const result = await paypal.captureOrder(order.provider_ref, `capture-${order.id}`);
      return { order: applyCapture(order, firstCapture(result)) };
    } catch (err) {
      if (!(err instanceof PayPalError)) throw err;
      const issues = err.issues();
      if (issues.includes("ORDER_ALREADY_CAPTURED")) {
        // Captured elsewhere (another tab, the return page): read the truth back from PayPal.
        const current = await paypal.getOrder(order.provider_ref);
        return { order: applyCapture(order, firstCapture(current)) };
      }
      if (issues.includes("INSTRUMENT_DECLINED")) {
        // PayPal's guidance: let the buyer choose another funding source; the order stays open.
        return { order: store.getOrder(order.id), declined: true };
      }
      throw err;
    }
  }

  // ---- webhook: verified by PayPal before anything acts on it ---------------------------------
  router.post("/webhooks/paypal", express.raw({ type: "*/*", limit: "1mb" }), async (req, res) => {
    const needed = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time"];
    let event;
    try {
      if (!config.webhookId || needed.some((h) => !req.get(h)) || !Buffer.isBuffer(req.body)) throw new Error("unsigned");
      event = JSON.parse(req.body.toString("utf8"));
      const headers = Object.fromEntries(needed.map((h) => [h, req.get(h)]));
      const ok = await paypal.verifyWebhook({ headers, event, webhookId: config.webhookId });
      if (!ok) throw new Error("verification failed");
    } catch (err) {
      return res.status(400).json({ error: "invalid signature" }); // do not say which check failed
    }
    try {
      const { duplicate } = store.processEventOnce(String(event.id), "paypal", String(event.event_type), () => {
        const r = event.resource || {};
        const related = ((r.supplementary_data || {}).related_ids || {}).order_id;
        // The capture must belong to a PayPal order THIS app created, for the order it names.
        const order = related ? store.findByProviderRef("paypal", String(related)) : null;
        const owned = order && (!r.custom_id || r.custom_id === order.id) ? order : null;
        switch (event.event_type) {
          case "PAYMENT.CAPTURE.COMPLETED":
            return applyCapture(owned, { ...r, status: "COMPLETED" });
          case "PAYMENT.CAPTURE.DENIED":
          case "PAYMENT.CAPTURE.DECLINED":
            return safeTransition(owned, "failed");
          case "PAYMENT.CAPTURE.REFUNDED": {
            // The refund's "up" link names the capture it refunds.
            const up = (r.links || []).find((l) => l.rel === "up");
            const captureId = up && String(up.href).split("/captures/")[1];
            return captureId ? safeTransition(store.findByPaymentRef("paypal", captureId.split(/[/?]/)[0]), "refunded") : null;
          }
          default:
            return null; // acknowledged, not used
        }
      });
      return res.status(200).json({ received: true, duplicate });
    } catch (err) {
      log.error(`paypal: webhook ${event.event_type} failed: ${err.message}`);
      return res.status(500).json({ error: "processing failed" }); // PayPal retries
    }
  });

  router.use("/api", express.json({ limit: "64kb" }));

  router.get("/api/paypal/config", (req, res) => {
    res.json({ env: config.env, client_id: config.clientId, currency: config.currency.toUpperCase() });
  });

  router.post("/api/paypal/orders", async (req, res) => {
    let priced;
    try {
      priced = await priceLineItems(catalog, req.body || {}, req);
    } catch (err) {
      if (err instanceof CheckoutInputError) return res.status(400).json({ error: err.message });
      throw err;
    }
    const order = store.createOrder({
      provider: "paypal",
      items: priced.lines,
      amountCents: priced.amountCents,
      currency: priced.currency,
      cartId: req.body && req.body.cart_id != null ? String(req.body.cart_id) : null,
    });
    const base = baseUrl(req);
    try {
      const created = await paypal.createOrder(
        {
          intent: "CAPTURE",
          purchase_units: [{
            reference_id: order.id,
            custom_id: order.id,
            amount: { ...money(priced.amountCents, priced.currency), breakdown: { item_total: money(priced.amountCents, priced.currency) } },
            items: priced.lines.map((l) => ({ name: l.name.slice(0, 127), sku: l.product_id.slice(0, 127), quantity: String(l.quantity), unit_amount: money(l.unit_amount_cents, l.currency) })),
          }],
          payment_source: {
            paypal: {
              experience_context: {
                return_url: `${base}/paypal/return`,
                cancel_url: `${base}/paypal/cancel`,
                user_action: "PAY_NOW",
                shipping_preference: "NO_SHIPPING",
              },
            },
          },
        },
        `create-${order.id}`,
      );
      store.setProviderRef(order.id, String(created.id));
      const approve = (created.links || []).find((l) => l.rel === "payer-action" || l.rel === "approve");
      return res.status(201).json({ order_id: order.id, paypal_order_id: created.id, approve_url: approve ? approve.href : null });
    } catch (err) {
      safeTransition(order, "cancelled");
      log.error(`paypal: could not create an order: ${err.message}`);
      return res.status(502).json({ error: "the payment provider is unavailable; try again" });
    }
  });

  async function captureByPayPalId(paypalOrderId) {
    if (!PAYPAL_ORDER_ID.test(paypalOrderId)) return { status: 400, body: { error: "bad order id" } };
    const order = store.findByProviderRef("paypal", paypalOrderId);
    if (!order) return { status: 404, body: { error: "unknown order" } }; // never capture someone else's order
    try {
      const out = await capture(order);
      if (out.declined) return { status: 402, body: { order_id: order.id, status: out.order.status, error: "the payment was declined", restart: true } };
      return { status: 200, body: { order_id: order.id, status: out.order.status } };
    } catch (err) {
      log.error(`paypal: capture failed: ${err.message}`);
      return { status: 502, body: { error: "the payment provider is unavailable; try again" } };
    }
  }

  router.post("/api/paypal/orders/:id/capture", async (req, res) => {
    const { status, body } = await captureByPayPalId(String(req.params.id));
    res.status(status).json(body);
  });

  router.get("/paypal/return", async (req, res) => {
    const { status, body } = await captureByPayPalId(String(req.query.token || ""));
    return reply(res, req, status, body);
  });

  router.get("/paypal/cancel", (req, res) => {
    const order = store.findByProviderRef("paypal", String(req.query.token || ""));
    return reply(res, req, 200, { order_id: order ? order.id : null, status: order ? order.status : null, cancelled: true });
  });

  router.get("/api/orders/:id", (req, res) => {
    const order = store.getOrder(req.params.id);
    if (!order || order.provider !== "paypal") return res.status(404).json({ error: "not found" });
    const { id, status, amount_cents, currency, items, created_at, paid_at, refunded_at } = order;
    res.json({ id, status, amount_cents, currency, items, created_at, paid_at, refunded_at });
  });

  router.post("/api/orders/:id/refund", async (req, res) => {
    if (!config.refundsEnabled) return res.status(404).json({ error: "refunds are not enabled" });
    if (!(await isAdmin(req))) return res.status(403).json({ error: "forbidden" });
    const order = store.getOrder(req.params.id);
    if (!order || order.provider !== "paypal") return res.status(404).json({ error: "not found" });
    if (order.status !== "paid") return res.status(409).json({ error: `order is ${order.status}` });
    if (!order.payment_ref) return res.status(409).json({ error: "order has no recorded payment" });
    try {
      // Full refund of the server-recorded amount; the client never names an amount.
      await paypal.refundCapture(order.payment_ref, money(order.amount_cents, order.currency), `refund-${order.id}`);
    } catch (err) {
      log.error(`paypal: refund failed: ${err.message}`);
      return res.status(502).json({ error: "the payment provider refused the refund" });
    }
    safeTransition(order, "refunded");
    return res.json({ id: order.id, status: store.getOrder(order.id).status });
  });

  return router;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function reply(res, req, status, body) {
  if (req.accepts(["json", "html"]) === "html") {
    const title = body.error ? "Payment problem" : body.cancelled ? "Payment cancelled" : body.status === "paid" ? "Thank you" : "Payment processing";
    const detail = body.error ? body.error : body.cancelled ? "You have not been charged." : body.status === "paid" ? "Your order is confirmed." : "We are confirming your payment.";
    return res
      .status(status)
      .type("html")
      .send(`<!doctype html><meta charset="utf-8"><title>${escapeHtml(title)}</title><main><h1>${escapeHtml(title)}</h1><p>${escapeHtml(detail)}</p>${body.order_id ? `<p>Order <code>${escapeHtml(body.order_id)}</code></p>` : ""}<p><a href="/">Back to the shop</a></p></main>`);
  }
  return res.status(status).json(body);
}

module.exports = { createPayPalCheckout, toValue, fromValue };
