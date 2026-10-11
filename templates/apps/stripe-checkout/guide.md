# Stripe Checkout: setup

Your app takes payments through Stripe Checkout, in **test mode**: no real money moves until a
person approves live keys for a deploy.

## How this compares

- **Emergent** gives a managed Stripe sandbox (injected `STRIPE_*` keys) or your own keys, and
  asks you to add the webhook URL to the Stripe Dashboard yourself; its advice is to validate
  amounts server-side and rely on webhooks rather than the client's redirect.
- **Lovable** creates the Stripe account for you, registers two webhook endpoints per
  environment, keeps keys out of your hands, and warns that deleting the webhook silently stops
  orders from completing.

BOSS keeps your own Stripe account (nothing is created on your behalf) and goes further on the
parts that lose money: prices are **only** computed on the server, every webhook is signature-
checked and processed once by event id, a paid amount that does not match the order is held for
review, and orders complete **even when no webhook arrives** (the success page reads the session
back from Stripe). That last point matters here because BOSS previews are private.

## 1. Connect Stripe (test mode)

1. Sign in at <https://dashboard.stripe.com> and switch on **Test mode** (top right).
2. Open **Developers → API keys**. Copy the **Secret key** (`sk_test_...`) and the
   **Publishable key** (`pk_test_...`).
3. In BOSS, open this app's **Setup** tab and set:
   - `STRIPE_SECRET_KEY`: the `sk_test_...` key;
   - `STRIPE_PUBLISHABLE_KEY`: the `pk_test_...` key.

   BOSS accepts only test-shaped values here; a `sk_live_` key is refused. Values are write-only:
   BOSS shows whether a key is set and its mode, never the key.
4. `STRIPE_WEBHOOK_SECRET` is already set to a generated value, which is enough for the build's
   tests. For real Stripe events, replace it in step 3 below.

## 2. Pay with a test card

1. Open the preview: {{preview_url}}
2. Press **Buy now** (or **Checkout** in the cart). Stripe's page opens.
3. Pay with **4242 4242 4242 4242**, any future expiry, any CVC, any postcode.
   - `4000 0025 0000 3155`: asks for 3-D Secure authentication.
   - `4000 0000 0000 9995`: declined (insufficient funds), to check the failure path.
4. You land on `{{preview_url}}/checkout/success`, which reads the session back from Stripe and
   marks the order **paid**. This works without any webhook.

## 3. Receive webhooks (optional on a preview, required in production)

The webhook is the authoritative path: it completes the order even when the buyer closes the tab
before the redirect, and records failures, expiries and refunds. Your webhook URL is:

    {{webhook_url:stripe}}

The preview is private, so Stripe cannot reach it directly. Pick one:

- **Stripe CLI (recommended, nothing is exposed).** On a machine that can reach the preview:
  `stripe login`, then `stripe listen --forward-to {{webhook_url:stripe}}`. The CLI prints a
  signing secret (`whsec_...`): set it as `STRIPE_WEBHOOK_SECRET` (env `preview`) in Setup.
  Try it with `stripe trigger checkout.session.completed` (that event is not for one of this
  app's orders, so it is acknowledged and ignored) or by paying again in the preview.
- **A tunnel.** Expose only the preview's port through a tunnel you control (for example
  `cloudflared tunnel --url <preview address>`), then in **Developers → Webhooks → Add endpoint**
  enter `<tunnel URL>/webhooks/stripe` with the events `checkout.session.completed`,
  `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`,
  `checkout.session.expired`, `payment_intent.payment_failed` and `charge.refunded`. Copy the
  endpoint's signing secret into `STRIPE_WEBHOOK_SECRET`. A tunnel makes the preview reachable
  from the internet: close it when you are done.

## 4. Refunds (optional)

Set `PAYMENTS_REFUNDS_ENABLED=true` and connect the app's admin check (for example the
`login-oauth` template plus a role). Only an admin can call `POST /api/orders/<id>/refund`; it
refunds the amount the server recorded, never an amount from the request.

## 5. Going live

1. Deploy the app with a public URL; add `{{public_url}}`'s `/webhooks/stripe` as a webhook
   endpoint in the Stripe Dashboard in **live** mode and copy its signing secret.
2. Set `STRIPE_MODE=live` and the live keys for the `deploy` environment. BOSS asks a person
   holding `app_secret:live` to approve "Use live Stripe keys" before the deploy may use them.
3. The app refuses to start with a live key in test mode, and with a test key in live mode.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The order stays `pending` after paying | The buyer did not return to the success page and no webhook arrives: set up step 3. |
| Webhook answers `400` | The signing secret does not match the sender (each `stripe listen` session and each Dashboard endpoint has its own), or the event is older than 5 minutes. |
| The app will not start: "not a test-mode secret key" | A live key in test mode, or a key pasted into the wrong variable. |
| Order is `needs_review` | Stripe reports a paid amount different from the order, or a payment arrived after the order was cancelled. Check it in the Stripe Dashboard before shipping or refunding. |
