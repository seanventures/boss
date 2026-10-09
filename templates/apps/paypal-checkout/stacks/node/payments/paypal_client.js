// A small PayPal REST client (Orders v2, Payments v2, webhook verification) on fetch. No SDK:
// PayPal's server SDKs change often; these four calls are stable and documented.
//
// The OAuth access token is cached until shortly before it expires and refreshed once on a 401.
class PayPalError extends Error {
  constructor(status, body) {
    super(`PayPal answered ${status}${body && body.name ? ` ${body.name}` : ""}`);
    this.status = status;
    this.body = body || {};
  }

  // PayPal's error detail issue codes, e.g. INSTRUMENT_DECLINED, ORDER_ALREADY_CAPTURED.
  issues() {
    return (this.body.details || []).map((d) => d.issue);
  }
}

class PayPalClient {
  constructor({ apiBase, clientId, clientSecret, fetchImpl = fetch, now = () => Date.now() }) {
    this.apiBase = apiBase;
    this.clientId = clientId;
    this.clientSecret = clientSecret;
    this.fetch = fetchImpl;
    this.now = now;
    this.token = null; // { value, expiresAt }
  }

  async accessToken(force = false) {
    if (!force && this.token && this.now() < this.token.expiresAt) return this.token.value;
    const basic = Buffer.from(`${this.clientId}:${this.clientSecret}`).toString("base64");
    const r = await this.fetch(`${this.apiBase}/v1/oauth2/token`, {
      method: "POST",
      headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded" },
      body: "grant_type=client_credentials",
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || !body.access_token) throw new PayPalError(r.status, body);
    // Refresh a minute early so a token never expires mid-request.
    const ttl = Math.max(0, Number(body.expires_in || 0) - 60) * 1000;
    this.token = { value: body.access_token, expiresAt: this.now() + ttl };
    return this.token.value;
  }

  async request(method, path, { body, requestId } = {}) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const token = await this.accessToken(attempt > 0);
      const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Prefer: "return=representation" };
      if (requestId) headers["PayPal-Request-Id"] = requestId; // PayPal's idempotency key
      const r = await this.fetch(`${this.apiBase}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      if (r.status === 401 && attempt === 0) continue; // token revoked or expired early: refresh once
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new PayPalError(r.status, out);
      return out;
    }
    throw new PayPalError(401, {});
  }

  createOrder(body, requestId) {
    return this.request("POST", "/v2/checkout/orders", { body, requestId });
  }

  getOrder(id) {
    return this.request("GET", `/v2/checkout/orders/${encodeURIComponent(id)}`);
  }

  captureOrder(id, requestId) {
    return this.request("POST", `/v2/checkout/orders/${encodeURIComponent(id)}/capture`, { body: {}, requestId });
  }

  refundCapture(captureId, amount, requestId) {
    return this.request("POST", `/v2/payments/captures/${encodeURIComponent(captureId)}/refund`, { body: { amount }, requestId });
  }

  // PayPal's verify-webhook-signature API: PayPal checks the certificate chain and the signature.
  async verifyWebhook({ headers, event, webhookId }) {
    const out = await this.request("POST", "/v1/notifications/verify-webhook-signature", {
      body: {
        auth_algo: headers["paypal-auth-algo"],
        cert_url: headers["paypal-cert-url"],
        transmission_id: headers["paypal-transmission-id"],
        transmission_sig: headers["paypal-transmission-sig"],
        transmission_time: headers["paypal-transmission-time"],
        webhook_id: webhookId,
        webhook_event: event,
      },
    });
    return out.verification_status === "SUCCESS";
  }
}

module.exports = { PayPalClient, PayPalError };
