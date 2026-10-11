package com.example.payments;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.stripe.StripeClient;
import com.stripe.exception.SignatureVerificationException;
import com.stripe.exception.StripeException;
import com.stripe.model.checkout.Session;
import com.stripe.net.RequestOptions;
import com.stripe.net.Webhook;
import com.stripe.param.RefundCreateParams;
import com.stripe.param.checkout.SessionCreateParams;
import jakarta.servlet.http.HttpServletRequest;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.function.Consumer;
import java.util.function.Predicate;
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
 * Stripe Checkout for a Spring Boot app (template {@code stripe-checkout}).
 *
 * <pre>
 *   POST /api/checkout             body {cart_id} or {items:[{product_id, quantity}]}; prices from the server
 *   GET  /checkout/success         ?session_id=... retrieves the session FROM STRIPE and fulfils
 *   GET  /checkout/cancel          the buyer came back without paying
 *   POST /webhooks/stripe          signed (Stripe-Signature), idempotent by event id
 *   GET  /api/orders/{id}          the order's status
 *   POST /api/orders/{id}/refund   optional; PAYMENTS_REFUNDS_ENABLED=true AND the admin check
 *   GET  /api/checkout/config      publishable key + mode (for the UI's test-mode banner)
 * </pre>
 *
 * Fulfilment has two idempotent paths: the success page (works on a private preview, where Stripe
 * cannot reach the webhook) and the webhook (authoritative; covers a buyer who closes the tab).
 */
@RestController
public class StripeCheckoutController {
  private static final Logger log = LoggerFactory.getLogger(StripeCheckoutController.class);
  private static final Pattern SESSION_ID = Pattern.compile("^cs_[A-Za-z0-9_]+$");
  private static final ObjectMapper JSON = new ObjectMapper();

  /** Who may refund: wire to the app's auth (e.g. login-oauth + a role). Default: nobody. */
  public interface AdminCheck extends Predicate<HttpServletRequest> {}

  /** Ship the goods here; called exactly once per paid order. */
  public interface OrderPaidHandler extends Consumer<OrderStore.Order> {}

  private final StripeClient stripe;
  private final StripeSettings settings;
  private final OrderStore store;
  private final Catalog catalog;
  private final AdminCheck isAdmin;
  private final OrderPaidHandler onOrderPaid;

  public StripeCheckoutController(StripeClient stripe, StripeSettings settings, OrderStore store, Catalog catalog,
      AdminCheck isAdmin, OrderPaidHandler onOrderPaid) {
    this.stripe = stripe;
    this.settings = settings;
    this.store = store;
    this.catalog = catalog;
    this.isAdmin = isAdmin;
    this.onOrderPaid = onOrderPaid;
  }

  /** The fields fulfilment reads, from Stripe's API object or a verified webhook payload. */
  record SessionView(String id, String paymentStatus, Long amountTotal, String currency, String orderId,
      String paymentIntent) {
    static SessionView of(Session s) {
      Map<String, String> md = s.getMetadata() == null ? Map.of() : s.getMetadata();
      return new SessionView(s.getId(), s.getPaymentStatus(), s.getAmountTotal(), s.getCurrency(),
          md.get("order_id"), s.getPaymentIntent());
    }

    static SessionView of(Map<?, ?> m) {
      Object md = m.get("metadata");
      Object orderId = md instanceof Map<?, ?> mm ? mm.get("order_id") : null;
      Object amount = m.get("amount_total");
      return new SessionView(str(m.get("id")), str(m.get("payment_status")),
          amount instanceof Number n ? n.longValue() : null, str(m.get("currency")), str(orderId),
          str(m.get("payment_intent")));
    }
  }

  private static String str(Object o) {
    return o == null ? null : String.valueOf(o);
  }

  private boolean safeTransition(Optional<OrderStore.Order> order, String to) {
    if (order.isEmpty()) return false;
    try {
      return store.transition(order.get().id, to);
    } catch (OrderStore.TransitionException e) {
      return false; // e.g. a late "failed" after "paid": ignore
    }
  }

  private Optional<OrderStore.Order> orderOfSession(SessionView s) {
    return store.get(s.orderId())
        .filter(o -> o.provider.equals("stripe") && Objects.equals(o.providerRef, s.id()));
  }

  /** Never trusts the session blindly: this app's session for that order, paid, for the priced amount. */
  Optional<OrderStore.Order> fulfil(SessionView s) {
    Optional<OrderStore.Order> found = orderOfSession(s);
    if (found.isEmpty() || !"paid".equals(s.paymentStatus())) return found;
    OrderStore.Order order = found.get();
    if (s.amountTotal() == null || s.amountTotal() != order.amountCents
        || !order.currency.equalsIgnoreCase(String.valueOf(s.currency()))) {
      if (safeTransition(found, "needs_review")) log.warn("stripe: order {} amount mismatch; needs review", order.id);
      return store.get(order.id);
    }
    if (List.of("paid", "refunded", "needs_review").contains(order.status)) return found;
    if (s.paymentIntent() != null) store.setPaymentRef(order.id, s.paymentIntent());
    if (order.status.equals("cancelled")) {
      // Money arrived for an order we had given up on: hold it for a person, never ship silently.
      safeTransition(found, "needs_review");
      return store.get(order.id);
    }
    if (safeTransition(found, "paid")) store.get(order.id).ifPresent(onOrderPaid);
    return store.get(order.id);
  }

  // ---- webhook: the RAW body is verified before anything parses it ---------------------------
  @PostMapping("/webhooks/stripe")
  public ResponseEntity<Map<String, Object>> webhook(@RequestBody(required = false) String payload,
      @RequestHeader(value = "Stripe-Signature", required = false) String signature) {
    Map<?, ?> event;
    try {
      if (payload == null || signature == null || payload.length() > 1_000_000) {
        throw new SignatureVerificationException("missing signature or body", signature);
      }
      Webhook.constructEvent(payload, signature, settings.webhookSecret, settings.webhookToleranceSeconds);
      event = JSON.readValue(payload, Map.class); // verified above
    } catch (Exception e) {
      // Bad signature, stale timestamp or malformed payload: refuse, and do not say which.
      return ResponseEntity.badRequest().body(Map.of("error", "invalid signature"));
    }
    String type = str(event.get("type"));
    Object data = event.get("data");
    Map<?, ?> obj = data instanceof Map<?, ?> d && d.get("object") instanceof Map<?, ?> o ? o : Map.of();
    try {
      boolean fresh = store.processEventOnce(str(event.get("id")), "stripe", type, () -> {
        switch (String.valueOf(type)) {
          case "checkout.session.completed", "checkout.session.async_payment_succeeded" -> fulfil(SessionView.of(obj));
          case "checkout.session.async_payment_failed" -> safeTransition(orderOfSession(SessionView.of(obj)), "failed");
          case "checkout.session.expired" -> safeTransition(orderOfSession(SessionView.of(obj)), "cancelled");
          case "payment_intent.payment_failed" -> {
            Object md = obj.get("metadata");
            String orderId = md instanceof Map<?, ?> m ? str(m.get("order_id")) : null;
            safeTransition(store.get(orderId).filter(o -> o.provider.equals("stripe")), "failed");
          }
          case "charge.refunded" -> {
            // Only a FULL refund marks the order refunded; a partial one stays paid.
            if (Boolean.TRUE.equals(obj.get("refunded")) && obj.get("payment_intent") != null) {
              safeTransition(store.findByPaymentRef("stripe", str(obj.get("payment_intent"))), "refunded");
            }
          }
          default -> { } // acknowledged, not used
        }
        return null;
      });
      return ResponseEntity.ok(Map.of("received", true, "duplicate", !fresh));
    } catch (RuntimeException e) {
      log.error("stripe: webhook {} failed: {}", type, e.getClass().getSimpleName());
      return ResponseEntity.status(500).body(Map.of("error", "processing failed")); // Stripe retries
    }
  }

  @GetMapping("/api/checkout/config")
  public Map<String, Object> config() {
    Map<String, Object> out = new LinkedHashMap<>();
    out.put("mode", settings.mode);
    out.put("publishable_key", settings.publishableKey.isEmpty() ? null : settings.publishableKey);
    return out;
  }

  @PostMapping("/api/checkout")
  public ResponseEntity<Map<String, Object>> checkout(@RequestBody(required = false) Map<String, Object> body,
      HttpServletRequest request) {
    Catalog.Priced priced;
    try {
      priced = Catalog.price(catalog, body, request);
    } catch (Catalog.CheckoutInputException | ArithmeticException e) {
      return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
    }
    Object cartId = body == null ? null : body.get("cart_id");
    OrderStore.Order order = store.createOrder("stripe", priced.lines(), priced.amountCents(), priced.currency(),
        cartId == null ? null : String.valueOf(cartId));
    String base = settings.baseUrl.isEmpty()
        ? request.getScheme() + "://" + request.getServerName() + ":" + request.getServerPort()
        : settings.baseUrl;
    SessionCreateParams.Builder params = SessionCreateParams.builder()
        .setMode(SessionCreateParams.Mode.PAYMENT)
        .setClientReferenceId(order.id)
        .putMetadata("order_id", order.id)
        .setPaymentIntentData(SessionCreateParams.PaymentIntentData.builder().putMetadata("order_id", order.id).build())
        .setSuccessUrl(base + "/checkout/success?session_id={CHECKOUT_SESSION_ID}")
        .setCancelUrl(base + "/checkout/cancel?order_id=" + order.id);
    for (Catalog.Line l : priced.lines()) {
      params.addLineItem(SessionCreateParams.LineItem.builder()
          .setQuantity(l.quantity())
          .setPriceData(SessionCreateParams.LineItem.PriceData.builder()
              .setCurrency(l.currency())
              .setUnitAmount(l.unitAmountCents())
              .setProductData(SessionCreateParams.LineItem.PriceData.ProductData.builder().setName(l.name()).build())
              .build())
          .build());
    }
    Session session;
    try {
      session = stripe.v1().checkout().sessions().create(params.build(),
          RequestOptions.builder().setIdempotencyKey("checkout-" + order.id).build());
    } catch (StripeException e) {
      safeTransition(Optional.of(order), "cancelled");
      log.error("stripe: could not create a Checkout Session: {}", e.getClass().getSimpleName());
      return ResponseEntity.status(502).body(Map.of("error", "the payment provider is unavailable; try again"));
    }
    store.setProviderRef(order.id, session.getId());
    Map<String, Object> out = new LinkedHashMap<>();
    out.put("order_id", order.id);
    out.put("session_id", session.getId());
    out.put("checkout_url", session.getUrl());
    return ResponseEntity.status(201).body(out);
  }

  @GetMapping("/checkout/success")
  public ResponseEntity<?> success(@RequestParam(name = "session_id", required = false) String sessionId,
      @RequestHeader(value = "Accept", required = false) String accept) {
    if (sessionId == null || !SESSION_ID.matcher(sessionId).matches()) {
      return reply(accept, 400, Map.of("error", "missing session"));
    }
    Session session;
    try {
      session = stripe.v1().checkout().sessions().retrieve(sessionId);
    } catch (StripeException e) {
      return reply(accept, 404, Map.of("error", "unknown session"));
    }
    Optional<OrderStore.Order> order = fulfil(SessionView.of(session));
    if (order.isEmpty()) return reply(accept, 404, Map.of("error", "unknown order"));
    return reply(accept, 200, Map.of("order_id", order.get().id, "status", order.get().status));
  }

  @GetMapping("/checkout/cancel")
  public ResponseEntity<?> cancel(@RequestParam(name = "order_id", required = false) String orderId,
      @RequestHeader(value = "Accept", required = false) String accept) {
    Optional<OrderStore.Order> order = store.get(orderId);
    Map<String, Object> body = new LinkedHashMap<>();
    body.put("order_id", order.map(o -> o.id).orElse(null));
    body.put("status", order.map(o -> o.status).orElse(null));
    body.put("cancelled", true);
    return reply(accept, 200, body);
  }

  @GetMapping("/api/orders/{id}")
  public ResponseEntity<Map<String, Object>> order(@PathVariable("id") String id) {
    return store.get(id).filter(o -> o.provider.equals("stripe"))
        .map(o -> ResponseEntity.ok(view(o)))
        .orElse(ResponseEntity.status(404).body(Map.of("error", "not found")));
  }

  @PostMapping("/api/orders/{id}/refund")
  public ResponseEntity<Map<String, Object>> refund(@PathVariable("id") String id, HttpServletRequest request) {
    if (!settings.refundsEnabled) return ResponseEntity.status(404).body(Map.of("error", "refunds are not enabled"));
    if (!isAdmin.test(request)) return ResponseEntity.status(403).body(Map.of("error", "forbidden"));
    Optional<OrderStore.Order> found = store.get(id).filter(o -> o.provider.equals("stripe"));
    if (found.isEmpty()) return ResponseEntity.status(404).body(Map.of("error", "not found"));
    OrderStore.Order order = found.get();
    if (!order.status.equals("paid")) return ResponseEntity.status(409).body(Map.of("error", "order is " + order.status));
    if (order.paymentRef == null) return ResponseEntity.status(409).body(Map.of("error", "order has no recorded payment"));
    try {
      // Full refund of the server-recorded amount; the client never names an amount.
      stripe.v1().refunds().create(
          RefundCreateParams.builder().setPaymentIntent(order.paymentRef).setAmount(order.amountCents).build(),
          RequestOptions.builder().setIdempotencyKey("refund-" + order.id).build());
    } catch (StripeException e) {
      log.error("stripe: refund failed: {}", e.getClass().getSimpleName());
      return ResponseEntity.status(502).body(Map.of("error", "the payment provider refused the refund"));
    }
    safeTransition(found, "refunded");
    return ResponseEntity.ok(Map.of("id", order.id, "status", store.get(order.id).map(o -> o.status).orElse("")));
  }

  private static Map<String, Object> view(OrderStore.Order o) {
    Map<String, Object> out = new LinkedHashMap<>();
    out.put("id", o.id);
    out.put("status", o.status);
    out.put("amount_cents", o.amountCents);
    out.put("currency", o.currency);
    out.put("items", o.items);
    out.put("created_at", o.createdAt);
    out.put("paid_at", o.paidAt);
    out.put("refunded_at", o.refundedAt);
    return out;
  }

  /** JSON for API clients, a small page for a browser coming back from Checkout. */
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
        detail = "We are confirming your payment; this page updates when it is done.";
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
