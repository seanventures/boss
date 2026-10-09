// Orders and processed webhook events for the `stripe-checkout` template.
//
// This reference keeps them in memory so it runs anywhere; ADAPT it to the app's database with
// the tables in schema.sql. What must survive the adaptation:
//   * `processed_events.event_id` is a PRIMARY KEY, and recording an event happens in the SAME
//     transaction as the order change it causes (so a crash cannot apply an event twice or lose it);
//   * order status only moves along TRANSITIONS below (paid never goes back to pending or failed);
//   * amounts are integer minor units (cents), never floats.
const crypto = require("node:crypto");

const TRANSITIONS = {
  pending: ["paid", "failed", "cancelled", "needs_review"],
  failed: ["paid", "cancelled", "needs_review"], // a buyer may retry a declined card in the same session
  paid: ["refunded"],
  cancelled: ["needs_review"], // paid after we cancelled it: a person decides
  refunded: [],
  needs_review: [],
};

class TransitionError extends Error {}

function canTransition(from, to) {
  return (TRANSITIONS[from] || []).includes(to);
}

class MemoryOrderStore {
  constructor() {
    this.orders = new Map();
    this.events = new Map(); // event_id -> { provider, type, processed_at }
  }

  createOrder({ provider, items, amountCents, currency, cartId = null }) {
    const order = {
      id: crypto.randomUUID(),
      provider,
      provider_ref: null, // Stripe: the Checkout Session id this app created for the order
      payment_ref: null, // Stripe: the PaymentIntent id, recorded when the order is paid
      cart_id: cartId,
      status: "pending",
      items: items.map((i) => ({ ...i })),
      amount_cents: amountCents,
      currency,
      created_at: new Date().toISOString(),
      paid_at: null,
      refunded_at: null,
    };
    this.orders.set(order.id, order);
    return { ...order };
  }

  getOrder(id) {
    const o = this.orders.get(String(id));
    return o ? { ...o, items: o.items.map((i) => ({ ...i })) } : null;
  }

  setProviderRef(id, ref) {
    const o = this.orders.get(id);
    if (!o) throw new TransitionError("no such order");
    o.provider_ref = ref;
  }

  setPaymentRef(id, ref) {
    const o = this.orders.get(id);
    if (!o) throw new TransitionError("no such order");
    o.payment_ref = ref;
  }

  findByPaymentRef(provider, ref) {
    for (const o of this.orders.values()) {
      if (o.provider === provider && o.payment_ref === ref) return this.getOrder(o.id);
    }
    return null;
  }

  // Move an order to `to`. Returns true when this call changed it, false when it was already
  // there (the idempotent case). Throws on a transition the state machine does not allow.
  transition(id, to) {
    const o = this.orders.get(id);
    if (!o) throw new TransitionError("no such order");
    if (o.status === to) return false;
    if (!canTransition(o.status, to)) throw new TransitionError(`order ${o.status} cannot become ${to}`);
    o.status = to;
    if (to === "paid") o.paid_at = new Date().toISOString();
    if (to === "refunded") o.refunded_at = new Date().toISOString();
    return true;
  }

  // Runs `apply()` only if `eventId` was never processed; records it in the same step. In the
  // in-memory store `apply` is synchronous, so nothing can interleave; in SQL, do both in one
  // transaction and let the primary key refuse the second insert.
  processEventOnce(eventId, provider, type, apply) {
    if (this.events.has(eventId)) return { duplicate: true };
    const result = apply();
    this.events.set(eventId, { provider, type, processed_at: new Date().toISOString() });
    return { duplicate: false, result };
  }
}

module.exports = { MemoryOrderStore, TransitionError, canTransition, TRANSITIONS };
