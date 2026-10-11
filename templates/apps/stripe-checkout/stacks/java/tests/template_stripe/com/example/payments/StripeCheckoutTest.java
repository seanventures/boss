package com.example.payments;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.stripe.StripeClient;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/**
 * The {@code stripe-checkout} template's own tests, against its HTTP routes (not internals), with
 * Stripe replaced by an in-process fake. No test reaches the network or uses a real key.
 */
class StripeCheckoutTest {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static FakeStripeApi fake;

  @BeforeAll
  static void start() throws Exception {
    fake = new FakeStripeApi();
  }

  @AfterAll
  static void stop() {
    fake.close();
  }

  record Res(int status, Map<String, Object> body, String text) {}

  /** One app per test: its own store, keys and webhook signing secret. */
  static final class Shop {
    final String webhookSecret = FakeStripeApi.newWebhookSecret();
    final OrderStore store = new OrderStore();
    final List<String> paid = new ArrayList<>();
    final MockMvc mvc;

    Shop(Map<String, String> extraEnv, Catalog catalog, StripeCheckoutController.AdminCheck admin) {
      Map<String, String> env = new HashMap<>();
      env.put("STRIPE_SECRET_KEY", FakeStripeApi.newSecretKey());
      env.put("STRIPE_PUBLISHABLE_KEY", FakeStripeApi.newPublishableKey());
      env.put("STRIPE_WEBHOOK_SECRET", webhookSecret);
      env.put("APP_BASE_URL", "http://shop.test");
      env.putAll(extraEnv);
      StripeSettings settings = StripeSettings.load(env::get);
      StripeClient client = StripeClient.builder().setApiKey(settings.secretKey).setApiBase(fake.url())
          .setMaxNetworkRetries(0).build();
      StripeCheckoutController controller = new StripeCheckoutController(client, settings, store,
          catalog == null ? new Catalog.StaticCatalog("usd") : catalog,
          admin == null ? request -> false : admin, order -> paid.add(order.id));
      mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    Shop() {
      this(Map.of(), null, null);
    }

    Res perform(MockHttpServletRequestBuilder req) throws Exception {
      MockHttpServletResponse r = mvc.perform(req).andReturn().getResponse();
      String text = r.getContentAsString();
      Map<String, Object> body = text.startsWith("{") ? JSON.readValue(text, Map.class) : Map.of();
      return new Res(r.getStatus(), body, text);
    }

    Res post(String path, Object body, Map<String, String> headers) throws Exception {
      MockHttpServletRequestBuilder req = MockMvcRequestBuilders.post(path)
          .contentType(MediaType.APPLICATION_JSON).content(JSON.writeValueAsString(body));
      headers.forEach(req::header);
      return perform(req);
    }

    Res post(String path, Object body) throws Exception {
      return post(path, body, Map.of());
    }

    Res get(String path) throws Exception {
      return perform(MockMvcRequestBuilders.get(path).accept(MediaType.APPLICATION_JSON));
    }

    Map<String, Object> checkout(List<Map<String, Object>> items) throws Exception {
      Res r = post("/api/checkout", Map.of("items", items));
      assertEquals(201, r.status(), r.text());
      return r.body();
    }

    Map<String, Object> checkout() throws Exception {
      return checkout(List.of(Map.of("product_id", "starter-pack", "quantity", 2)));
    }

    Res webhook(Map<String, Object> event, String secret, Long timestamp, String header) throws Exception {
      String payload = JSON.writeValueAsString(event);
      MockHttpServletRequestBuilder req = MockMvcRequestBuilders.post("/webhooks/stripe")
          .contentType(MediaType.APPLICATION_JSON).content(payload);
      long ts = timestamp == null ? System.currentTimeMillis() / 1000 : timestamp;
      String sig = header != null ? header : FakeStripeApi.sign(payload, secret == null ? webhookSecret : secret, ts);
      if (!sig.isEmpty()) req.header("Stripe-Signature", sig);
      return perform(req);
    }

    Res webhook(Map<String, Object> event) throws Exception {
      return webhook(event, null, null, null);
    }

    String status(Object orderId) {
      return store.get(String.valueOf(orderId)).orElseThrow().status;
    }
  }

  static Map<String, Object> event(String type, Map<String, Object> obj) {
    Map<String, Object> e = new LinkedHashMap<>();
    e.put("id", "evt_" + FakeStripeApi.hex(10));
    e.put("object", "event");
    e.put("type", type);
    e.put("created", System.currentTimeMillis() / 1000);
    e.put("data", Map.of("object", obj));
    return e;
  }

  // ---- configuration: test mode by default -----------------------------------------------------

  @Test
  void aLiveKeyIsRefusedInTestModeATestKeyInLiveModeAndErrorsNeverEchoTheKey() {
    String live = "sk_" + "live_" + FakeStripeApi.hex(16);
    StripeSettings.ConfigException e = assertThrows(StripeSettings.ConfigException.class,
        () -> StripeSettings.load(Map.of("STRIPE_SECRET_KEY", live, "STRIPE_WEBHOOK_SECRET", "x")::get));
    assertTrue(e.getMessage().contains("not a test-mode secret key"));
    assertFalse(e.getMessage().contains(live));
    String test = FakeStripeApi.newSecretKey();
    assertThrows(StripeSettings.ConfigException.class, () -> StripeSettings.load(
        Map.of("STRIPE_MODE", "live", "STRIPE_SECRET_KEY", test, "STRIPE_WEBHOOK_SECRET", "x")::get));
    assertThrows(StripeSettings.ConfigException.class, () -> StripeSettings.load(Map.of("STRIPE_SECRET_KEY", test,
        "STRIPE_WEBHOOK_SECRET", "x", "STRIPE_PUBLISHABLE_KEY", "pk_" + "live_" + FakeStripeApi.hex(16))::get));
    assertThrows(StripeSettings.ConfigException.class, () -> StripeSettings.load(Map.of("STRIPE_SECRET_KEY", test)::get));
    StripeSettings ok = StripeSettings.load(Map.of("STRIPE_SECRET_KEY", test, "STRIPE_WEBHOOK_SECRET", "x")::get);
    assertEquals("test", ok.mode);
    assertFalse(ok.toString().contains(test));
  }

  @Test
  void theUiConfigRouteSaysTestModeAndNeverExposesTheSecretKey() throws Exception {
    Res r = new Shop().get("/api/checkout/config");
    assertEquals(200, r.status());
    assertEquals("test", r.body().get("mode"));
    assertTrue(String.valueOf(r.body().get("publishable_key")).startsWith("pk_test_"));
    assertFalse(r.text().contains("sk_"));
  }

  // ---- prices come from the server -------------------------------------------------------------

  @Test
  void checkoutCreatesACheckoutSessionPricedByTheServer() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    Map<String, Object> session = fake.sessions.get(out.get("session_id"));
    assertEquals(List.of(Map.of("quantity", 2L, "unit_amount", 1500L, "currency", "usd", "name", "Starter pack")),
        session.get("line_items_sent"));
    assertEquals(3000L, session.get("amount_total"));
    assertEquals(Map.of("order_id", out.get("order_id")), session.get("metadata"));
    assertEquals("http://shop.test/checkout/success?session_id={CHECKOUT_SESSION_ID}", session.get("success_url"));
    assertEquals(session.get("url"), out.get("checkout_url"));
    Res order = shop.get("/api/orders/" + out.get("order_id"));
    assertEquals("pending", order.body().get("status"));
    assertEquals(3000, ((Number) order.body().get("amount_cents")).intValue());
    Map<String, String> req = fake.requests.stream()
        .filter(r -> "/v1/checkout/sessions".equals(r.get("__path")) && out.get("order_id").equals(r.get("metadata[order_id]")))
        .findFirst().orElseThrow();
    assertEquals("checkout-" + out.get("order_id"), req.get("__idempotency_key"));
  }

  @Test
  void aTamperedClientPriceAmountOrCurrencyIsIgnored() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> item = new HashMap<>(Map.of("product_id", "pro-pack", "quantity", 1, "price", 1,
        "unit_amount", 1, "unit_amount_cents", 1, "amount", 1, "currency", "jpy", "name", "Free"));
    Res r = shop.post("/api/checkout", Map.of("amount", 1, "currency", "jpy", "items", List.of(item)));
    assertEquals(201, r.status());
    Map<String, Object> session = fake.sessions.get(r.body().get("session_id"));
    assertEquals(List.of(Map.of("quantity", 1L, "unit_amount", 4900L, "currency", "usd", "name", "Pro pack")),
        session.get("line_items_sent"));
    assertEquals(4900L, shop.store.get(String.valueOf(r.body().get("order_id"))).orElseThrow().amountCents);
  }

  @Test
  void unknownProductsEmptyOrdersAndBadQuantitiesAreRefusedBeforeStripeIsCalled() throws Exception {
    Shop shop = new Shop();
    int before = fake.sessions.size();
    List<Object> bad = List.of(
        Map.of("items", List.of(Map.of("product_id", "no-such-thing", "quantity", 1))),
        Map.of("items", List.of()), Map.of(), List.of(),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 0))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 1.5))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", "2"))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", true))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 100))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", -1))));
    for (Object body : bad) {
      assertEquals(400, shop.post("/api/checkout", body).status(), String.valueOf(body));
    }
    assertEquals(before, fake.sessions.size());
  }

  @Test
  void checkoutFromACatalogCartCartUsesServerPricesAndAPriceChangeIsReflected() throws Exception {
    long[] price = {2500};
    Catalog cartCatalog = new Catalog() {
      final Catalog base = new Catalog.StaticCatalog("usd");

      @Override
      public Optional<Catalog.Product> product(String id) {
        return base.product(id);
      }

      @Override
      public Optional<List<Catalog.Line>> cartLines(String cartId, jakarta.servlet.http.HttpServletRequest request) {
        if (!"cart-abc".equals(cartId)) return Optional.empty();
        return Optional.of(List.of(new Catalog.Line("tee-1", "T-shirt", price[0], "usd", 3)));
      }
    };
    Shop shop = new Shop(Map.of(), cartCatalog, null);
    Res first = shop.post("/api/checkout", Map.of("cart_id", "cart-abc", "total", 1));
    assertEquals(201, first.status());
    assertEquals(7500L, fake.sessions.get(first.body().get("session_id")).get("amount_total"));
    price[0] = 2700;
    Res second = shop.post("/api/checkout", Map.of("cart_id", "cart-abc"));
    assertEquals(8100L, fake.sessions.get(second.body().get("session_id")).get("amount_total"));
    assertEquals(400, shop.post("/api/checkout", Map.of("cart_id", "someone-elses")).status());
  }

  /** Declared here exactly as catalog-cart declares it (the payment template never imports it). */
  public static class CatalogError extends RuntimeException {
    public final int status;
    public final String code;

    public CatalogError(int status, String code) {
      super(code);
      this.status = status;
      this.code = code;
    }
  }

  /** Not catalog-cart's class, though it has the same fields. */
  public static class Lookalike extends RuntimeException {
    public final int status = 409;
    public final String code = "product_unavailable";
  }

  /** catalog-cart's provider refusing a cart it cannot sell now. */
  static final class RefusingCatalog implements Catalog {
    final Catalog base = new Catalog.StaticCatalog("usd");
    RuntimeException failure = new CatalogError(409, "product_unavailable");

    @Override
    public Optional<Catalog.Product> product(String id) {
      if ("gone".equals(id)) throw new CatalogError(404, "product_not_found");
      return base.product(id);
    }

    @Override
    public Optional<List<Catalog.Line>> cartLines(String cartId, jakarta.servlet.http.HttpServletRequest request) {
      throw failure;
    }
  }

  /** The payment template maps catalog-cart's CatalogError itself (by name and shape): a 400 with
   *  its code, never a 500, and no order or Stripe call. Anything else stays the app's own error. */
  @Test
  void aCartCatalogCartCannotSellIsA400WithItsCodeBeforeStripeIsCalled() throws Exception {
    RefusingCatalog catalog = new RefusingCatalog();
    Shop shop = new Shop(Map.of(), catalog, null);
    int before = fake.requests.size();
    Res cart = shop.post("/api/checkout", Map.of("cart_id", "cart-abc"));
    assertEquals(400, cart.status(), cart.text());
    assertEquals(Map.of("error", "product_unavailable"), cart.body());
    Res items = shop.post("/api/checkout", Map.of("items", List.of(Map.of("product_id", "gone", "quantity", 1))));
    assertEquals(400, items.status(), items.text());
    assertEquals(Map.of("error", "product_not_found"), items.body());
    catalog.failure = new CatalogError(409, "cart_empty");
    assertEquals(Map.of("error", "cart_empty"), shop.post("/api/checkout", Map.of("cart_id", "cart-abc")).body());
    for (RuntimeException failure : List.of(new IllegalStateException("database is down"),
        new CatalogError(503, "catalog_down"), new Lookalike())) {
      catalog.failure = failure;
      Exception thrown = assertThrows(Exception.class, () -> shop.post("/api/checkout", Map.of("cart_id", "cart-abc")));
      assertEquals(failure, thrown.getCause(), "not mapped to the client's 400");
    }
    assertEquals(before, fake.requests.size(), "Stripe must not be called");
  }

  // ---- webhooks: signed, fresh, idempotent -----------------------------------------------------

  @Test
  void aWebhookWithABadForgedOrMissingSignatureIsRefusedAndChangesNothing() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    Map<String, Object> session = fake.pay((String) out.get("session_id"), Map.of());
    Map<String, Object> ev = event("checkout.session.completed", session);
    assertEquals(400, shop.webhook(ev, FakeStripeApi.newWebhookSecret(), null, null).status()); // forged
    assertEquals(400, shop.webhook(ev, null, null, "t=1,v1=deadbeef").status());
    assertEquals(400, shop.webhook(ev, null, null, "").status()); // no header
    // A valid signature over a DIFFERENT body (the attacker edits the amount after signing).
    String sig = FakeStripeApi.sign(JSON.writeValueAsString(ev), shop.webhookSecret, System.currentTimeMillis() / 1000);
    Map<String, Object> edited = new LinkedHashMap<>(session);
    edited.put("amount_total", 1);
    Map<String, Object> tampered = new LinkedHashMap<>(ev);
    tampered.put("data", Map.of("object", edited));
    Res r = shop.perform(MockMvcRequestBuilders.post("/webhooks/stripe").contentType(MediaType.APPLICATION_JSON)
        .content(JSON.writeValueAsString(tampered)).header("Stripe-Signature", sig));
    assertEquals(400, r.status());
    assertEquals("pending", shop.status(out.get("order_id")));
    assertTrue(shop.paid.isEmpty());
  }

  @Test
  void aWebhookSignedMoreThanFiveMinutesAgoIsRefused() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    Map<String, Object> ev = event("checkout.session.completed", fake.pay((String) out.get("session_id"), Map.of()));
    assertEquals(400, shop.webhook(ev, null, System.currentTimeMillis() / 1000 - 600, null).status());
    assertEquals("pending", shop.status(out.get("order_id")));
  }

  @Test
  void aSignedCompletedEventFulfilsOnceAndAReplayedEventIsProcessedOnce() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    Map<String, Object> session = fake.pay((String) out.get("session_id"), Map.of());
    Map<String, Object> ev = event("checkout.session.completed", session);
    Res first = shop.webhook(ev);
    assertEquals(200, first.status());
    assertEquals(false, first.body().get("duplicate"));
    assertEquals("paid", shop.status(out.get("order_id")));
    Res again = shop.webhook(ev);
    assertEquals(200, again.status());
    assertEquals(true, again.body().get("duplicate"));
    assertEquals(200, shop.webhook(event("checkout.session.completed", session)).status()); // new id, same session
    assertEquals(List.of(out.get("order_id")), shop.paid);
  }

  @Test
  void theSuccessPageFulfilsFromStripeIdempotentWithTheWebhook() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    String path = "/checkout/success?session_id=" + out.get("session_id");
    Res pending = shop.get(path);
    assertEquals(200, pending.status());
    assertEquals("pending", pending.body().get("status"));
    Map<String, Object> session = fake.pay((String) out.get("session_id"), Map.of());
    assertEquals("paid", shop.get(path).body().get("status"));
    assertEquals("paid", shop.get(path).body().get("status"));
    assertEquals(200, shop.webhook(event("checkout.session.completed", session)).status());
    assertEquals(List.of(out.get("order_id")), shop.paid);
    Res page = shop.perform(MockMvcRequestBuilders.get(path).accept(MediaType.TEXT_HTML));
    assertTrue(page.text().contains("Thank you"));
  }

  @Test
  void theSuccessPageRefusesAMalformedOrUnknownSessionId() throws Exception {
    Shop shop = new Shop();
    assertEquals(400, shop.get("/checkout/success?session_id=../../v1/refunds").status());
    assertEquals(400, shop.get("/checkout/success").status());
    assertEquals(404, shop.get("/checkout/success?session_id=cs_test_nope").status());
  }

  @Test
  void aPaidSessionForADifferentAmountIsHeldForReview() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    Map<String, Object> session = fake.pay((String) out.get("session_id"), Map.of("amount_total", 100L));
    assertEquals(200, shop.webhook(event("checkout.session.completed", session)).status());
    assertEquals("needs_review", shop.status(out.get("order_id")));
    assertTrue(shop.paid.isEmpty());
  }

  @Test
  void anEventNamingAnOrderWithASessionThisAppDidNotCreateFulfilsNothing() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    Map<String, Object> other = Map.of("id", "cs_test_" + FakeStripeApi.hex(8), "object", "checkout.session",
        "payment_status", "paid", "amount_total", 3000, "currency", "usd",
        "metadata", Map.of("order_id", out.get("order_id")));
    assertEquals(200, shop.webhook(event("checkout.session.completed", other)).status());
    assertEquals("pending", shop.status(out.get("order_id")));
  }

  @Test
  void orderStateMachine() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.checkout();
    String sid = (String) out.get("session_id");
    Map<String, Object> pi = Map.of("id", fake.sessions.get(sid).get("payment_intent"), "object", "payment_intent",
        "metadata", Map.of("order_id", out.get("order_id")));
    shop.webhook(event("payment_intent.payment_failed", pi));
    assertEquals("failed", shop.status(out.get("order_id")));
    shop.webhook(event("checkout.session.completed", fake.pay(sid, Map.of())));
    assertEquals("paid", shop.status(out.get("order_id")));
    assertEquals(200, shop.webhook(event("payment_intent.payment_failed", pi)).status());
    assertEquals("paid", shop.status(out.get("order_id")));

    Map<String, Object> second = shop.checkout();
    String sid2 = (String) second.get("session_id");
    shop.webhook(event("checkout.session.expired", new LinkedHashMap<>(fake.sessions.get(sid2))));
    assertEquals("cancelled", shop.status(second.get("order_id")));
    // Paid after it was cancelled: held for a person, never shipped silently.
    shop.webhook(event("checkout.session.completed", fake.pay(sid2, Map.of())));
    assertEquals("needs_review", shop.status(second.get("order_id")));
    assertEquals(List.of(out.get("order_id")), shop.paid);
  }

  // ---- refunds (optional) ----------------------------------------------------------------------

  @Test
  void refundsAreOffByDefaultNeedAnAdminAndRefundTheRecordedAmount() throws Exception {
    assertEquals(404, new Shop().post("/api/orders/" + UUID.randomUUID() + "/refund", Map.of()).status());
    Shop shop = new Shop(Map.of("PAYMENTS_REFUNDS_ENABLED", "true"), null,
        request -> "yes".equals(request.getHeader("x-test-admin")));
    Map<String, Object> out = shop.checkout();
    String path = "/api/orders/" + out.get("order_id") + "/refund";
    Map<String, String> admin = Map.of("x-test-admin", "yes");
    assertEquals(409, shop.post(path, Map.of(), admin).status()); // not paid
    Map<String, Object> session = fake.pay((String) out.get("session_id"), Map.of());
    shop.webhook(event("checkout.session.completed", session));
    assertEquals(403, shop.post(path, Map.of("amount", 999999)).status());
    Res r = shop.post(path, Map.of("amount", 999999), admin);
    assertEquals(200, r.status());
    assertEquals("refunded", r.body().get("status"));
    Map<String, Object> refund = fake.refunds.get(fake.refunds.size() - 1);
    assertEquals(3000L, refund.get("amount"));
    assertEquals(session.get("payment_intent"), refund.get("payment_intent"));
    assertEquals("refund-" + out.get("order_id"), refund.get("idempotency_key"));
    Map<String, Object> charge = Map.of("id", "ch_1", "object", "charge", "refunded", true,
        "payment_intent", session.get("payment_intent"));
    assertEquals(200, shop.webhook(event("charge.refunded", charge)).status());
    assertEquals("refunded", shop.status(out.get("order_id")));
  }

  @Test
  void whenStripeIsUnavailableCheckoutAnswers502AndTheOrderIsCancelled() throws Exception {
    Shop shop = new Shop();
    fake.down = true;
    Res r;
    try {
      r = shop.post("/api/checkout", Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 1))));
    } finally {
      fake.down = false;
    }
    assertEquals(502, r.status());
    List<OrderStore.Order> orders = shop.store.all();
    assertEquals(1, orders.size());
    assertEquals("cancelled", orders.get(0).status);
  }
}
