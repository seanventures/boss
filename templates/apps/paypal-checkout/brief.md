# Add PayPal Checkout payments

Add one-off payments with **PayPal Checkout (Orders v2)** to this app, against the **PayPal
sandbox**. The reference implementation for this app's stack is attached; adapt it into the
app's own router, data layer and UI rather than copying files over the top of the app.

## What to build (every stack)

| Route | What it does |
|---|---|
| `POST /api/paypal/orders` | Body `{cart_id}` or `{items: [{product_id, quantity}]}`. Prices every line **on the server**, creates an `orders` row (`pending`, integer cents), creates a PayPal order (`intent: CAPTURE`, `custom_id` and `reference_id` = our order id, items and `item_total` breakdown from server prices, `PayPal-Request-Id: create-<order id>`), and answers `201 {order_id, paypal_order_id, approve_url}`. Any price, amount or currency the client sends is ignored. |
| `POST /api/paypal/orders/:id/capture` | `:id` is the PayPal order id the JS SDK's `onApprove` gives. Captures **only a PayPal order this app created** (404 otherwise, without calling PayPal), with `PayPal-Request-Id: capture-<order id>`; an already-paid order is never captured again. `ORDER_ALREADY_CAPTURED` is reconciled by reading the order back; `INSTRUMENT_DECLINED` answers `402 {restart: true}` (the buttons call `actions.restart()`). Pays the order only for a `COMPLETED` capture of exactly the priced amount and currency; a mismatch is `needs_review`. |
| `GET /paypal/return?token=` | The redirect flow's return page: the same server-side capture. Works on a **private preview**: no webhook is needed. |
| `GET /paypal/cancel` | The buyer came back without paying. |
| `POST /webhooks/paypal` | Refused (`400`) unless all five `PayPal-*` transmission headers are present and PayPal's **verify-webhook-signature** API answers `SUCCESS` for `PAYPAL_WEBHOOK_ID`. Records the event id in `processed_events` (primary key) with the order change it causes. Handles `PAYMENT.CAPTURE.COMPLETED` (completes a capture PayPal first reported as `PENDING`), `PAYMENT.CAPTURE.DENIED`/`DECLINED`, `PAYMENT.CAPTURE.REFUNDED`; an event's capture must belong to a PayPal order this app created for the order its `custom_id` names. |
| `GET /api/orders/:id` | The order's status. |
| `POST /api/orders/:id/refund` | Optional: `404` unless `PAYMENTS_REFUNDS_ENABLED=true`; `403` unless the app's `isAdmin` check passes (default: nobody). Refunds the recorded capture for the server-recorded amount with `PayPal-Request-Id: refund-<order id>`. |
| `GET /api/paypal/config` | `{env, client_id, currency}` for the JS SDK script tag. Never the secret. |

The OAuth access token (`POST /v1/oauth2/token`, client credentials) is cached until a minute
before it expires and refreshed once on a `401`. The API host is **fixed per environment**:
`api-m.sandbox.paypal.com` unless `PAYPAL_ENV=live` **and** `APP_ENV=production` (the app refuses
to start with `PAYPAL_ENV=live` otherwise); it is never read from configuration.

Money is converted between integer minor units and PayPal's decimal strings without floating
point (JPY, HUF and TWD have no decimals).

Order status uses the same state machine as `stripe-checkout`; if the app already has its
`orders` table, reuse it (`provider = 'paypal'`), and serve one `GET /api/orders/:id` and one
refund route that dispatch on `provider`.

## With `catalog-cart`

`POST /api/paypal/orders` takes the cart's `cart_id` and prices from the cart's checkout contract
(`cart_id → [{product_id, name, unit_amount_cents, currency, quantity}]`, server prices, for the
requesting session's cart only). Show the PayPal buttons in the cart drawer. Without it, keep the
static catalog and one product's buttons.

## Per stack

- **node (Express)**: `payments/config.js`, `paypal_client.js` (fetch, no SDK), `store.js`,
  `catalog.js`, `paypal.js` (an Express router) and `server.js`; the buttons in
  `public/checkout.html`. Add `express` and the two scripts `pretest:template` / `test:template`
  from the reference `package.json`. Copy `tests/template_paypal/` as is.
- **python (FastAPI)**: `payments/` (config, `paypal_client.py` on httpx, store, catalog,
  `paypal_checkout.py` with an `APIRouter` factory) and `main.py`. Add `fastapi`, `httpx` and
  `pytest` to the app's requirements. Copy `tests/template_paypal/` as is.

The template's tests run against an in-process fake of the PayPal API (token, orders, capture,
refund, verify-webhook-signature) and never reach the network. Keep them as they are; they are
the proof this template was applied.
