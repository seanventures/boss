// An in-process fake of the parts of the Stripe API the template calls. The real `stripe` SDK
// talks to it (host/port/protocol overrides), so requests are encoded exactly as in production
// and no test ever reaches api.stripe.com.
import http from "node:http";
import crypto from "node:crypto";

export async function startFakeStripe() {
  const state = { sessions: new Map(), refunds: [], requests: [], idempotency: new Map(), down: false };

  function json(res, status, body) {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  }

  function lineItems(form) {
    const items = [];
    for (let i = 0; form.has(`line_items[${i}][quantity]`); i++) {
      items.push({
        quantity: Number(form.get(`line_items[${i}][quantity]`)),
        unit_amount: Number(form.get(`line_items[${i}][price_data][unit_amount]`)),
        currency: form.get(`line_items[${i}][price_data][currency]`),
        name: form.get(`line_items[${i}][price_data][product_data][name]`),
      });
    }
    return items;
  }

  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const form = new URLSearchParams(raw);
      state.requests.push({ method: req.method, path: req.url, form, headers: req.headers });
      if (state.down) return json(res, 500, { error: { type: "api_error", message: "down" } });
      const auth = req.headers.authorization || "";
      if (!auth.startsWith("Bearer ")) return json(res, 401, { error: { type: "invalid_request_error" } });
      const key = req.headers["idempotency-key"];
      if (req.method === "POST" && req.url === "/v1/checkout/sessions") {
        if (key && state.idempotency.has(key)) return json(res, 200, state.idempotency.get(key));
        const items = lineItems(form);
        const id = "cs_test_" + crypto.randomBytes(12).toString("hex");
        const session = {
          id,
          object: "checkout.session",
          mode: form.get("mode"),
          url: `https://checkout.stripe.test/c/pay/${id}`,
          amount_total: items.reduce((s, i) => s + i.unit_amount * i.quantity, 0),
          currency: items[0] && items[0].currency,
          client_reference_id: form.get("client_reference_id"),
          metadata: { order_id: form.get("metadata[order_id]") },
          payment_status: "unpaid",
          status: "open",
          payment_intent: "pi_" + crypto.randomBytes(12).toString("hex"),
          success_url: form.get("success_url"),
          cancel_url: form.get("cancel_url"),
          line_items_sent: items,
        };
        state.sessions.set(id, session);
        if (key) state.idempotency.set(key, session);
        return json(res, 200, session);
      }
      const m = req.url.match(/^\/v1\/checkout\/sessions\/([A-Za-z0-9_]+)/);
      if (req.method === "GET" && m) {
        const s = state.sessions.get(m[1]);
        return s ? json(res, 200, s) : json(res, 404, { error: { type: "invalid_request_error", message: "No such session" } });
      }
      if (req.method === "POST" && req.url === "/v1/refunds") {
        const refund = { id: "re_" + crypto.randomBytes(8).toString("hex"), object: "refund", status: "succeeded",
          payment_intent: form.get("payment_intent"), amount: Number(form.get("amount")), idempotency_key: key };
        state.refunds.push(refund);
        return json(res, 200, refund);
      }
      return json(res, 404, { error: { type: "invalid_request_error", message: "unknown route" } });
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return {
    state,
    port: server.address().port,
    pay(sessionId, overrides = {}) {
      const s = state.sessions.get(sessionId);
      Object.assign(s, { payment_status: "paid", status: "complete" }, overrides);
      return s;
    },
    close: () => new Promise((r) => server.close(r)),
  };
}

// A Stripe-Signature header computed the documented way: v1 = HMAC-SHA256(secret, "<t>.<payload>").
export function signStripePayload(payload, secret, timestamp = Math.floor(Date.now() / 1000)) {
  const v1 = crypto.createHmac("sha256", secret).update(`${timestamp}.${payload}`, "utf8").digest("hex");
  return `t=${timestamp},v1=${v1}`;
}

// A fresh signing secret per test run, in Stripe's shape. Never a real one.
export function newWebhookSecret() {
  return "whsec_" + crypto.randomBytes(24).toString("base64").replace(/[^A-Za-z0-9]/g, "A");
}

export function newTestKeys() {
  const r = () => crypto.randomBytes(16).toString("hex");
  return { secretKey: "sk_test_" + r(), publishableKey: "pk_test_" + r() };
}
