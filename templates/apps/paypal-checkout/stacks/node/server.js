// How an Express app mounts the `paypal-checkout` template. In an existing app, add the
// `createPayPalCheckout(...)` router to the app's own server (BEFORE any global express.json(), or
// exclude /webhooks/paypal from it: the webhook reads its own body).
const path = require("node:path");
const express = require("express");
const { loadPayPalConfig } = require("./payments/config");
const { PayPalClient } = require("./payments/paypal_client");
const { MemoryOrderStore } = require("./payments/store");
const { staticCatalog, DEFAULT_PRODUCTS } = require("./payments/catalog");
const { createPayPalCheckout } = require("./payments/paypal");

function createApp({ env = process.env, paypal, store = new MemoryOrderStore(), catalog, isAdmin, onOrderPaid } = {}) {
  const config = loadPayPalConfig(env); // sandbox unless PAYPAL_ENV=live AND APP_ENV=production
  const client = paypal || new PayPalClient({ apiBase: config.apiBase, clientId: config.clientId, clientSecret: config.clientSecret });
  const app = express();
  app.set("trust proxy", true);
  app.use(
    createPayPalCheckout({
      paypal: client,
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
  app.listen(port, () => console.log(`listening on ${port} (PayPal ${config.env})`));
}

module.exports = { createApp };
