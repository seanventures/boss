# PayPal Checkout: setup

Your app takes payments through PayPal, against the **PayPal sandbox**: no real money moves. The
app only talks to PayPal's live API when `PAYPAL_ENV=live` and `APP_ENV=production`, which a
person approves for a deploy.

## How this compares

- **Emergent** documents PayPal as: create a sandbox app in the PayPal Developer dashboard, give
  the agent the Client ID and Secret, test, then swap in live credentials after the first publish.
  Its previews are public, so PayPal can reach them.
- **Lovable** supports Stripe and Paddle only; it has no PayPal integration.

BOSS follows the same dashboard steps and adds what protects the money: prices only from the
server, each order captured once (with PayPal's idempotency key), only orders this app created
can be captured, a captured amount that differs from the order is held for review, every webhook
is checked by PayPal's own verification API against this app's webhook id, and orders complete
**without any webhook** (the capture happens on your server when the buyer approves), which is
what a private BOSS preview needs. Sandbox is enforced by the API host, because PayPal's sandbox
and live credentials look alike.

## 1. Create a sandbox app

1. Sign in at <https://developer.paypal.com> and open **Apps & Credentials**, toggle **Sandbox**.
2. Open **Default Application** or **Create App** (type: Merchant).
3. Copy the **Client ID** and the **Secret**.
4. In BOSS, open this app's **Setup** tab and set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.
   Values are write-only: BOSS shows whether each is set, never the value.

## 2. Pay with a sandbox buyer

1. In the developer dashboard open **Testing Tools → Sandbox Accounts**: there is a *Personal*
   (buyer) account; open it to see its email and password (or create one).
2. Open the preview: {{preview_url}}
3. Press the **PayPal** button, sign in as the sandbox buyer and approve. The page captures the
   payment on the server and shows the order as paid. The redirect flow returns to
   `{{preview_url}}/paypal/return`, which captures the same way.
4. To try a card instead, use the **Debit or Credit Card** button with a card number generated
   by the dashboard's **Testing Tools → Card Testing** tool.

## 3. Receive webhooks (optional on a preview, recommended in production)

The webhook finishes captures PayPal reports as *pending* (for example eChecks) and records
denials and refunds made in the PayPal dashboard. Your webhook URL is:

    {{webhook_url:paypal}}

The preview is private, so PayPal cannot reach it directly. To test webhooks, expose only the
preview through a tunnel you control (for example `cloudflared tunnel --url <preview address>`),
then in your sandbox app's **Webhooks → Add Webhook** enter `<tunnel URL>/webhooks/paypal` with the
events `Payment capture completed`, `Payment capture denied`, `Payment capture declined` and
`Payment capture refunded`. Copy the **Webhook ID** PayPal shows into `PAYPAL_WEBHOOK_ID`. Until
it is set, the app refuses every webhook (BOSS fills a random placeholder). You can also send
test events from **Testing Tools → Webhooks Simulator**: they are not for this app's orders, so
they are verified, acknowledged and ignored. Close the tunnel when you are done.

## 4. Refunds (optional)

Set `PAYMENTS_REFUNDS_ENABLED=true` and connect the app's admin check (for example the
`login-oauth` template plus a role). Only an admin can call `POST /api/orders/<id>/refund`; it
refunds the recorded capture for the amount the server recorded.

## 5. Going live

1. Create a **Live** app in **Apps & Credentials** (a verified PayPal Business account is needed)
   and register `{{public_url}}`'s `/webhooks/paypal` as its webhook.
2. For the `deploy` environment set the live `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` and
   `PAYPAL_WEBHOOK_ID`, plus `PAYPAL_ENV=live` and `APP_ENV=production`. BOSS asks a person holding
   `app_secret:live` to approve live keys, and the app's egress needs `api-m.paypal.com` added for
   the deploy (the template only requests the sandbox host).

## Troubleshooting

| Symptom | Cause |
|---|---|
| The app will not start: "only allowed when APP_ENV=production" | `PAYPAL_ENV=live` outside production. Previews always use the sandbox. |
| The buttons say the payment was declined | The sandbox buyer's funding source was declined; choose another (the buttons restart). |
| Webhook answers `400` | `PAYPAL_WEBHOOK_ID` is not this webhook's id, the event was edited, or it did not come from PayPal. |
| Order stays `pending` | PayPal reported the capture as pending; the `PAYMENT.CAPTURE.COMPLETED` webhook completes it (step 3). |
| Order is `needs_review` | PayPal captured an amount different from the order, or money arrived for a cancelled order. Check the PayPal dashboard before shipping or refunding. |
