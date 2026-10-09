// An in-process fake of the PayPal REST endpoints the template calls (OAuth token, Orders v2,
// captures, refunds, verify-webhook-signature). No test reaches PayPal.
//
// Webhook signatures: real PayPal signs with a certificate and its verify API checks it. The fake
// keeps that contract: an event "signed" by `signPayPalEvent` with the fake's signing secret (made
// per test run) verifies as SUCCESS; a forged or edited event, or another webhook id, is FAILURE.
import http from "node:http";
import crypto from "node:crypto";

export function signPayPalEvent(event, { secret, webhookId, transmissionId = crypto.randomUUID(), time = new Date().toISOString() }) {
  const digest = crypto.createHash("sha256").update(JSON.stringify(event)).digest("hex");
  const sig = crypto.createHmac("sha256", secret).update(`${transmissionId}|${time}|${webhookId}|${digest}`).digest("base64");
  return {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-fake",
    "paypal-transmission-id": transmissionId,
    "paypal-transmission-sig": sig,
    "paypal-transmission-time": time,
  };
}

export async function startFakePayPal({ clientId, clientSecret, webhookId }) {
  const signingSecret = crypto.randomBytes(32).toString("hex");
  const state = {
    orders: new Map(), tokens: new Set(), tokenRequests: 0, requests: [], refunds: [], verifications: [],
    captureByRequestId: new Map(), down: false, declineNext: false, pendingNext: false, captureAmountOverride: null,
    expiresIn: 32400,
  };

  function json(res, status, body) {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  }
  function error(res, status, name, issue) {
    json(res, status, { name, message: name, details: issue ? [{ issue }] : [] });
  }
  const id = () => crypto.randomBytes(9).toString("hex").toUpperCase();

  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const url = req.url;
      state.requests.push({ method: req.method, url, headers: req.headers, body: raw });
      if (state.down) return error(res, 503, "SERVICE_UNAVAILABLE");
      if (req.method === "POST" && url === "/v1/oauth2/token") {
        state.tokenRequests++;
        const expected = "Basic " + Buffer.from(`${clientId}:${clientSecret}`).toString("base64");
        if (req.headers.authorization !== expected || raw !== "grant_type=client_credentials") return error(res, 401, "invalid_client");
        const token = "A21AA" + crypto.randomBytes(16).toString("hex");
        state.tokens.add(token);
        return json(res, 200, { access_token: token, token_type: "Bearer", expires_in: state.expiresIn });
      }
      const bearer = (req.headers.authorization || "").replace(/^Bearer /, "");
      if (!state.tokens.has(bearer)) return error(res, 401, "AUTHENTICATION_FAILURE");
      const body = raw ? JSON.parse(raw) : {};
      if (req.method === "POST" && url === "/v2/checkout/orders") {
        const oid = id();
        const order = { id: oid, status: "PAYER_ACTION_REQUIRED", intent: body.intent, purchase_units: body.purchase_units,
          request_id: req.headers["paypal-request-id"], payment_source: body.payment_source,
          links: [{ rel: "payer-action", href: `https://www.sandbox.paypal.com/checkoutnow?token=${oid}`, method: "GET" }] };
        state.orders.set(oid, order);
        return json(res, 201, order);
      }
      let m = url.match(/^\/v2\/checkout\/orders\/([A-Z0-9]+)\/capture$/);
      if (req.method === "POST" && m) {
        const order = state.orders.get(m[1]);
        if (!order) return error(res, 404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID");
        const rid = req.headers["paypal-request-id"];
        if (rid && state.captureByRequestId.has(rid)) return json(res, 201, state.captureByRequestId.get(rid));
        if (order.status === "COMPLETED") return error(res, 422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED");
        if (order.status !== "APPROVED") return error(res, 422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED");
        if (state.declineNext) {
          state.declineNext = false;
          return error(res, 422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED");
        }
        const unit = order.purchase_units[0];
        const capture = { id: "CAP" + id(), status: state.pendingNext ? "PENDING" : "COMPLETED",
          amount: state.captureAmountOverride || { currency_code: unit.amount.currency_code, value: unit.amount.value },
          custom_id: unit.custom_id, supplementary_data: { related_ids: { order_id: order.id } } };
        state.pendingNext = false;
        unit.payments = { captures: [capture] };
        order.status = "COMPLETED";
        if (rid) state.captureByRequestId.set(rid, order);
        return json(res, 201, order);
      }
      m = url.match(/^\/v2\/checkout\/orders\/([A-Z0-9]+)$/);
      if (req.method === "GET" && m) {
        const order = state.orders.get(m[1]);
        return order ? json(res, 200, order) : error(res, 404, "RESOURCE_NOT_FOUND");
      }
      m = url.match(/^\/v2\/payments\/captures\/([A-Z0-9]+)\/refund$/);
      if (req.method === "POST" && m) {
        const refund = { id: "REF" + id(), status: "COMPLETED", capture_id: m[1], amount: body.amount, request_id: req.headers["paypal-request-id"] };
        state.refunds.push(refund);
        return json(res, 201, refund);
      }
      if (req.method === "POST" && url === "/v1/notifications/verify-webhook-signature") {
        state.verifications.push(body);
        const digest = crypto.createHash("sha256").update(JSON.stringify(body.webhook_event)).digest("hex");
        const expected = crypto.createHmac("sha256", signingSecret)
          .update(`${body.transmission_id}|${body.transmission_time}|${body.webhook_id}|${digest}`).digest("base64");
        const ok = body.webhook_id === webhookId && typeof body.transmission_sig === "string"
          && body.transmission_sig.length === expected.length
          && crypto.timingSafeEqual(Buffer.from(body.transmission_sig), Buffer.from(expected));
        return json(res, 200, { verification_status: ok ? "SUCCESS" : "FAILURE" });
      }
      return error(res, 404, "NOT_FOUND");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return {
    state,
    url: `http://127.0.0.1:${server.address().port}`,
    signingSecret,
    approve(orderId) {
      state.orders.get(orderId).status = "APPROVED";
    },
    revokeTokens() {
      state.tokens.clear();
    },
    close: () => new Promise((r) => server.close(r)),
  };
}

// Sandbox-shaped credentials made per test run. Never real ones.
export function newCredentials() {
  return {
    clientId: "A" + crypto.randomBytes(30).toString("base64url"),
    clientSecret: "E" + crypto.randomBytes(30).toString("base64url"),
    webhookId: crypto.randomBytes(8).toString("hex").toUpperCase(),
  };
}
