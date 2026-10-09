// Stripe settings for the `stripe-checkout` template. Test mode is the default: a live key is
// refused unless STRIPE_MODE=live, and a test key is refused when STRIPE_MODE=live, so a preview
// can never charge a real card by accident and a deploy can never silently run on test keys.
const TEST_SECRET = /^sk_test_[A-Za-z0-9]+$/;
const LIVE_SECRET = /^sk_live_[A-Za-z0-9]+$/;
const TEST_PUBLISHABLE = /^pk_test_[A-Za-z0-9]+$/;
const LIVE_PUBLISHABLE = /^pk_live_[A-Za-z0-9]+$/;

class ConfigError extends Error {}

function loadStripeConfig(env = process.env) {
  const mode = (env.STRIPE_MODE || "test").trim().toLowerCase();
  if (mode !== "test" && mode !== "live") {
    throw new ConfigError("STRIPE_MODE must be 'test' or 'live'");
  }
  const secretKey = (env.STRIPE_SECRET_KEY || "").trim();
  const publishableKey = (env.STRIPE_PUBLISHABLE_KEY || "").trim();
  const webhookSecret = (env.STRIPE_WEBHOOK_SECRET || "").trim();
  const [secretRx, pubRx] = mode === "live" ? [LIVE_SECRET, LIVE_PUBLISHABLE] : [TEST_SECRET, TEST_PUBLISHABLE];
  // Never echo a key in an error: name the variable and the shape it must have, nothing else.
  if (!secretRx.test(secretKey)) {
    throw new ConfigError(`STRIPE_SECRET_KEY is not a ${mode}-mode secret key (STRIPE_MODE=${mode})`);
  }
  if (publishableKey && !pubRx.test(publishableKey)) {
    throw new ConfigError(`STRIPE_PUBLISHABLE_KEY is not a ${mode}-mode publishable key (STRIPE_MODE=${mode})`);
  }
  if (!webhookSecret) {
    throw new ConfigError("STRIPE_WEBHOOK_SECRET is not set");
  }
  const currency = (env.STRIPE_CURRENCY || "usd").trim().toLowerCase();
  if (!/^[a-z]{3}$/.test(currency)) throw new ConfigError("STRIPE_CURRENCY must be a 3-letter ISO code");
  return {
    mode,
    secretKey,
    publishableKey,
    webhookSecret,
    currency,
    // Where Checkout sends the buyer back. Unset: the request's own origin.
    baseUrl: (env.APP_BASE_URL || "").replace(/\/+$/, ""),
    refundsEnabled: (env.PAYMENTS_REFUNDS_ENABLED || "false").toLowerCase() === "true",
    // Stripe-Signature timestamps older than this are refused (replay window), in seconds.
    webhookToleranceSeconds: 300,
  };
}

module.exports = { loadStripeConfig, ConfigError };
