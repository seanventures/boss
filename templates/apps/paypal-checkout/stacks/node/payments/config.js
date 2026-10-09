// PayPal settings for the `paypal-checkout` template. SANDBOX by default: the app talks to the
// live PayPal API only when PAYPAL_ENV=live AND APP_ENV=production, so a build or a preview can
// never move real money (PayPal's sandbox and live credentials look alike, so the host is the guard).
const HOSTS = {
  sandbox: "https://api-m.sandbox.paypal.com",
  live: "https://api-m.paypal.com",
};

class ConfigError extends Error {}

function loadPayPalConfig(env = process.env) {
  const paypalEnv = (env.PAYPAL_ENV || "sandbox").trim().toLowerCase();
  if (!(paypalEnv in HOSTS)) throw new ConfigError("PAYPAL_ENV must be 'sandbox' or 'live'");
  const appEnv = (env.APP_ENV || "development").trim().toLowerCase();
  if (paypalEnv === "live" && appEnv !== "production") {
    throw new ConfigError("PAYPAL_ENV=live is only allowed when APP_ENV=production; previews and builds use the sandbox");
  }
  const clientId = (env.PAYPAL_CLIENT_ID || "").trim();
  const clientSecret = (env.PAYPAL_CLIENT_SECRET || "").trim();
  const webhookId = (env.PAYPAL_WEBHOOK_ID || "").trim();
  // Never echo a credential in an error: name the variable only.
  if (!clientId) throw new ConfigError("PAYPAL_CLIENT_ID is not set");
  if (!clientSecret) throw new ConfigError("PAYPAL_CLIENT_SECRET is not set");
  const currency = (env.PAYPAL_CURRENCY || "usd").trim().toLowerCase();
  if (!/^[a-z]{3}$/.test(currency)) throw new ConfigError("PAYPAL_CURRENCY must be a 3-letter ISO code");
  return {
    env: paypalEnv,
    apiBase: HOSTS[paypalEnv], // fixed per environment; never taken from configuration
    clientId,
    clientSecret,
    webhookId, // empty: webhooks are refused (the return page still completes orders)
    currency,
    baseUrl: (env.APP_BASE_URL || "").replace(/\/+$/, ""),
    refundsEnabled: (env.PAYMENTS_REFUNDS_ENABLED || "false").toLowerCase() === "true",
  };
}

module.exports = { loadPayPalConfig, ConfigError, HOSTS };
