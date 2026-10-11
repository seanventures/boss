package com.example.payments;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.LongSupplier;

/**
 * A small PayPal REST client (Orders v2, Payments v2, webhook verification) on java.net.http. No
 * SDK: these calls are stable and documented. The OAuth access token is cached until shortly
 * before it expires and refreshed once on a 401.
 */
public class PayPalClient {
  private static final ObjectMapper JSON = new ObjectMapper();

  public static final class PayPalException extends Exception {
    public final int status;
    public final Map<String, Object> body;

    PayPalException(int status, Map<String, Object> body) {
      super("PayPal answered " + status + (body.get("name") != null ? " " + body.get("name") : ""));
      this.status = status;
      this.body = body;
    }

    /** PayPal's error detail issue codes, e.g. INSTRUMENT_DECLINED, ORDER_ALREADY_CAPTURED. */
    public List<String> issues() {
      List<String> out = new ArrayList<>();
      if (body.get("details") instanceof List<?> details) {
        for (Object d : details) {
          if (d instanceof Map<?, ?> m && m.get("issue") != null) out.add(String.valueOf(m.get("issue")));
        }
      }
      return out;
    }
  }

  private final String apiBase;
  private final String basicAuth;
  private final LongSupplier nowMillis;
  private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
  private String token;
  private long tokenExpiresAt;

  public PayPalClient(String apiBase, String clientId, String clientSecret, LongSupplier nowMillis) {
    this.apiBase = apiBase;
    this.basicAuth = "Basic " + Base64.getEncoder()
        .encodeToString((clientId + ":" + clientSecret).getBytes(StandardCharsets.UTF_8));
    this.nowMillis = nowMillis;
  }

  public PayPalClient(String apiBase, String clientId, String clientSecret) {
    this(apiBase, clientId, clientSecret, System::currentTimeMillis);
  }

  @SuppressWarnings("unchecked")
  private static Map<String, Object> parse(String body) {
    try {
      Object o = body == null || body.isEmpty() ? Map.of() : JSON.readValue(body, Object.class);
      return o instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
    } catch (IOException e) {
      return Map.of();
    }
  }

  synchronized String accessToken(boolean force) throws PayPalException, IOException, InterruptedException {
    if (!force && token != null && nowMillis.getAsLong() < tokenExpiresAt) return token;
    HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(apiBase + "/v1/oauth2/token"))
        .header("Authorization", basicAuth)
        .header("Content-Type", "application/x-www-form-urlencoded")
        .timeout(Duration.ofSeconds(20))
        .POST(HttpRequest.BodyPublishers.ofString("grant_type=client_credentials")).build(),
        HttpResponse.BodyHandlers.ofString());
    Map<String, Object> body = parse(r.body());
    if (r.statusCode() != 200 || body.get("access_token") == null) throw new PayPalException(r.statusCode(), body);
    long ttl = Math.max(0, ((Number) body.getOrDefault("expires_in", 0)).longValue() - 60); // refresh a minute early
    token = String.valueOf(body.get("access_token"));
    tokenExpiresAt = nowMillis.getAsLong() + ttl * 1000;
    return token;
  }

  public Map<String, Object> request(String method, String path, Object body, String requestId)
      throws PayPalException, IOException, InterruptedException {
    for (int attempt = 0; attempt < 2; attempt++) {
      HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(apiBase + path))
          .header("Authorization", "Bearer " + accessToken(attempt > 0))
          .header("Content-Type", "application/json")
          .header("Prefer", "return=representation")
          .timeout(Duration.ofSeconds(20))
          .method(method, body == null ? HttpRequest.BodyPublishers.noBody()
              : HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
      if (requestId != null) b.header("PayPal-Request-Id", requestId); // PayPal's idempotency key
      HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
      if (r.statusCode() == 401 && attempt == 0) continue; // token revoked or expired early: refresh once
      Map<String, Object> out = parse(r.body());
      if (r.statusCode() >= 400) throw new PayPalException(r.statusCode(), out);
      return out;
    }
    throw new PayPalException(401, Map.of());
  }

  private static String seg(String s) {
    return URLEncoder.encode(s, StandardCharsets.UTF_8);
  }

  public Map<String, Object> createOrder(Map<String, Object> body, String requestId) throws Exception {
    return request("POST", "/v2/checkout/orders", body, requestId);
  }

  public Map<String, Object> getOrder(String id) throws Exception {
    return request("GET", "/v2/checkout/orders/" + seg(id), null, null);
  }

  public Map<String, Object> captureOrder(String id, String requestId) throws Exception {
    return request("POST", "/v2/checkout/orders/" + seg(id) + "/capture", Map.of(), requestId);
  }

  public Map<String, Object> refundCapture(String captureId, Map<String, Object> amount, String requestId) throws Exception {
    return request("POST", "/v2/payments/captures/" + seg(captureId) + "/refund", Map.of("amount", amount), requestId);
  }

  /** PayPal's verify-webhook-signature API: PayPal checks the certificate and the signature. */
  public boolean verifyWebhook(Map<String, String> headers, Object event, String webhookId) throws Exception {
    Map<String, Object> body = new LinkedHashMap<>();
    body.put("auth_algo", headers.get("paypal-auth-algo"));
    body.put("cert_url", headers.get("paypal-cert-url"));
    body.put("transmission_id", headers.get("paypal-transmission-id"));
    body.put("transmission_sig", headers.get("paypal-transmission-sig"));
    body.put("transmission_time", headers.get("paypal-transmission-time"));
    body.put("webhook_id", webhookId);
    body.put("webhook_event", event);
    return "SUCCESS".equals(request("POST", "/v1/notifications/verify-webhook-signature", body, null)
        .get("verification_status"));
  }
}
