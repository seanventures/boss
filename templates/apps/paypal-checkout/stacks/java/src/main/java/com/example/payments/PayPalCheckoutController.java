package com.example.payments;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.Consumer;
import java.util.function.Predicate;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.util.HtmlUtils;

/**
 * PayPal Checkout (Orders v2) for a Spring Boot app (template {@code paypal-checkout}).
 *
 * <pre>
 *   POST /api/paypal/orders              body {cart_id} or {items:[{product_id, quantity}]}; server prices
 *   POST /api/paypal/orders/{id}/capture {id} = the PayPal order id; only an order THIS app created, once
 *   GET  /paypal/return                  ?token=...: captures server-side (works on a private preview)
 *   GET  /paypal/cancel                  the buyer came back without paying
 *   POST /webhooks/paypal                verified with PayPal's verify-webhook-signature API, idempotent
 *   GET  /api/orders/{id}                the order's status
 *   POST /api/orders/{id}/refund         optional; PAYMENTS_REFUNDS_ENABLED=true AND the admin check
 *   GET  /api/paypal/config              client id + env + currency for the JS SDK (never the secret)
 * </pre>
 */
@RestController
public class PayPalCheckoutController {
  private static final Logger log = LoggerFactory.getLogger(PayPalCheckoutController.class);
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final Pattern PAYPAL_ORDER_ID = Pattern.compile("^[A-Z0-9]{10,40}$");
  private static final Pattern DECIMAL = Pattern.compile("^(\\d+)(?:\\.(\\d{1,2}))?$");
  /** Currencies PayPal takes without decimals. */
  private static final Set<String> ZERO_DECIMAL = Set.of("jpy", "huf", "twd");
  private static final List<String> SIGNATURE_HEADERS = List.of("paypal-auth-algo", "paypal-cert-url",
      "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time");

  /** Who may refund: wire to the app's auth (e.g. login-oauth + a role). Default: nobody. */
  public interface AdminCheck extends Predicate<HttpServletRequest> {}

  /** Ship the goods here; called exactly once per paid order. */
  public interface OrderPaidHandler extends Consumer<OrderStore.Order> {}

  private final PayPalClient paypal;
  private final PayPalSettings settings;
  private final OrderStore store;
  private final Catalog catalog;
  private final AdminCheck isAdmin;
  private final OrderPaidHandler onOrderPaid;

  public PayPalCheckoutController(PayPalClient paypal, PayPalSettings settings, OrderStore store, Catalog catalog,
      AdminCheck isAdmin, OrderPaidHandler onOrderPaid) {
    this.paypal = paypal;
    this.settings = settings;
    this.store = store;
    this.catalog = catalog;
    this.isAdmin = isAdmin;
    this.onOrderPaid = onOrderPaid;
  }

  /** Integer minor units to PayPal's decimal string, without floating point. */
  static String toValue(long minor, String currency) {
    if (ZERO_DECIMAL.contains(currency)) return Long.toString(minor);
    return (minor / 100) + "." + String.format("%02d", minor % 100);
  }

  /** PayPal's decimal string to integer minor units, or null if it is not a plain amount. */
  static Long fromValue(Object value, String currency) {
    String s = value == null ? "" : String.valueOf(value);
    if (ZERO_DECIMAL.contains(currency)) return s.matches("^\\d+$") ? Long.parseLong(s) : null;
    Matcher m = DECIMAL.matcher(s);
    if (!m.matches()) return null;
    String frac = m.group(2) == null ? "00" : (m.group(2) + "0").substring(0, 2);
    return Long.parseLong(m.group(1)) * 100 + Long.parseLong(frac);
  }

  static Map<String, Object> money(long minor, String currency) {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("currency_code", currency.toUpperCase());
    m.put("value", toValue(minor, currency));
    return m;
  }

  private static Map<?, ?> map(Object o) {
    return o instanceof Map<?, ?> m ? m : Map.of();
  }

  private static String str(Object o) {
    return o == null ? null : String.valueOf(o);
  }

  private static Map<?, ?> firstCapture(Map<?, ?> paypalOrder) {
    if (paypalOrder.get("purchase_units") instanceof List<?> units && !units.isEmpty()
        && map(map(units.get(0)).get("payments")).get("captures") instanceof List<?> captures && !captures.isEmpty()) {
      return map(captures.get(0));
    }
    return null;
  }

  private boolean safeTransition(Optional<OrderStore.Order> order, String to) {
    if (order.isEmpty()) return false;
    try {
      return store.transition(order.get().id, to);
    } catch (OrderStore.TransitionException e) {
      return false; // e.g. a late "denied" after "paid"
    }
  }

  /** Pays the order only for a COMPLETED capture of exactly the priced amount and currency. */
  Optional<OrderStore.Order> applyCapture(Optional<OrderStore.Order> found, Map<?, ?> capture) {
    if (found.isEmpty() || capture == null) return found;
    OrderStore.Order order = found.get();
    String status = str(capture.get("status"));
    if ("DECLINED".equals(status) || "FAILED".equals(status)) {
      safeTransition(found, "failed");
      return store.get(order.id);
    }
    if (!"COMPLETED".equals(status)) return store.get(order.id); // PENDING: the webhook finishes it
    Map<?, ?> amount = map(capture.get("amount"));
    String currency = String.valueOf(amount.get("currency_code")).toLowerCase();
    Long value = fromValue(amount.get("value"), currency);
    if (!currency.equals(order.currency) || value == null || value != order.amountCents) {
      if (safeTransition(found, "needs_review")) log.warn("paypal: order {} amount mismatch; needs review", order.id);
      return store.get(order.id);
    }
    OrderStore.Order current = store.get(order.id).orElseThrow();
    if (List.of("paid", "refunded", "needs_review").contains(current.status)) return Optional.of(current);
    if (capture.get("id") != null) store.setPaymentRef(order.id, str(capture.get("id")));
    if (current.status.equals("cancelled")) {
      safeTransition(found, "needs_review"); // money for an order we gave up on: a person decides
      return store.get(order.id);
    }
    if (safeTransition(found, "paid")) store.get(order.id).ifPresent(onOrderPaid);
    return store.get(order.id);
  }

  private record CaptureResult(int status, Map<String, Object> body) {}

  private CaptureResult captureByPayPalId(String paypalOrderId) {
    if (paypalOrderId == null || !PAYPAL_ORDER_ID.matcher(paypalOrderId).matches()) {
      return new CaptureResult(400, Map.of("error", "bad order id"));
    }
    Optional<OrderStore.Order> found = store.findByProviderRef("paypal", paypalOrderId);
    if (found.isEmpty()) return new CaptureResult(404, Map.of("error", "unknown order")); // never someone else's
    OrderStore.Order order = found.get();
    Map<String, Object> body = new LinkedHashMap<>();
    body.put("order_id", order.id);
    if (!order.status.equals("pending") && !order.status.equals("failed")) {
      body.put("status", order.status); // already paid (or held): no second call to PayPal
      return new CaptureResult(200, body);
    }
    try {
      Optional<OrderStore.Order> out;
      try {
        out = applyCapture(found, firstCapture(paypal.captureOrder(order.providerRef, "capture-" + order.id)));
      } catch (PayPalClient.PayPalException e) {
        if (e.issues().contains("ORDER_ALREADY_CAPTURED")) {
          // Captured elsewhere (another tab, the return page): read the truth back from PayPal.
          out = applyCapture(found, firstCapture(paypal.getOrder(order.providerRef)));
        } else if (e.issues().contains("INSTRUMENT_DECLINED")) {
          // PayPal's guidance: let the buyer choose another funding source; the order stays open.
          body.put("status", store.get(order.id).map(o -> o.status).orElse(""));
          body.put("error", "the payment was declined");
          body.put("restart", true);
          return new CaptureResult(402, body);
        } else {
          throw e;
        }
      }
      body.put("status", out.map(o -> o.status).orElse(""));
      return new CaptureResult(200, body);
    } catch (Exception e) {
      if (e instanceof InterruptedException) Thread.currentThread().interrupt();
      log.error("paypal: capture failed: {}", e.getClass().getSimpleName());
      return new CaptureResult(502, Map.of("error", "the payment provider is unavailable; try again"));
    }
  }

  // ---- webhook: verified by PayPal before anything acts on it ---------------------------------
  @PostMapping("/webhooks/paypal")
  public ResponseEntity<Map<String, Object>> webhook(@RequestBody(required = false) String payload,
      HttpServletRequest request) {
    Map<?, ?> event;
    try {
      Map<String, String> headers = new LinkedHashMap<>();
      for (String h : SIGNATURE_HEADERS) {
        String v = request.getHeader(h);
        if (v == null || v.isEmpty()) throw new IllegalArgumentException("unsigned");
        headers.put(h, v);
      }
      if (settings.webhookId.isEmpty() || payload == null || payload.length() > 1_000_000) {
        throw new IllegalArgumentException("unsigned");
      }
      event = JSON.readValue(payload, Map.class);
      if (!paypal.verifyWebhook(headers, event, settings.webhookId)) throw new IllegalArgumentException("verification failed");
    } catch (Exception e) {
      if (e instanceof InterruptedException) Thread.currentThread().interrupt();
      return ResponseEntity.badRequest().body(Map.of("error", "invalid signature")); // do not say which check
    }
    String type = str(event.get("event_type"));
    Map<?, ?> r = map(event.get("resource"));
    try {
      boolean fresh = store.processEventOnce(str(event.get("id")), "paypal", type, () -> {
        String related = str(map(map(r.get("supplementary_data")).get("related_ids")).get("order_id"));
        // The capture must belong to a PayPal order THIS app created, for the order it names.
        Optional<OrderStore.Order> owned = (related == null ? Optional.<OrderStore.Order>empty()
            : store.findByProviderRef("paypal", related))
            .filter(o -> r.get("custom_id") == null || o.id.equals(str(r.get("custom_id"))));
        switch (String.valueOf(type)) {
          case "PAYMENT.CAPTURE.COMPLETED" -> {
            Map<Object, Object> completed = new LinkedHashMap<>(r);
            completed.put("status", "COMPLETED");
            applyCapture(owned, completed);
          }
          case "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED" -> safeTransition(owned, "failed");
          case "PAYMENT.CAPTURE.REFUNDED" -> {
            if (r.get("links") instanceof List<?> links) {
              for (Object l : links) {
                String href = str(map(l).get("href"));
                if ("up".equals(str(map(l).get("rel"))) && href != null && href.contains("/captures/")) {
                  String captureId = href.substring(href.indexOf("/captures/") + 10).split("[/?]")[0];
                  safeTransition(store.findByPaymentRef("paypal", captureId), "refunded");
                }
              }
            }
          }
          default -> { } // acknowledged, not used
        }
        return null;
      });
      return ResponseEntity.ok(Map.of("received", true, "duplicate", !fresh));
    } catch (RuntimeException e) {
      log.error("paypal: webhook {} failed: {}", type, e.getClass().getSimpleName());
      return ResponseEntity.status(500).body(Map.of("error", "processing failed")); // PayPal retries
    }
  }

  @GetMapping("/api/paypal/config")
  public Map<String, Object> config() {
    Map<String, Object> out = new LinkedHashMap<>();
    out.put("env", settings.env);
    out.put("client_id", settings.clientId);
    out.put("currency", settings.currency.toUpperCase());
    return out;
  }

  @PostMapping("/api/paypal/orders")
  public ResponseEntity<Map<String, Object>> createOrder(@RequestBody(required = false) Map<String, Object> body,
      HttpServletRequest request) {
    Catalog.Priced priced;
    try {
      priced = Catalog.price(catalog, body, request);
    } catch (Catalog.CheckoutInputException | ArithmeticException e) {
      return ResponseEntity.badRequest().body(Map.of("error", String.valueOf(e.getMessage())));
    }
    Object cartId = body == null ? null : body.get("cart_id");
    OrderStore.Order order = store.createOrder("paypal", priced.lines(), priced.amountCents(), priced.currency(),
        cartId == null ? null : String.valueOf(cartId));
    String base = settings.baseUrl.isEmpty()
        ? request.getScheme() + "://" + request.getServerName() + ":" + request.getServerPort()
        : settings.baseUrl;
    Map<String, Object> total = money(priced.amountCents(), priced.currency());
    Map<String, Object> amount = new LinkedHashMap<>(total);
    amount.put("breakdown", Map.of("item_total", total));
    List<Map<String, Object>> items = priced.lines().stream().map(l -> {
      Map<String, Object> item = new LinkedHashMap<>();
      item.put("name", l.name().length() > 127 ? l.name().substring(0, 127) : l.name());
      item.put("sku", l.productId().length() > 127 ? l.productId().substring(0, 127) : l.productId());
      item.put("quantity", Long.toString(l.quantity()));
      item.put("unit_amount", money(l.unitAmountCents(), l.currency()));
      return item;
    }).toList();
    Map<String, Object> unit = new LinkedHashMap<>();
    unit.put("reference_id", order.id);
    unit.put("custom_id", order.id);
    unit.put("amount", amount);
    unit.put("items", items);
    Map<String, Object> experience = new LinkedHashMap<>();
    experience.put("return_url", base + "/paypal/return");
    experience.put("cancel_url", base + "/paypal/cancel");
    experience.put("user_action", "PAY_NOW");
    experience.put("shipping_preference", "NO_SHIPPING");
    Map<String, Object> create = new LinkedHashMap<>();
    create.put("intent", "CAPTURE");
    create.put("purchase_units", List.of(unit));
    create.put("payment_source", Map.of("paypal", Map.of("experience_context", experience)));
    Map<String, Object> created;
    try {
      created = paypal.createOrder(create, "create-" + order.id);
    } catch (Exception e) {
      if (e instanceof InterruptedException) Thread.currentThread().interrupt();
      safeTransition(Optional.of(order), "cancelled");
      log.error("paypal: could not create an order: {}", e.getClass().getSimpleName());
      return ResponseEntity.status(502).body(Map.of("error", "the payment provider is unavailable; try again"));
    }
    store.setProviderRef(order.id, str(created.get("id")));
    String approve = null;
    if (created.get("links") instanceof List<?> links) {
      for (Object l : links) {
        String rel = str(map(l).get("rel"));
        if ("payer-action".equals(rel) || "approve".equals(rel)) approve = str(map(l).get("href"));
      }
    }
    Map<String, Object> out = new LinkedHashMap<>();
    out.put("order_id", order.id);
    out.put("paypal_order_id", created.get("id"));
    out.put("approve_url", approve);
    return ResponseEntity.status(201).body(out);
  }

  @PostMapping("/api/paypal/orders/{id}/capture")
  public ResponseEntity<Map<String, Object>> capture(@PathVariable("id") String id) {
    CaptureResult r = captureByPayPalId(id);
    return ResponseEntity.status(r.status()).body(r.body());
  }

  @GetMapping("/paypal/return")
  public ResponseEntity<?> paypalReturn(@RequestParam(name = "token", required = false) String token,
      @RequestHeader(value = "Accept", required = false) String accept) {
    CaptureResult r = captureByPayPalId(token);
    return reply(accept, r.status(), r.body());
  }

  @GetMapping("/paypal/cancel")
  public ResponseEntity<?> paypalCancel(@RequestParam(name = "token", required = false) String token,
      @RequestHeader(value = "Accept", required = false) String accept) {
    Optional<OrderStore.Order> order = token == null ? Optional.empty() : store.findByProviderRef("paypal", token);
    Map<String, Object> body = new LinkedHashMap<>();
    body.put("order_id", order.map(o -> o.id).orElse(null));
    body.put("status", order.map(o -> o.status).orElse(null));
    body.put("cancelled", true);
    return reply(accept, 200, body);
  }

  @GetMapping("/api/orders/{id}")
  public ResponseEntity<Map<String, Object>> order(@PathVariable("id") String id) {
    return store.get(id).filter(o -> o.provider.equals("paypal"))
        .map(o -> {
          Map<String, Object> out = new LinkedHashMap<>();
          out.put("id", o.id);
          out.put("status", o.status);
          out.put("amount_cents", o.amountCents);
          out.put("currency", o.currency);
          out.put("items", o.items);
          out.put("created_at", o.createdAt);
          out.put("paid_at", o.paidAt);
          out.put("refunded_at", o.refundedAt);
          return ResponseEntity.ok(out);
        })
        .orElse(ResponseEntity.status(404).body(Map.of("error", "not found")));
  }

  @PostMapping("/api/orders/{id}/refund")
  public ResponseEntity<Map<String, Object>> refund(@PathVariable("id") String id, HttpServletRequest request) {
    if (!settings.refundsEnabled) return ResponseEntity.status(404).body(Map.of("error", "refunds are not enabled"));
    if (!isAdmin.test(request)) return ResponseEntity.status(403).body(Map.of("error", "forbidden"));
    Optional<OrderStore.Order> found = store.get(id).filter(o -> o.provider.equals("paypal"));
    if (found.isEmpty()) return ResponseEntity.status(404).body(Map.of("error", "not found"));
    OrderStore.Order order = found.get();
    if (!order.status.equals("paid")) return ResponseEntity.status(409).body(Map.of("error", "order is " + order.status));
    if (order.paymentRef == null) return ResponseEntity.status(409).body(Map.of("error", "order has no recorded payment"));
    try {
      // Full refund of the server-recorded amount; the client never names an amount.
      paypal.refundCapture(order.paymentRef, money(order.amountCents, order.currency), "refund-" + order.id);
    } catch (Exception e) {
      if (e instanceof InterruptedException) Thread.currentThread().interrupt();
      log.error("paypal: refund failed: {}", e.getClass().getSimpleName());
      return ResponseEntity.status(502).body(Map.of("error", "the payment provider refused the refund"));
    }
    safeTransition(found, "refunded");
    return ResponseEntity.ok(Map.of("id", order.id, "status", store.get(order.id).map(o -> o.status).orElse("")));
  }

  /** JSON for API clients, a small page for a browser coming back from PayPal. */
  private static ResponseEntity<?> reply(String accept, int status, Map<String, Object> body) {
    String a = accept == null ? "" : accept;
    if (a.contains("text/html") && !a.contains("application/json")) {
      String title;
      String detail;
      if (body.get("error") != null) {
        title = "Payment problem";
        detail = String.valueOf(body.get("error"));
      } else if (Boolean.TRUE.equals(body.get("cancelled"))) {
        title = "Payment cancelled";
        detail = "You have not been charged.";
      } else if ("paid".equals(body.get("status"))) {
        title = "Thank you";
        detail = "Your order is confirmed.";
      } else {
        title = "Payment processing";
        detail = "We are confirming your payment.";
      }
      String order = body.get("order_id") == null ? ""
          : "<p>Order <code>" + HtmlUtils.htmlEscape(String.valueOf(body.get("order_id"))) + "</code></p>";
      String html = "<!doctype html><meta charset=\"utf-8\"><title>" + HtmlUtils.htmlEscape(title) + "</title><main><h1>"
          + HtmlUtils.htmlEscape(title) + "</h1><p>" + HtmlUtils.htmlEscape(detail) + "</p>" + order
          + "<p><a href=\"/\">Back to the shop</a></p></main>";
      return ResponseEntity.status(status).contentType(MediaType.TEXT_HTML).body(html);
    }
    return ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON).body(body);
  }
}
