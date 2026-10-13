# Add Stripe Checkout payments

Add one-off payments with **Stripe Checkout** (the Stripe-hosted payment page) to this app, in
**test mode**. The reference implementation for this app's stack is attached; adapt it into the
app's own router, data layer and UI rather than copying files over the top of the app.

## What to build (every stack)

| Route | What it does |
|---|---|
| `POST /api/checkout` | Body `{cart_id}` or `{items: [{product_id, quantity}]}`. Prices every line **on the server** (product table or the cart), creates an `orders` row (`pending`, integer cents), creates a Checkout Session with `price_data` built from those server prices, `metadata.order_id`, `payment_intent_data.metadata.order_id`, an idempotency key `checkout-<order id>`, and answers `201 {order_id, session_id, checkout_url}`. Any price, amount or currency the client sends is ignored. |
| `GET /checkout/success?session_id=` | Retrieves the session **from Stripe** (never trusts the query string), checks it is the session this app created for that order and that `amount_total`/`currency` match the order, and fulfils idempotently. Answers JSON to API clients and a small page to a browser. This is what makes a **private preview** work: no inbound webhook is needed. |
| `GET /checkout/cancel` | The buyer came back without paying. |
| `POST /webhooks/stripe` | Verifies `Stripe-Signature` against `STRIPE_WEBHOOK_SECRET` over the **raw** body with a 300-second tolerance (the SDK's `constructEvent`); `400` on any failure. Records the event id in `processed_events` (primary key) in the same transaction as the order change, so a replay is a no-op. Handles `checkout.session.completed`, `checkout.session.async_payment_succeeded` / `_failed`, `checkout.session.expired`, `payment_intent.payment_failed`, `charge.refunded`. |
| `GET /api/orders/:id` | The order's status, for the UI. |
| `POST /api/orders/:id/refund` | Optional: `404` unless `PAYMENTS_REFUNDS_ENABLED=true`; `403` unless the app's `isAdmin` check passes (default: nobody). Refunds the server-recorded amount with an idempotency key. |
| `GET /api/checkout/config` | `{mode, publishable_key}` for the UI's test-mode banner. Never the secret key. |

Order status moves only `pending → paid | failed | cancelled | needs_review`, `failed → paid`,
`paid → refunded`, `cancelled → needs_review`. A paid session whose amount differs from the
order, or money arriving for a cancelled order, goes to `needs_review` and is never shipped.
Fulfilment (the app's "ship it" hook) runs **exactly once** per paid order, whichever path
(success page or webhook) gets there first.

Tables: `orders` and `processed_events` as in `schema.sql`. If the app already has an `orders`
table from `paypal-checkout`, reuse it (`provider = 'stripe'`).

Configuration: `STRIPE_MODE` (default `test`). In test mode the app **refuses to start** with a
`sk_live_` key, and in live mode with a `sk_test_` key; errors name the variable, never the value.

## With `catalog-cart`

If the app has the `catalog-cart` template, `POST /api/checkout` takes its `cart_id` and prices
the lines from the cart's checkout contract (`cart_id → [{product_id, name, unit_amount_cents,
currency, quantity}]`, server prices, for the requesting session's cart only). Replace the
reference's static catalog with that contract and add a "Checkout" button to the cart drawer.
catalog-cart provides it under exactly the names `payments/catalog` calls: node `createCatalog(...)`
returns `getProduct` / `cartLineItems(cartId, req)`, python `create_catalog(...)` returns
`get_product` / `cart_line_items(cart_id, request)`, java `CheckoutCatalog.product` /
`cartLines(cartId, request)`. Its `CatalogError` (a cart that cannot be sold now) needs no
wiring: `payments/catalog` itself turns it into `CheckoutInputError` / `CheckoutInputException`
(matched by class name and its 4xx `status` + `code`, not imported), so an unsellable cart is a
400 with catalog-cart's code, never a 500, and Stripe is never called. Keep that mapping when
adapting the adapter.
Without it, keep the static catalog and a single "Buy now" button.

## Per stack

- **node (Express)**: `payments/config.js`, `store.js`, `catalog.js`, `stripe.js` (an Express
  router) and `server.js` show the mounting. Mount the webhook **before** any global
  `express.json()` (it needs the raw body). Add `express` and `stripe` to `package.json`.
- **python (FastAPI)**: `payments/` (config, store, catalog, `stripe_checkout.py` with an
  `APIRouter` factory) and `main.py`. Add `stripe`, `fastapi`, `httpx` and `pytest` to the app's
  requirements.
- **java (Spring Boot)**: the `com.example.payments` package (config, store, catalog, controller)
  and `pom.xml` dependencies (`com.stripe:stripe-java`). Move the package under the app's base
  package if needed.

## Its tests stay with the connector

This connector is pre-tested. Its own tests use an in-process fake of the Stripe API and compute
webhook signatures with a signing secret generated in the test: they never reach the network.
They run in the connector's own test tier, not in the app: do not copy `tests/template_stripe/`
(or the reference `package.json`'s `test:template` scripts) into the app, and do not write tests
for the connector. A build that adds it is checked for its wiring only: the app starts and these
routes answer.
