// How an Express app mounts the `stripe-checkout` template. In an existing app, add the
// `createStripeCheckout(...)` router to the app's own server (BEFORE any global express.json(),
// or exclude /webhooks/stripe from it: the webhook needs the raw body to verify the signature).
const path = require("node:path");
const express = require("express");
const Stripe = require("stripe");
const { loadStripeConfig } = require("./payments/config");
const { MemoryOrderStore } = require("./payments/store");
const { staticCatalog, DEFAULT_PRODUCTS } = require("./payments/catalog");
const { createStripeCheckout } = require("./payments/stripe");

function createApp({ env = process.env, stripe, store = new MemoryOrderStore(), catalog, isAdmin, onOrderPaid } = {}) {
  const config = loadStripeConfig(env); // refuses a live key in test mode, and the reverse
  const client = stripe || new Stripe(config.secretKey, { maxNetworkRetries: 2 });
  const app = express();
  app.set("trust proxy", true);
  app.use(
    createStripeCheckout({
      stripe: client,
      config,
      store,
      catalog: catalog || staticCatalog(DEFAULT_PRODUCTS.map((p) => ({ ...p, currency: config.currency }))),
      isAdmin, // wire to the app's auth (e.g. login-oauth + a role); default: nobody may refund
      onOrderPaid, // ship the goods here; called exactly once per paid order
    }),
  );
  app.use(express.static(path.join(__dirname, "public")));
  return { app, store, config };
}

if (require.main === module) {
  const { app, config } = createApp();
  const port = Number(process.env.PORT || 3000);
  app.listen(port, () => console.log(`listening on ${port} (Stripe ${config.mode} mode)`));
}

module.exports = { createApp };
