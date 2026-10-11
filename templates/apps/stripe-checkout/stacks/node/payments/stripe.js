// Stripe Checkout for an Express app (template `stripe-checkout`).
//
//   POST /api/checkout              body {cart_id} or {items:[{product_id, quantity}]}
//                                   -> {order_id, session_id, checkout_url}; prices from the server
//   GET  /checkout/success          ?session_id=... retrieves the session FROM STRIPE and fulfils
//   GET  /checkout/cancel           the buyer came back without paying
//   POST /webhooks/stripe           signed (Stripe-Signature), idempotent by event id
//   GET  /api/orders/:id            the order's status (the UI polls it)
//   POST /api/orders/:id/refund     optional; PAYMENTS_REFUNDS_ENABLED=true AND isAdmin(req)
//   GET  /api/checkout/config       publishable key + mode (for the UI's test-mode banner)
//
// Fulfilment has two paths and both are idempotent: the success page (works on a private
// preview, where Stripe cannot reach the webhook) and the webhook (the authoritative one, which
// also covers a buyer who closes the tab before the redirect).
const express = require("express");
const { priceLineItems, CheckoutInputError } = require("./catalog");
const { TransitionError } = require("./store");

function createStripeCheckout({ stripe, config, store, catalog, isAdmin = () => false, onOrderPaid = () => {}, log = console }) {
  const router = express.Router();

  function baseUrl(req) {
    return config.baseUrl || `${req.protocol}://${req.get("host")}`;
  }

  // Fulfil an order from a Checkout Session object (from Stripe's API or a verified webhook).
  // Returns the order. Never trusts the session blindly: it must be the session this app created
  // for that order, for the amount and currency the server priced.
  function fulfilFromSession(session) {
    const orderId = session && session.metadata && session.metadata.order_id;
    const order = orderId ? store.getOrder(orderId) : null;
    if (!order || order.provider !== "stripe" || order.provider_ref !== session.id) return null;
    if (session.payment_status !== "paid") return order;
    if (session.amount_total !== order.amount_cents || String(session.currency).toLowerCase() !== order.currency) {
      // Paid, but not what we priced: do not ship; a person looks at it.
      if (safeTransition(order, "needs_review")) log.warn(`stripe: order ${order.id} amount mismatch; needs review`);
      return store.getOrder(order.id);
    }
    if (order.status === "paid" || order.status === "refunded" || order.status === "needs_review") return order;
    if (session.payment_intent) store.setPaymentRef(order.id, String(session.payment_intent));
    if (order.status === "cancelled") {
      // Money arrived for an order we had given up on: hold it for a person, never ship silently.
      safeTransition(order, "needs_review");
      return store.getOrder(order.id);
    }
    if (safeTransition(order, "paid")) onOrderPaid(store.getOrder(order.id));
    return store.getOrder(order.id);
  }

  function orderFromPaymentIntent(pi) {
    const orderId = pi && pi.metadata && pi.metadata.order_id;
    const order = orderId ? store.getOrder(orderId) : null;
    return order && order.provider === "stripe" ? order : null;
  }

  function safeTransition(order, to) {
    if (!order) return false;
    try {
      return store.transition(order.id, to);
    } catch (err) {
      if (err instanceof TransitionError) return false; // e.g. a late "failed" after "paid": ignore
      throw err;
    }
  }

  // ---- webhook: raw body, BEFORE any JSON parser touches it ------------------------------------
  router.post("/webhooks/stripe", express.raw({ type: "*/*", limit: "1mb" }), (req, res) => {
    const signature = req.get("stripe-signature");
    let event;
    try {
      if (!signature || !Buffer.isBuffer(req.body)) throw new Error("missing signature or body");
      event = stripe.webhooks.constructEvent(req.body, signature, config.webhookSecret, config.webhookToleranceSeconds);
    } catch (err) {
      // Bad signature, stale timestamp or malformed payload: refuse, and do not say which.
      return res.status(400).json({ error: "invalid signature" });
    }
    try {
      const { duplicate } = store.processEventOnce(event.id, "stripe", event.type, () => {
        const obj = event.data && event.data.object;
        switch (event.type) {
          case "checkout.session.completed":
          case "checkout.session.async_payment_succeeded":
            return fulfilFromSession(obj);
          case "checkout.session.async_payment_failed":
            return safeTransition(orderOfSession(obj), "failed");
          case "checkout.session.expired":
            return safeTransition(orderOfSession(obj), "cancelled");
          case "payment_intent.payment_failed":
            return safeTransition(orderFromPaymentIntent(obj), "failed");
          case "charge.refunded":
            // Only a FULL refund marks the order refunded; a partial one stays paid.
            return obj && obj.refunded && obj.payment_intent
              ? safeTransition(store.findByPaymentRef("stripe", String(obj.payment_intent)), "refunded")
              : null;
          default:
            return null; // acknowledged, not used
        }
      });
      return res.status(200).json({ received: true, duplicate });
    } catch (err) {
      log.error(`stripe: webhook ${event.type} failed: ${err.message}`);
      return res.status(500).json({ error: "processing failed" }); // Stripe retries
    }
  });

  function orderOfSession(session) {
    const orderId = session && session.metadata && session.metadata.order_id;
    const order = orderId ? store.getOrder(orderId) : null;
    return order && order.provider === "stripe" && order.provider_ref === session.id ? order : null;
  }

  router.use("/api", express.json({ limit: "64kb" }));

  router.get("/api/checkout/config", (req, res) => {
    res.json({ mode: config.mode, publishable_key: config.publishableKey || null });
  });

  router.post("/api/checkout", async (req, res) => {
    let priced;
    try {
      priced = await priceLineItems(catalog, req.body || {}, req);
    } catch (err) {
      if (err instanceof CheckoutInputError) return res.status(400).json({ error: err.message });
      throw err;
    }
    const order = store.createOrder({
      provider: "stripe",
      items: priced.lines,
      amountCents: priced.amountCents,
      currency: priced.currency,
      cartId: req.body && req.body.cart_id != null ? String(req.body.cart_id) : null,
    });
    const base = baseUrl(req);
    try {
      const session = await stripe.checkout.sessions.create(
        {
          mode: "payment",
          line_items: priced.lines.map((l) => ({
            quantity: l.quantity,
            price_data: { currency: l.currency, unit_amount: l.unit_amount_cents, product_data: { name: l.name } },
          })),
          client_reference_id: order.id,
          metadata: { order_id: order.id },
          payment_intent_data: { metadata: { order_id: order.id } },
          success_url: `${base}/checkout/success?session_id={CHECKOUT_SESSION_ID}`,
          cancel_url: `${base}/checkout/cancel?order_id=${order.id}`,
        },
        { idempotencyKey: `checkout-${order.id}` },
      );
      store.setProviderRef(order.id, session.id);
      return res.status(201).json({ order_id: order.id, session_id: session.id, checkout_url: session.url });
    } catch (err) {
      safeTransition(order, "cancelled");
      log.error(`stripe: could not create a Checkout Session: ${err.message}`);
      return res.status(502).json({ error: "the payment provider is unavailable; try again" });
    }
  });

  router.get("/checkout/success", async (req, res) => {
    const sessionId = String(req.query.session_id || "");
    if (!/^cs_[A-Za-z0-9_]+$/.test(sessionId)) return reply(res, req, 400, { error: "missing session" });
    let session;
    try {
      session = await stripe.checkout.sessions.retrieve(sessionId);
    } catch (err) {
      return reply(res, req, 404, { error: "unknown session" });
    }
    const order = fulfilFromSession(session);
    if (!order) return reply(res, req, 404, { error: "unknown order" });
    return reply(res, req, 200, { order_id: order.id, status: order.status });
  });

  router.get("/checkout/cancel", (req, res) => {
    const order = store.getOrder(String(req.query.order_id || ""));
    return reply(res, req, 200, { order_id: order ? order.id : null, status: order ? order.status : null, cancelled: true });
  });

  router.get("/api/orders/:id", (req, res) => {
    const order = store.getOrder(req.params.id);
    if (!order || order.provider !== "stripe") return res.status(404).json({ error: "not found" });
    const { id, status, amount_cents, currency, items, created_at, paid_at, refunded_at } = order;
    res.json({ id, status, amount_cents, currency, items, created_at, paid_at, refunded_at });
  });

  router.post("/api/orders/:id/refund", async (req, res) => {
    if (!config.refundsEnabled) return res.status(404).json({ error: "refunds are not enabled" });
    if (!(await isAdmin(req))) return res.status(403).json({ error: "forbidden" });
    const order = store.getOrder(req.params.id);
    if (!order || order.provider !== "stripe") return res.status(404).json({ error: "not found" });
    if (order.status !== "paid") return res.status(409).json({ error: `order is ${order.status}` });
    if (!order.payment_ref) return res.status(409).json({ error: "order has no recorded payment" });
    try {
      // Full refund of the server-recorded amount; the client never names an amount.
      await stripe.refunds.create(
        { payment_intent: order.payment_ref, amount: order.amount_cents },
        { idempotencyKey: `refund-${order.id}` },
      );
    } catch (err) {
      log.error(`stripe: refund failed: ${err.message}`);
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

// JSON for API clients, a small page for a browser coming back from Checkout.
function reply(res, req, status, body) {
  if (req.accepts(["json", "html"]) === "html") {
    const title = body.error ? "Payment problem" : body.cancelled ? "Payment cancelled" : body.status === "paid" ? "Thank you" : "Payment processing";
    const detail = body.error ? body.error : body.cancelled ? "You have not been charged." : body.status === "paid" ? "Your order is confirmed." : "We are confirming your payment; this page updates when it is done.";
    return res
      .status(status)
      .type("html")
      .send(`<!doctype html><meta charset="utf-8"><title>${escapeHtml(title)}</title><main><h1>${escapeHtml(title)}</h1><p>${escapeHtml(detail)}</p>${body.order_id ? `<p>Order <code>${escapeHtml(body.order_id)}</code></p>` : ""}<p><a href="/">Back to the shop</a></p></main>`);
  }
  return res.status(status).json(body);
}

module.exports = { createStripeCheckout };
