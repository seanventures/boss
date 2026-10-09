package com.example.payments;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/**
 * The {@code paypal-checkout} template's own tests, against its HTTP routes (not internals), with
 * PayPal replaced by an in-process fake. No test reaches the network or uses a real credential.
 */
class PayPalCheckoutTest {
  private static final ObjectMapper JSON = new ObjectMapper();
  private final List<Shop> shops = new ArrayList<>();

  @AfterEach
  void closeFakes() {
    shops.forEach(s -> s.fake.close());
  }

  record Res(int status, Map<String, Object> body, String text) {}

  final class Shop {
    final FakePayPalApi fake;
    final OrderStore store = new OrderStore();
    final List<String> paid = new ArrayList<>();
    final PayPalClient paypal;
    final MockMvc mvc;

    Shop(Map<String, String> extraEnv, Catalog catalog, PayPalCheckoutController.AdminCheck admin, AtomicLong clock)
        throws Exception {
      fake = new FakePayPalApi();
      shops.add(this);
      Map<String, String> env = new HashMap<>();
      env.put("PAYPAL_CLIENT_ID", fake.clientId);
      env.put("PAYPAL_CLIENT_SECRET", fake.clientSecret);
      env.put("PAYPAL_WEBHOOK_ID", fake.webhookId);
      env.put("APP_BASE_URL", "http://shop.test");
      env.putAll(extraEnv);
      PayPalSettings settings = PayPalSettings.load(env::get);
      paypal = clock == null ? new PayPalClient(fake.url(), fake.clientId, fake.clientSecret)
          : new PayPalClient(fake.url(), fake.clientId, fake.clientSecret, clock::get);
      PayPalCheckoutController controller = new PayPalCheckoutController(paypal, settings, store,
          catalog == null ? new Catalog.StaticCatalog("usd") : catalog,
          admin == null ? request -> false : admin, order -> paid.add(order.id));
      mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    Shop() throws Exception {
      this(Map.of(), null, null, null);
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

    Map<String, Object> create() throws Exception {
      Res r = post("/api/paypal/orders", Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 2))));
      assertEquals(201, r.status(), r.text());
      return r.body();
    }

    Res webhook(String eventJson, Map<String, String> headers) throws Exception {
      MockHttpServletRequestBuilder req = MockMvcRequestBuilders.post("/webhooks/paypal")
          .contentType(MediaType.APPLICATION_JSON).content(eventJson);
      headers.forEach(req::header);
      return perform(req);
    }

    Res webhook(Map<String, Object> event) throws Exception {
      String json = JSON.writeValueAsString(event);
      return webhook(json, FakePayPalApi.sign(json, fake.signingSecret, fake.webhookId));
    }

    String status(Object orderId) {
      return store.get(String.valueOf(orderId)).orElseThrow().status;
    }

    Map<?, ?> unit(Object paypalOrderId) {
      return (Map<?, ?>) ((List<?>) fake.orders.get(String.valueOf(paypalOrderId)).get("purchase_units")).get(0);
    }

    long captureCalls() {
      return fake.requests.stream().filter(r -> r.get("path").endsWith("/capture")).count();
    }
  }

  static Map<String, Object> captureEvent(String type, Object paypalOrderId, Object orderId, Map<String, Object> amount) {
    Map<String, Object> resource = new LinkedHashMap<>();
    resource.put("id", "CAP" + FakePayPalApi.hex(6).toUpperCase());
    resource.put("status", type.equals("PAYMENT.CAPTURE.COMPLETED") ? "COMPLETED" : "DECLINED");
    resource.put("amount", amount);
    resource.put("custom_id", orderId);
    resource.put("supplementary_data", Map.of("related_ids", Map.of("order_id", paypalOrderId)));
    Map<String, Object> e = new LinkedHashMap<>();
    e.put("id", "WH-" + UUID.randomUUID());
    e.put("event_type", type);
    e.put("resource_type", "capture");
    e.put("resource", resource);
    return e;
  }

  static Map<String, Object> usd(String value) {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("currency_code", "USD");
    m.put("value", value);
    return m;
  }

  // ---- configuration: sandbox by default -------------------------------------------------------

  @Test
  void theSandboxIsTheDefaultAndLiveNeedsPaypalEnvLiveAndAppEnvProduction() {
    Map<String, String> creds = Map.of("PAYPAL_CLIENT_ID", "id", "PAYPAL_CLIENT_SECRET", "x".repeat(8));
    PayPalSettings cfg = PayPalSettings.load(creds::get);
    assertEquals("sandbox", cfg.env);
    assertEquals("https://api-m.sandbox.paypal.com", cfg.apiBase);
    Map<String, String> live = new HashMap<>(creds);
    live.put("PAYPAL_ENV", "live");
    assertThrows(PayPalSettings.ConfigException.class, () -> PayPalSettings.load(live::get));
    live.put("APP_ENV", "preview");
    assertThrows(PayPalSettings.ConfigException.class, () -> PayPalSettings.load(live::get));
    live.put("APP_ENV", "production");
    assertEquals("https://api-m.paypal.com", PayPalSettings.load(live::get).apiBase);
    Map<String, String> evil = new HashMap<>(creds);
    evil.put("PAYPAL_ENV", "https://evil.example");
    assertThrows(PayPalSettings.ConfigException.class, () -> PayPalSettings.load(evil::get));
    assertThrows(PayPalSettings.ConfigException.class, () -> PayPalSettings.load(Map.of("PAYPAL_CLIENT_ID", "id")::get));
    assertFalse(cfg.toString().contains("x".repeat(8)));
  }

  @Test
  void moneyIsConvertedInIntegerMinorUnitsNeverThroughFloats() {
    assertEquals("30.00", PayPalCheckoutController.toValue(3000, "usd"));
    assertEquals("0.01", PayPalCheckoutController.toValue(1, "usd"));
    assertEquals("500", PayPalCheckoutController.toValue(500, "jpy"));
    assertEquals(3000L, PayPalCheckoutController.fromValue("30.00", "usd"));
    assertEquals(3010L, PayPalCheckoutController.fromValue("30.1", "usd"));
    assertEquals(29L, PayPalCheckoutController.fromValue("0.29", "usd"));
    assertNull(PayPalCheckoutController.fromValue("1e3", "usd"));
    assertNull(PayPalCheckoutController.fromValue("-1.00", "usd"));
  }

  @Test
  void theConfigRouteGivesTheJsSdkTheClientIdAndEnvNeverTheSecret() throws Exception {
    Shop shop = new Shop();
    Res r = shop.get("/api/paypal/config");
    assertEquals(Map.of("env", "sandbox", "client_id", shop.fake.clientId, "currency", "USD"), r.body());
    assertFalse(r.text().contains(shop.fake.clientSecret));
  }

  // ---- prices come from the server -------------------------------------------------------------

  @Test
  void creatingAnOrderPricesItOnTheServerWithAnIdempotencyKey() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    Map<String, Object> pp = shop.fake.orders.get(String.valueOf(out.get("paypal_order_id")));
    Map<?, ?> unit = shop.unit(out.get("paypal_order_id"));
    assertEquals("CAPTURE", pp.get("intent"));
    assertEquals(Map.of("currency_code", "USD", "value", "30.00",
        "breakdown", Map.of("item_total", Map.of("currency_code", "USD", "value", "30.00"))), unit.get("amount"));
    assertEquals(List.of(Map.of("name", "Starter pack", "sku", "starter-pack", "quantity", "2",
        "unit_amount", Map.of("currency_code", "USD", "value", "15.00"))), unit.get("items"));
    assertEquals(out.get("order_id"), unit.get("custom_id"));
    assertEquals("create-" + out.get("order_id"), pp.get("request_id"));
    assertEquals("https://www.sandbox.paypal.com/checkoutnow?token=" + out.get("paypal_order_id"), out.get("approve_url"));
    assertEquals("pending", shop.get("/api/orders/" + out.get("order_id")).body().get("status"));
  }

  @Test
  void aTamperedClientPriceAmountOrCurrencyIsIgnored() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> item = new HashMap<>(Map.of("product_id", "pro-pack", "quantity", 1, "price", 1,
        "unit_amount", Map.of("value", "0.01"), "currency", "jpy"));
    Res r = shop.post("/api/paypal/orders", Map.of("amount", "0.01", "currency_code", "JPY", "items", List.of(item)));
    assertEquals(201, r.status());
    Map<?, ?> amount = (Map<?, ?>) shop.unit(r.body().get("paypal_order_id")).get("amount");
    assertEquals("49.00", amount.get("value"));
    assertEquals("USD", amount.get("currency_code"));
  }

  @Test
  void unknownProductsEmptyOrdersAndBadQuantitiesAreRefusedBeforePaypalIsCalled() throws Exception {
    Shop shop = new Shop();
    List<Object> bad = List.of(
        Map.of("items", List.of(Map.of("product_id", "nope", "quantity", 1))), Map.of("items", List.of()), Map.of(),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 0))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 2.5))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", "1"))),
        Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 100))));
    for (Object body : bad) {
      assertEquals(400, shop.post("/api/paypal/orders", body).status(), String.valueOf(body));
    }
    assertTrue(shop.fake.orders.isEmpty());
  }

  @Test
  void anOrderFromACatalogCartCartUsesTheCartsServerPrices() throws Exception {
    Catalog cartCatalog = new Catalog() {
      final Catalog base = new Catalog.StaticCatalog("usd");

      @Override
      public Optional<Catalog.Product> product(String id) {
        return base.product(id);
      }

      @Override
      public Optional<List<Catalog.Line>> cartLines(String cartId, jakarta.servlet.http.HttpServletRequest request) {
        return "cart-abc".equals(cartId)
            ? Optional.of(List.of(new Catalog.Line("tee-1", "T-shirt", 2599, "usd", 3))) : Optional.empty();
      }
    };
    Shop shop = new Shop(Map.of(), cartCatalog, null, null);
    Res r = shop.post("/api/paypal/orders", Map.of("cart_id", "cart-abc", "total", "0.01"));
    assertEquals(201, r.status());
    assertEquals("77.97", ((Map<?, ?>) shop.unit(r.body().get("paypal_order_id")).get("amount")).get("value"));
    assertEquals(400, shop.post("/api/paypal/orders", Map.of("cart_id", "someone-elses")).status());
  }

  // ---- capture: only ours, only once -----------------------------------------------------------

  @Test
  void captureIsRefusedForAPaypalOrderThisAppDidNotCreateWithoutCallingPaypal() throws Exception {
    Shop shop = new Shop();
    int before = shop.fake.requests.size();
    assertEquals(404, shop.post("/api/paypal/orders/5O190127TN364715T/capture", Map.of()).status());
    assertEquals(400, shop.post("/api/paypal/orders/not-an-id/capture", Map.of()).status());
    assertEquals(before, shop.fake.requests.size());
  }

  @Test
  void capturePaysTheOrderOnceAndADoubleCaptureShipsOnce() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    String ppId = String.valueOf(out.get("paypal_order_id"));
    shop.fake.approve(ppId);
    assertEquals(Map.of("order_id", out.get("order_id"), "status", "paid"),
        shop.post("/api/paypal/orders/" + ppId + "/capture", Map.of()).body());
    long n = shop.captureCalls();
    assertEquals(Map.of("order_id", out.get("order_id"), "status", "paid"),
        shop.post("/api/paypal/orders/" + ppId + "/capture", Map.of()).body());
    assertEquals(n, shop.captureCalls(), "an already-paid order is not captured again");
    assertEquals(List.of(out.get("order_id")), shop.paid);
    Map<String, String> req = shop.fake.requests.stream().filter(r -> r.get("path").endsWith("/capture")).findFirst().orElseThrow();
    assertEquals("capture-" + out.get("order_id"), req.get("request_id"));
  }

  @Test
  void aCaptureForADifferentAmountIsHeldForReview() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    shop.fake.approve(String.valueOf(out.get("paypal_order_id")));
    shop.fake.captureAmountOverride = usd("0.01");
    assertEquals("needs_review", shop.post("/api/paypal/orders/" + out.get("paypal_order_id") + "/capture", Map.of()).body().get("status"));
    assertTrue(shop.paid.isEmpty());
  }

  @Test
  void aDeclinedInstrumentAnswers402RestartAndLeavesTheOrderOpen() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    shop.fake.approve(String.valueOf(out.get("paypal_order_id")));
    shop.fake.declineNext = true;
    Res r = shop.post("/api/paypal/orders/" + out.get("paypal_order_id") + "/capture", Map.of());
    assertEquals(402, r.status());
    assertEquals(true, r.body().get("restart"));
    assertEquals("pending", shop.status(out.get("order_id")));
  }

  @Test
  void theReturnPageCapturesServerSideAndReconcilesAnOrderCapturedElsewhere() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    String ppId = String.valueOf(out.get("paypal_order_id"));
    shop.fake.approve(ppId);
    shop.paypal.captureOrder(ppId, "elsewhere"); // another tab, another request id
    Res r = shop.get("/paypal/return?token=" + ppId + "&PayerID=PAYER1");
    assertEquals(200, r.status());
    assertEquals("paid", r.body().get("status"));
    Res page = shop.perform(MockMvcRequestBuilders.get("/paypal/return?token=" + ppId).accept(MediaType.TEXT_HTML));
    assertTrue(page.text().contains("Thank you"));
    assertEquals(List.of(out.get("order_id")), shop.paid);
  }

  // ---- webhooks: verified by PayPal, idempotent ------------------------------------------------

  @Test
  void aWebhookThatFailsPaypalsVerificationIsRefusedAndChangesNothing() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    String json = JSON.writeValueAsString(captureEvent("PAYMENT.CAPTURE.COMPLETED", out.get("paypal_order_id"),
        out.get("order_id"), usd("30.00")));
    assertEquals(400, shop.webhook(json, FakePayPalApi.sign(json, FakePayPalApi.hex(32), shop.fake.webhookId)).status()); // forged
    assertEquals(400, shop.webhook(json, FakePayPalApi.sign(json, shop.fake.signingSecret, "OTHERWEBHOOK")).status());
    Map<String, String> headers = FakePayPalApi.sign(json, shop.fake.signingSecret, shop.fake.webhookId);
    assertEquals(400, shop.webhook(json.replace("\"30.00\"", "\"0.01\""), headers).status()); // signed, then edited
    int asked = shop.fake.verifications.size();
    assertEquals(400, shop.webhook(json, Map.of()).status()); // no PayPal headers: PayPal is not even asked
    assertEquals(asked, shop.fake.verifications.size());
    assertEquals("pending", shop.status(out.get("order_id")));
    Map<String, Object> v = shop.fake.verifications.get(0);
    assertEquals(shop.fake.webhookId, v.get("webhook_id"));
    assertEquals("SHA256withRSA", v.get("auth_algo"));
  }

  @Test
  void aVerifiedCaptureCompletedCompletesAPendingCaptureOnceAndAReplayIsIgnored() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    String ppId = String.valueOf(out.get("paypal_order_id"));
    shop.fake.approve(ppId);
    shop.fake.pendingNext = true;
    assertEquals("pending", shop.post("/api/paypal/orders/" + ppId + "/capture", Map.of()).body().get("status"));
    Map<String, Object> ev = captureEvent("PAYMENT.CAPTURE.COMPLETED", ppId, out.get("order_id"), usd("30.00"));
    Res first = shop.webhook(ev);
    assertEquals(200, first.status());
    assertEquals(false, first.body().get("duplicate"));
    assertEquals("paid", shop.status(out.get("order_id")));
    assertEquals(true, shop.webhook(ev).body().get("duplicate"));
    assertEquals(List.of(out.get("order_id")), shop.paid);
  }

  @Test
  void aVerifiedEventForAnOrderThisAppDidNotCreateFulfilsNothing() throws Exception {
    Shop shop = new Shop();
    Map<String, Object> out = shop.create();
    assertEquals(200, shop.webhook(captureEvent("PAYMENT.CAPTURE.COMPLETED", "9XY12345AB6789012", out.get("order_id"), usd("30.00"))).status());
    assertEquals(200, shop.webhook(captureEvent("PAYMENT.CAPTURE.COMPLETED", out.get("paypal_order_id"), UUID.randomUUID().toString(), usd("30.00"))).status());
    assertEquals("pending", shop.status(out.get("order_id")));
  }

  // ---- the access token ------------------------------------------------------------------------

  @Test
  void theAccessTokenIsCachedRefreshedBeforeItExpiresAndOnceOnA401() throws Exception {
    AtomicLong now = new AtomicLong(1_000_000);
    Shop shop = new Shop(Map.of(), null, null, now);
    shop.create();
    shop.create();
    assertEquals(1, shop.fake.tokenRequests.get());
    now.addAndGet((32400 - 30) * 1000L); // inside the last minute of the token's life
    shop.create();
    assertEquals(2, shop.fake.tokenRequests.get());
    shop.fake.tokens.clear();
    shop.create();
    assertEquals(3, shop.fake.tokenRequests.get());
  }

  // ---- refunds (optional) and outages ----------------------------------------------------------

  @Test
  void refundsAreOffByDefaultNeedAnAdminAndRefundTheRecordedCaptureAmount() throws Exception {
    assertEquals(404, new Shop().post("/api/orders/" + UUID.randomUUID() + "/refund", Map.of()).status());
    Shop shop = new Shop(Map.of("PAYMENTS_REFUNDS_ENABLED", "true"), null,
        request -> "yes".equals(request.getHeader("x-test-admin")), null);
    Map<String, Object> out = shop.create();
    String path = "/api/orders/" + out.get("order_id") + "/refund";
    Map<String, String> admin = Map.of("x-test-admin", "yes");
    assertEquals(409, shop.post(path, Map.of(), admin).status());
    shop.fake.approve(String.valueOf(out.get("paypal_order_id")));
    shop.post("/api/paypal/orders/" + out.get("paypal_order_id") + "/capture", Map.of());
    assertEquals(403, shop.post(path, Map.of("amount", "9999.00")).status());
    Res r = shop.post(path, Map.of("amount", "9999.00"), admin);
    assertEquals(200, r.status());
    assertEquals("refunded", r.body().get("status"));
    Map<String, Object> refund = shop.fake.refunds.get(shop.fake.refunds.size() - 1);
    assertEquals(Map.of("currency_code", "USD", "value", "30.00"), refund.get("amount"));
    assertEquals(shop.store.get(String.valueOf(out.get("order_id"))).orElseThrow().paymentRef, refund.get("capture_id"));
    assertEquals("refund-" + out.get("order_id"), refund.get("request_id"));
  }

  @Test
  void whenPaypalIsUnavailableCreatingAnOrderAnswers502AndTheOrderIsCancelled() throws Exception {
    Shop shop = new Shop();
    shop.fake.down = true;
    Res r = shop.post("/api/paypal/orders", Map.of("items", List.of(Map.of("product_id", "starter-pack", "quantity", 1))));
    shop.fake.down = false;
    assertEquals(502, r.status());
    List<OrderStore.Order> orders = shop.store.all();
    assertEquals(1, orders.size());
    assertEquals("cancelled", orders.get(0).status);
  }
}
