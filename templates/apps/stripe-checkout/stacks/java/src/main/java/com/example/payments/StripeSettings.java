package com.example.payments;

import java.util.Locale;
import java.util.function.Function;
import java.util.regex.Pattern;

/**
 * Stripe settings for the {@code stripe-checkout} template. Test mode is the default: a live key is
 * refused unless STRIPE_MODE=live, and a test key is refused when STRIPE_MODE=live, so a preview can
 * never charge a real card by accident and a deploy can never silently run on test keys.
 */
public final class StripeSettings {
  private static final Pattern TEST_SECRET = Pattern.compile("^sk_test_[A-Za-z0-9]+$");
  private static final Pattern LIVE_SECRET = Pattern.compile("^sk_live_[A-Za-z0-9]+$");
  private static final Pattern TEST_PUBLISHABLE = Pattern.compile("^pk_test_[A-Za-z0-9]+$");
  private static final Pattern LIVE_PUBLISHABLE = Pattern.compile("^pk_live_[A-Za-z0-9]+$");

  public static final class ConfigException extends IllegalStateException {
    public ConfigException(String message) {
      super(message);
    }
  }

  public final String mode;
  public final String secretKey;
  public final String publishableKey;
  public final String webhookSecret;
  public final String currency;
  public final String baseUrl;
  public final boolean refundsEnabled;
  /** Stripe-Signature timestamps older than this are refused (replay window), in seconds. */
  public final long webhookToleranceSeconds = 300;

  private StripeSettings(String mode, String secretKey, String publishableKey, String webhookSecret,
      String currency, String baseUrl, boolean refundsEnabled) {
    this.mode = mode;
    this.secretKey = secretKey;
    this.publishableKey = publishableKey;
    this.webhookSecret = webhookSecret;
    this.currency = currency;
    this.baseUrl = baseUrl;
    this.refundsEnabled = refundsEnabled;
  }

  private static String get(Function<String, String> env, String name, String fallback) {
    String v = env.apply(name);
    return v == null ? fallback : v.trim();
  }

  /** Reads the settings; the errors name the variable and the shape it must have, never a value. */
  public static StripeSettings load(Function<String, String> env) {
    String mode = get(env, "STRIPE_MODE", "test").toLowerCase(Locale.ROOT);
    if (!mode.equals("test") && !mode.equals("live")) {
      throw new ConfigException("STRIPE_MODE must be 'test' or 'live'");
    }
    boolean live = mode.equals("live");
    String secretKey = get(env, "STRIPE_SECRET_KEY", "");
    String publishableKey = get(env, "STRIPE_PUBLISHABLE_KEY", "");
    String webhookSecret = get(env, "STRIPE_WEBHOOK_SECRET", "");
    if (!(live ? LIVE_SECRET : TEST_SECRET).matcher(secretKey).matches()) {
      throw new ConfigException("STRIPE_SECRET_KEY is not a " + mode + "-mode secret key (STRIPE_MODE=" + mode + ")");
    }
    if (!publishableKey.isEmpty() && !(live ? LIVE_PUBLISHABLE : TEST_PUBLISHABLE).matcher(publishableKey).matches()) {
      throw new ConfigException("STRIPE_PUBLISHABLE_KEY is not a " + mode + "-mode publishable key (STRIPE_MODE=" + mode + ")");
    }
    if (webhookSecret.isEmpty()) {
      throw new ConfigException("STRIPE_WEBHOOK_SECRET is not set");
    }
    String currency = get(env, "STRIPE_CURRENCY", "usd").toLowerCase(Locale.ROOT);
    if (!currency.matches("^[a-z]{3}$")) {
      throw new ConfigException("STRIPE_CURRENCY must be a 3-letter ISO code");
    }
    String baseUrl = get(env, "APP_BASE_URL", "").replaceAll("/+$", "");
    boolean refunds = get(env, "PAYMENTS_REFUNDS_ENABLED", "false").equalsIgnoreCase("true");
    return new StripeSettings(mode, secretKey, publishableKey, webhookSecret, currency, baseUrl, refunds);
  }

  @Override
  public String toString() { // never print a key
    return "StripeSettings[mode=" + mode + ", currency=" + currency + "]";
  }
}
