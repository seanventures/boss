# PayPal Checkout: security note

## What it protects

- **Price integrity.** Every amount is computed on the server (product table or the
  `catalog-cart` cart) in integer minor units and sent to PayPal with an item breakdown; the
  client sends product ids and quantities only. Decimal strings are produced and parsed without
  floating point.
- **Capture only what we created, once.** Capture looks the PayPal order up among this app's
  orders (an unknown id is refused without calling PayPal), skips an order already paid, and
  sends `PayPal-Request-Id` so a retried capture cannot charge twice. A `COMPLETED` capture pays
  the order only for exactly the priced amount and currency; anything else is `needs_review`.
- **Webhook authenticity.** A webhook needs all PayPal transmission headers and a `SUCCESS` from
  PayPal's verify-webhook-signature API for this app's `PAYPAL_WEBHOOK_ID` (so an event signed for
  another merchant's endpoint is refused). Its capture must belong to one of this app's PayPal
  orders and to the order its `custom_id` names. Event ids are recorded (primary key) with the
  change they cause, so a replay is ignored.
- **Sandbox by default.** The API host is fixed per environment and is the live host only when
  `PAYPAL_ENV=live` and `APP_ENV=production`; BOSS never gives live credentials to a build or a
  preview, and a deploy needs a person's approval to use them.
- **Secrets.** The client secret only ever goes to PayPal's token endpoint over HTTPS; it is never
  logged, returned (`/api/paypal/config` returns the public client id) or committed. The OAuth
  token is held in memory only.
- **Refunds** are off by default; when on, they need the app's admin check and refund the
  recorded capture for the recorded amount, idempotently.

## What it does not do

- **No buyer authentication or authorization.** Anyone can start a checkout; the order id (a
  random UUID) is the only handle on `GET /api/orders/:id`. Add `login-oauth` to tie orders to
  users, and keep refunds behind a real admin role.
- **No rate limit** on order creation; put one in front of it in production.
- **No subscriptions, vaulting, shipping or tax.** One-off captures only.
- **Webhook verification depends on PayPal's API being reachable** (a call per event). If it is
  down, webhooks are refused and PayPal retries them; orders still complete through capture.
- The in-memory store in the Node reference is for tests; the app must use a database with the
  constraints in `schema.sql`, or a restart loses orders and the idempotency record.
