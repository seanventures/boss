-- Tables for the `paypal-checkout` template (shared with `stripe-checkout` when both are applied:
-- `provider` tells them apart). Adapt the types to the app's database; keep the constraints.
CREATE TABLE IF NOT EXISTS orders (
  id            TEXT PRIMARY KEY,                -- a random UUID, never a sequence
  provider      TEXT NOT NULL CHECK (provider IN ('stripe', 'paypal')),
  provider_ref  TEXT UNIQUE,                     -- Stripe Checkout Session id / PayPal order id
  payment_ref   TEXT,                            -- Stripe PaymentIntent id / PayPal capture id
  cart_id       TEXT,                            -- set when the checkout came from catalog-cart
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded', 'needs_review')),
  items         TEXT NOT NULL,                   -- JSON: [{product_id, name, unit_amount_cents, currency, quantity}]
  amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),   -- integer minor units, priced by the server
  currency      TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  paid_at       TEXT,
  refunded_at   TEXT
);

-- Webhook idempotency: insert the event id in the SAME transaction as the order change it causes;
-- the primary key refuses a replay.
CREATE TABLE IF NOT EXISTS processed_events (
  event_id      TEXT PRIMARY KEY,
  provider      TEXT NOT NULL,
  type          TEXT NOT NULL,
  processed_at  TEXT NOT NULL
);
