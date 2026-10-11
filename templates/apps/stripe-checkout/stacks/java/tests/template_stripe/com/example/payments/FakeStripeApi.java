package com.example.payments;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * An in-process fake of the parts of the Stripe API the template calls. The real stripe-java client
 * talks to it (setApiBase), so requests are encoded exactly as in production and no test reaches
 * api.stripe.com.
 */
final class FakeStripeApi implements AutoCloseable {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final SecureRandom RANDOM = new SecureRandom();
  private static final Pattern SESSION_PATH = Pattern.compile("^/v1/checkout/sessions/([A-Za-z0-9_]+)");

  final Map<String, Map<String, Object>> sessions = new ConcurrentHashMap<>();
  final List<Map<String, Object>> refunds = new CopyOnWriteArrayList<>();
  final List<Map<String, String>> requests = new CopyOnWriteArrayList<>();
  final Map<String, Map<String, Object>> idempotency = new ConcurrentHashMap<>();
  volatile boolean down = false;
  private final HttpServer server;

  FakeStripeApi() throws IOException {
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

  private static Map<String, String> form(String raw) {
    Map<String, String> out = new HashMap<>();
    if (raw.isEmpty()) return out;
    for (String pair : raw.split("&")) {
      int i = pair.indexOf('=');
      String k = URLDecoder.decode(i < 0 ? pair : pair.substring(0, i), StandardCharsets.UTF_8);
      String v = i < 0 ? "" : URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8);
      out.put(k, v);
    }
    return out;
  }

  private void send(HttpExchange ex, int status, Object body) throws IOException {
    byte[] data = JSON.writeValueAsBytes(body);
    ex.getResponseHeaders().add("Content-Type", "application/json");
    ex.sendResponseHeaders(status, data.length);
    ex.getResponseBody().write(data);
    ex.close();
  }

  private void handle(HttpExchange ex) throws IOException {
    String raw = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
    Map<String, String> form = form(raw);
    String path = ex.getRequestURI().getPath();
    String method = ex.getRequestMethod();
    String key = ex.getRequestHeaders().getFirst("Idempotency-Key");
    Map<String, String> rec = new HashMap<>(form);
    rec.put("__method", method);
    rec.put("__path", path);
    rec.put("__idempotency_key", key == null ? "" : key);
    requests.add(rec);
    if (down) {
      send(ex, 500, Map.of("error", Map.of("type", "api_error", "message", "down")));
      return;
    }
    String auth = ex.getRequestHeaders().getFirst("Authorization");
    if (auth == null || !auth.startsWith("Bearer ")) {
      send(ex, 401, Map.of("error", Map.of("type", "invalid_request_error")));
      return;
    }
    if (method.equals("POST") && path.equals("/v1/checkout/sessions")) {
      if (key != null && idempotency.containsKey(key)) {
        send(ex, 200, idempotency.get(key));
        return;
      }
      List<Map<String, Object>> items = new ArrayList<>();
      long total = 0;
      for (int i = 0; form.containsKey("line_items[" + i + "][quantity]"); i++) {
        String p = "line_items[" + i + "]";
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("quantity", Long.parseLong(form.get(p + "[quantity]")));
        item.put("unit_amount", Long.parseLong(form.get(p + "[price_data][unit_amount]")));
        item.put("currency", form.get(p + "[price_data][currency]"));
        item.put("name", form.get(p + "[price_data][product_data][name]"));
        total += (Long) item.get("quantity") * (Long) item.get("unit_amount");
        items.add(item);
      }
      String id = "cs_test_" + hex(12);
      Map<String, Object> s = new LinkedHashMap<>();
      s.put("id", id);
      s.put("object", "checkout.session");
      s.put("mode", form.get("mode"));
      s.put("url", "https://checkout.stripe.test/c/pay/" + id);
      s.put("amount_total", total);
      s.put("currency", items.isEmpty() ? null : items.get(0).get("currency"));
      s.put("client_reference_id", form.get("client_reference_id"));
      s.put("metadata", Map.of("order_id", form.getOrDefault("metadata[order_id]", "")));
      s.put("payment_status", "unpaid");
      s.put("status", "open");
      s.put("payment_intent", "pi_" + hex(12));
      s.put("success_url", form.get("success_url"));
      s.put("cancel_url", form.get("cancel_url"));
      s.put("line_items_sent", items);
      sessions.put(id, s);
      if (key != null) idempotency.put(key, s);
      send(ex, 200, s);
      return;
    }
    Matcher m = SESSION_PATH.matcher(path);
    if (method.equals("GET") && m.find()) {
      Map<String, Object> s = sessions.get(m.group(1));
      if (s == null) {
        send(ex, 404, Map.of("error", Map.of("type", "invalid_request_error", "message", "No such session")));
      } else {
        send(ex, 200, s);
      }
      return;
    }
    if (method.equals("POST") && path.equals("/v1/refunds")) {
      Map<String, Object> r = new LinkedHashMap<>();
      r.put("id", "re_" + hex(8));
      r.put("object", "refund");
      r.put("status", "succeeded");
      r.put("payment_intent", form.get("payment_intent"));
      r.put("amount", Long.parseLong(form.getOrDefault("amount", "0")));
      r.put("idempotency_key", key);
      refunds.add(r);
      send(ex, 200, r);
      return;
    }
    send(ex, 404, Map.of("error", Map.of("type", "invalid_request_error", "message", "unknown route")));
  }

  Map<String, Object> pay(String sessionId, Map<String, Object> overrides) {
    Map<String, Object> s = sessions.get(sessionId);
    s.put("payment_status", "paid");
    s.put("status", "complete");
    s.putAll(overrides);
    return new LinkedHashMap<>(s);
  }

  /** A Stripe-Signature header computed the documented way: v1 = HMAC-SHA256(secret, "t.payload"). */
  static String sign(String payload, String secret, long timestamp) {
    try {
      Mac mac = Mac.getInstance("HmacSHA256");
      mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
      byte[] sig = mac.doFinal((timestamp + "." + payload).getBytes(StandardCharsets.UTF_8));
      return "t=" + timestamp + ",v1=" + HexFormat.of().formatHex(sig);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }

  /** A fresh signing secret per test, in Stripe's shape. Never a real one. */
  static String newWebhookSecret() {
    return "whsec_" + hex(24);
  }

  static String newSecretKey() {
    return "sk_test_" + hex(16);
  }

  static String newPublishableKey() {
    return "pk_test_" + hex(16);
  }

  @Override
  public void close() {
    server.stop(0);
  }
}
