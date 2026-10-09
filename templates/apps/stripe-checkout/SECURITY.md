# Stripe Checkout: security note

## What it protects

- **Price integrity.** Every amount is computed on the server from the product table (or the
  `catalog-cart` cart) in integer minor units. The client sends product ids and quantities only;
  a price, amount or currency in the request is ignored. Quantities are whole numbers 1-99.
- **Webhook authenticity.** `POST /webhooks/stripe` verifies the `Stripe-Signature` HMAC-SHA256
  over the raw body with `STRIPE_WEBHOOK_SECRET`, and refuses a timestamp older than 5 minutes
  (replay window). A forged, tampered or unsigned request gets `400` and changes nothing.
- **Exactly-once fulfilment.** Each event id is recorded (primary key) in the same transaction
  as the order change it causes; a replayed event is acknowledged and ignored. The success page
  and the webhook converge on one state machine, so the "ship it" hook runs once per order.
- **No trust in the redirect.** The success page reads the session back from Stripe with the
  secret key; it fulfils only the session this app created for that order, only when Stripe says
  it is paid, and only for the amount and currency the server priced. A mismatch, or money for a
  cancelled order, is held as `needs_review`.
- **Test mode by default.** The app refuses a live key unless `STRIPE_MODE=live`, and BOSS never
  gives a live key to a build or a preview; a deploy needs a person's approval to use one.
- **Secrets.** Keys come from the environment (BOSS's vault); they are never logged, returned by
  an API (`/api/checkout/config` returns the publishable key only) or committed.
- **Refunds** are off by default; when on, they need the app's admin check and refund the
  recorded amount with an idempotency key.

## What it does not do

- **No authentication or authorization of buyers.** Anyone can start a checkout; an order id
  (a random UUID) is the only handle on `GET /api/orders/:id`. Add `login-oauth` to tie orders to
  users, and keep refunds behind a real admin role.
- **No fraud screening** beyond Stripe's own (Radar). No rate limit on `POST /api/checkout`;
  put one in front of it in production.
- **No subscriptions, taxes, coupons or shipping.** One-off payments only.
- **No PCI scope reduction beyond Checkout's.** Card data never touches the app because Stripe
  hosts the payment page; do not replace Checkout with a custom card form without re-reviewing.
- The in-memory store in the reference is for tests; the app must use a database with the
  constraints in `schema.sql`, or a restart loses orders and the idempotency record.
