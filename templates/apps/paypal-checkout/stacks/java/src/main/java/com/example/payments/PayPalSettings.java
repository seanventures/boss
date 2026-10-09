package com.example.payments;

import java.util.Locale;
import java.util.Map;
import java.util.function.Function;

/**
 * PayPal settings for the {@code paypal-checkout} template. SANDBOX by default: the app talks to the
 * live PayPal API only when PAYPAL_ENV=live AND APP_ENV=production, so a build or a preview can
 * never move real money (PayPal's sandbox and live credentials look alike, so the host is the guard).
 */
public final class PayPalSettings {
  public static final Map<String, String> HOSTS = Map.of(
      "sandbox", "https://api-m.sandbox.paypal.com",
      "live", "https://api-m.paypal.com");

  public static final class ConfigException extends IllegalStateException {
    public ConfigException(String message) {
      super(message);
    }
  }

  public final String env;
  /** Fixed per environment; never taken from configuration. */
  public final String apiBase;
  public final String clientId;
  public final String clientSecret;
  /** Empty: webhooks are refused (the return page still completes orders). */
  public final String webhookId;
  public final String currency;
  public final String baseUrl;
  public final boolean refundsEnabled;

  private PayPalSettings(String env, String clientId, String clientSecret, String webhookId, String currency,
      String baseUrl, boolean refundsEnabled) {
    this.env = env;
    this.apiBase = HOSTS.get(env);
    this.clientId = clientId;
    this.clientSecret = clientSecret;
    this.webhookId = webhookId;
    this.currency = currency;
    this.baseUrl = baseUrl;
    this.refundsEnabled = refundsEnabled;
  }

  private static String get(Function<String, String> env, String name, String fallback) {
    String v = env.apply(name);
    return v == null ? fallback : v.trim();
  }

  /** Reads the settings; errors name the variable, never a value. */
  public static PayPalSettings load(Function<String, String> env) {
    String paypalEnv = get(env, "PAYPAL_ENV", "sandbox").toLowerCase(Locale.ROOT);
    if (!HOSTS.containsKey(paypalEnv)) throw new ConfigException("PAYPAL_ENV must be 'sandbox' or 'live'");
    String appEnv = get(env, "APP_ENV", "development").toLowerCase(Locale.ROOT);
    if (paypalEnv.equals("live") && !appEnv.equals("production")) {
      throw new ConfigException("PAYPAL_ENV=live is only allowed when APP_ENV=production; previews and builds use the sandbox");
    }
    String clientId = get(env, "PAYPAL_CLIENT_ID", "");
    String clientSecret = get(env, "PAYPAL_CLIENT_SECRET", "");
    if (clientId.isEmpty()) throw new ConfigException("PAYPAL_CLIENT_ID is not set");
    if (clientSecret.isEmpty()) throw new ConfigException("PAYPAL_CLIENT_SECRET is not set");
    String currency = get(env, "PAYPAL_CURRENCY", "usd").toLowerCase(Locale.ROOT);
    if (!currency.matches("^[a-z]{3}$")) throw new ConfigException("PAYPAL_CURRENCY must be a 3-letter ISO code");
    return new PayPalSettings(paypalEnv, clientId, clientSecret, get(env, "PAYPAL_WEBHOOK_ID", ""), currency,
        get(env, "APP_BASE_URL", "").replaceAll("/+$", ""),
        get(env, "PAYMENTS_REFUNDS_ENABLED", "false").equalsIgnoreCase("true"));
  }

  @Override
  public String toString() { // never print a credential
    return "PayPalSettings[env=" + env + ", currency=" + currency + "]";
  }
}
