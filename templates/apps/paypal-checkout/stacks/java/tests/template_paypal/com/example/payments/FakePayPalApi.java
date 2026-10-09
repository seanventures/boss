package com.example.payments;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.Base64;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * An in-process fake of the PayPal REST endpoints the template calls (OAuth token, Orders v2,
 * captures, refunds, verify-webhook-signature). No test reaches PayPal.
 *
 * <p>Webhook signatures: real PayPal signs with a certificate and its verify API checks it. The fake
 * keeps that contract: an event signed by {@link #sign} with the fake's signing secret (made per
 * test) verifies as SUCCESS; a forged or edited event, or another webhook id, is FAILURE.
 */
final class FakePayPalApi implements AutoCloseable {
  static final ObjectMapper JSON = new ObjectMapper();
  private static final SecureRandom RANDOM = new SecureRandom();
  private static final Pattern CAPTURE = Pattern.compile("^/v2/checkout/orders/([A-Z0-9]+)/capture$");
  private static final Pattern ORDER = Pattern.compile("^/v2/checkout/orders/([A-Z0-9]+)$");
  private static final Pattern REFUND = Pattern.compile("^/v2/payments/captures/([A-Z0-9]+)/refund$");

  final String clientId = "A" + b64(30);
  final String clientSecret = "E" + b64(30);
  final String webhookId = hex(8).toUpperCase();
  final String signingSecret = hex(32);
  final Map<String, Map<String, Object>> orders = new ConcurrentHashMap<>();
  final Set<String> tokens = ConcurrentHashMap.newKeySet();
  final AtomicInteger tokenRequests = new AtomicInteger();
  final List<Map<String, String>> requests = new CopyOnWriteArrayList<>();
  final List<Map<String, Object>> refunds = new CopyOnWriteArrayList<>();
  final List<Map<String, Object>> verifications = new CopyOnWriteArrayList<>();
  final Map<String, Map<String, Object>> captureByRequestId = new ConcurrentHashMap<>();
  volatile boolean down;
  volatile boolean declineNext;
  volatile boolean pendingNext;
  volatile Map<String, Object> captureAmountOverride;
  volatile long expiresIn = 32400;
  private final HttpServer server;

  FakePayPalApi() throws IOException {
    server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    server.createContext("/", this::handle);
    server.start();
  }

  String url() {
    return "http://127.0.0.1:" + server.getAddress().getPort();
  }

  static String hex(int bytes) {
    byte[] b = new byte[bytes];
    RANDOM.nextBytes(b);
    return HexFormat.of().formatHex(b);
  }

  static String b64(int bytes) {
    byte[] b = new byte[bytes];
    RANDOM.nextBytes(b);
    return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
  }

  static String hmac(String secret, String data) {
    try {
      Mac mac = Mac.getInstance("HmacSHA256");
      mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
      return Base64.getEncoder().encodeToString(mac.doFinal(data.getBytes(StandardCharsets.UTF_8)));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }

  static String sha256(String data) {
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(data.getBytes(StandardCharsets.UTF_8)));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }

  /** Transmission headers for {@code eventJson} as PayPal would send them (signed the fake's way). */
  static Map<String, String> sign(String eventJson, String secret, String webhookId) {
    try {
      String canonical = JSON.writeValueAsString(JSON.readValue(eventJson, Object.class));
      String id = UUID.randomUUID().toString();
      String time = Instant.now().toString();
      Map<String, String> h = new LinkedHashMap<>();
      h.put("paypal-auth-algo", "SHA256withRSA");
      h.put("paypal-cert-url", "https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-fake");
      h.put("paypal-transmission-id", id);
      h.put("paypal-transmission-sig", hmac(secret, id + "|" + time + "|" + webhookId + "|" + sha256(canonical)));
      h.put("paypal-transmission-time", time);
      return h;
    } catch (IOException e) {
      throw new IllegalStateException(e);
    }
  }

  void approve(String orderId) {
    orders.get(orderId).put("status", "APPROVED");
  }

  private void send(HttpExchange ex, int status, Object body) throws IOException {
    byte[] data = JSON.writeValueAsBytes(body);
    ex.getResponseHeaders().add("Content-Type", "application/json");
    ex.sendResponseHeaders(status, data.length);
    ex.getResponseBody().write(data);
    ex.close();
  }

  private void error(HttpExchange ex, int status, String name, String issue) throws IOException {
    send(ex, status, Map.of("name", name, "message", name,
        "details", issue == null ? List.of() : List.of(Map.of("issue", issue))));
  }

  @SuppressWarnings("unchecked")
  private void handle(HttpExchange ex) throws IOException {
    String raw = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
    String path = ex.getRequestURI().getRawPath();
    String method = ex.getRequestMethod();
    String rid = ex.getRequestHeaders().getFirst("PayPal-Request-Id");
    String auth = ex.getRequestHeaders().getFirst("Authorization");
    Map<String, String> rec = new HashMap<>();
    rec.put("method", method);
    rec.put("path", path);
    rec.put("request_id", rid == null ? "" : rid);
    requests.add(rec);
    if (down) {
      error(ex, 503, "SERVICE_UNAVAILABLE", null);
      return;
    }
    if (method.equals("POST") && path.equals("/v1/oauth2/token")) {
      tokenRequests.incrementAndGet();
      String expected = "Basic " + Base64.getEncoder().encodeToString((clientId + ":" + clientSecret).getBytes(StandardCharsets.UTF_8));
      if (!expected.equals(auth) || !raw.equals("grant_type=client_credentials")) {
        error(ex, 401, "invalid_client", null);
        return;
      }
      String token = "A21AA" + hex(16);
      tokens.add(token);
      send(ex, 200, Map.of("access_token", token, "token_type", "Bearer", "expires_in", expiresIn));
      return;
    }
    if (auth == null || !tokens.contains(auth.replaceFirst("^Bearer ", ""))) {
      error(ex, 401, "AUTHENTICATION_FAILURE", null);
      return;
    }
    Map<String, Object> body = raw.isEmpty() ? Map.of() : JSON.readValue(raw, Map.class);
    if (method.equals("POST") && path.equals("/v2/checkout/orders")) {
      String id = hex(9).toUpperCase();
      Map<String, Object> order = new LinkedHashMap<>();
      order.put("id", id);
      order.put("status", "PAYER_ACTION_REQUIRED");
      order.put("intent", body.get("intent"));
      order.put("purchase_units", body.get("purchase_units"));
      order.put("request_id", rid);
      order.put("payment_source", body.get("payment_source"));
      order.put("links", List.of(Map.of("rel", "payer-action", "method", "GET",
          "href", "https://www.sandbox.paypal.com/checkoutnow?token=" + id)));
      orders.put(id, order);
      send(ex, 201, order);
      return;
    }
    Matcher m = CAPTURE.matcher(path);
    if (method.equals("POST") && m.matches()) {
      Map<String, Object> order = orders.get(m.group(1));
      if (order == null) {
        error(ex, 404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID");
        return;
      }
      if (rid != null && captureByRequestId.containsKey(rid)) {
        send(ex, 201, captureByRequestId.get(rid));
        return;
      }
      if ("COMPLETED".equals(order.get("status"))) {
        error(ex, 422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED");
        return;
      }
      if (!"APPROVED".equals(order.get("status"))) {
        error(ex, 422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED");
        return;
      }
      if (declineNext) {
        declineNext = false;
        error(ex, 422, "UNPROCESSABLE_ENTITY", "INSTRUMENT_DECLINED");
        return;
      }
      Map<String, Object> unit = ((List<Map<String, Object>>) order.get("purchase_units")).get(0);
      Map<String, Object> amount = (Map<String, Object>) unit.get("amount");
      Map<String, Object> capture = new LinkedHashMap<>();
      capture.put("id", "CAP" + hex(6).toUpperCase());
      capture.put("status", pendingNext ? "PENDING" : "COMPLETED");
      capture.put("amount", captureAmountOverride != null ? captureAmountOverride
          : Map.of("currency_code", amount.get("currency_code"), "value", amount.get("value")));
      capture.put("custom_id", unit.get("custom_id"));
      capture.put("supplementary_data", Map.of("related_ids", Map.of("order_id", order.get("id"))));
      pendingNext = false;
      unit.put("payments", Map.of("captures", List.of(capture)));
      order.put("status", "COMPLETED");
      if (rid != null) captureByRequestId.put(rid, order);
      send(ex, 201, order);
      return;
    }
    m = ORDER.matcher(path);
    if (method.equals("GET") && m.matches()) {
      Map<String, Object> order = orders.get(m.group(1));
      if (order == null) {
        error(ex, 404, "RESOURCE_NOT_FOUND", null);
      } else {
        send(ex, 200, order);
      }
      return;
    }
    m = REFUND.matcher(path);
    if (method.equals("POST") && m.matches()) {
      Map<String, Object> refund = new LinkedHashMap<>();
      refund.put("id", "REF" + hex(6).toUpperCase());
      refund.put("status", "COMPLETED");
      refund.put("capture_id", m.group(1));
      refund.put("amount", body.get("amount"));
      refund.put("request_id", rid);
      refunds.add(refund);
      send(ex, 201, refund);
      return;
    }
    if (method.equals("POST") && path.equals("/v1/notifications/verify-webhook-signature")) {
      verifications.add(body);
      String canonical = JSON.writeValueAsString(body.get("webhook_event"));
      String expected = hmac(signingSecret, body.get("transmission_id") + "|" + body.get("transmission_time") + "|"
          + body.get("webhook_id") + "|" + sha256(canonical));
      boolean ok = webhookId.equals(body.get("webhook_id")) && MessageDigest.isEqual(
          expected.getBytes(StandardCharsets.UTF_8), String.valueOf(body.get("transmission_sig")).getBytes(StandardCharsets.UTF_8));
      send(ex, 200, Map.of("verification_status", ok ? "SUCCESS" : "FAILURE"));
      return;
    }
    error(ex, 404, "NOT_FOUND", null);
  }

  @Override
  public void close() {
    server.stop(0);
  }
}
