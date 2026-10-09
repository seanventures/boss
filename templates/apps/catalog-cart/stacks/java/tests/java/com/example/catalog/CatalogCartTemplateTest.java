package com.example.catalog;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.catalog.cart.CartUserResolver;
import com.example.catalog.cart.CatalogStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.StreamSupport;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;

/**
 * The catalog-cart template's own tests, against its HTTP routes (not internals). The signed-in
 * user comes from the app's login (login-oauth's principal when applied); here a test-only
 * resolver reads a header, so merge-on-login is tested without a provider.
 * Run: mvn -q test -Dtest=CatalogCartTemplateTest
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {"CART_SECRET=fake-cart-secret-for-the-template-tests-only", "APP_BASE_URL=https://app.example.test"})
class CatalogCartTemplateTest {
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient HTTP = HttpClient.newHttpClient();

    @TestConfiguration
    static class TestUser {
        /** The signed-in user stands in as a header HERE ONLY; the app's real one is its login's. */
        @Bean
        @Primary
        CartUserResolver headerUser() {
            return req -> req.getHeader("X-Test-User");
        }
    }

    @LocalServerPort
    int port;

    @Autowired
    CatalogStore store;

    @BeforeEach
    void freshCatalog() {
        store.reset(CatalogStore.loadSeed());
    }

    record Res(int status, JsonNode body) {}

    class Browser {
        final Map<String, String> jar = new LinkedHashMap<>();
        final List<String> setCookies = new ArrayList<>();
        String user;

        Browser() {}

        Browser(String user) {
            this.user = user;
        }

        Res request(String method, String path, String body, String contentType) throws Exception {
            HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                    .method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
            if (body != null) b.header("Content-Type", contentType);
            if (user != null) b.header("X-Test-User", user);
            if (!jar.isEmpty()) b.header("Cookie", jar.entrySet().stream().map(e -> e.getKey() + "=" + e.getValue()).collect(Collectors.joining("; ")));
            HttpResponse<String> res = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
            for (String raw : res.headers().allValues("set-cookie")) {
                setCookies.add(raw);
                String pair = raw.split(";", 2)[0];
                String name = pair.substring(0, pair.indexOf('='));
                String value = pair.substring(pair.indexOf('=') + 1);
                if (value.isEmpty() || raw.contains("Max-Age=0")) jar.remove(name); else jar.put(name, value);
            }
            return new Res(res.statusCode(), res.body().isEmpty() ? null : JSON.readTree(res.body()));
        }

        Res get(String path) throws Exception {
            return request("GET", path, null, null);
        }

        Res json(String method, String path, Object body) throws Exception {
            return request(method, path, JSON.writeValueAsString(body), "application/json");
        }

        Res add(String productId, Object quantity) throws Exception {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("product_id", productId);
            body.put("quantity", quantity);
            return json("POST", "/api/cart/items", body);
        }

        String cartId() {
            return jar.get("cart").split("\\.")[0];
        }
    }

    static List<String> ids(JsonNode items) {
        return StreamSupport.stream(items.spliterator(), false).map(n -> n.path("id").asText()).toList();
    }

    static List<String> lines(JsonNode items, String... fields) {
        return StreamSupport.stream(items.spliterator(), false)
                .map(n -> java.util.Arrays.stream(fields).map(f -> n.path(f).asText()).collect(Collectors.joining(":"))).toList();
    }

    @Test
    void theListShowsActiveProductsOnly12ToAPage() throws Exception {
        Res r = new Browser().get("/api/products");
        assertEquals(200, r.status());
        assertEquals(18, r.body().path("total").asInt());
        assertEquals(12, r.body().path("items").size());
        assertEquals(2, r.body().path("pages").asInt());
        for (JsonNode p : r.body().path("items")) {
            assertFalse(p.has("stock"));
            assertTrue(p.path("in_stock").isBoolean());
        }
        List<String> all = ids(new Browser().get("/api/products?page_size=50").body().path("items"));
        assertFalse(all.contains("p19") || all.contains("p20"), "inactive products are hidden");
    }

    @Test
    void searchMatchesNameDescriptionAndSku() throws Exception {
        Browser b = new Browser();
        assertEquals(List.of("p02"), ids(b.get("/api/products?q=MUG").body().path("items")));
        assertEquals(List.of("p04"), ids(b.get("/api/products?q=sku-004").body().path("items")));
        assertEquals(List.of("p02"), ids(b.get("/api/products?q=dishwasher").body().path("items")));
        assertEquals(0, b.get("/api/products?q=withdrawn").body().path("total").asInt());
    }

    @Test
    void paginationBoundsAreEnforced() throws Exception {
        Browser b = new Browser();
        for (String q : List.of("page=0", "page=-1", "page=abc", "page=1.5", "page_size=0", "page_size=51")) {
            assertEquals(400, b.get("/api/products?" + q).status(), q);
        }
        assertEquals(400, b.get("/api/products?q=" + "x".repeat(101)).status());
        List<String> p1 = ids(b.get("/api/products?page=1&page_size=10").body().path("items"));
        List<String> p2 = ids(b.get("/api/products?page=2&page_size=10").body().path("items"));
        assertEquals(10, p1.size());
        assertEquals(8, p2.size());
        Set<String> both = new HashSet<>(p1);
        both.addAll(p2);
        assertEquals(18, both.size());
        assertEquals(0, b.get("/api/products?page=9").body().path("items").size());
    }

    @Test
    void detailAnswersActiveAnd404Otherwise() throws Exception {
        Browser b = new Browser();
        Res one = b.get("/api/products/p05");
        assertEquals(200, one.status());
        assertEquals(3850, one.body().path("price_cents").asLong());
        assertEquals(404, b.get("/api/products/p19").status());
        assertEquals(404, b.get("/api/products/nope").status());
    }

    @Test
    void cartTotalsAreServerComputedInIntegerCents() throws Exception {
        Browser b = new Browser();
        assertEquals(201, b.add("p01", 3).status());
        JsonNode body = b.add("p09", 2).body();
        assertEquals(3 * 1999 + 2 * 999, body.path("subtotal_cents").asLong());
        assertTrue(body.path("subtotal_cents").isIntegralNumber());
        assertEquals(5, body.path("item_count").asInt());
        assertEquals(List.of("p01:5997", "p09:1998"), lines(body.path("items"), "product_id", "line_total_cents"));
        JsonNode cheat = b.json("POST", "/api/cart/items", Map.of("product_id", "p02", "quantity", 1, "price_cents", 1)).body();
        assertTrue(lines(cheat.path("items"), "product_id", "unit_price_cents").contains("p02:1450"), "a client price is ignored");
    }

    @Test
    void quantityBoundsAreEnforced() throws Exception {
        Browser b = new Browser();
        List<Object> bad = new ArrayList<>(List.of(0, -1, 100, 1.5, "2", true));
        bad.add(null);
        for (Object q : bad) {
            assertEquals(400, b.add("p01", q).status(), String.valueOf(q));
        }
        b.add("p12", 90);
        assertEquals(400, b.add("p12", 10).status(), "the line total is bounded too");
    }

    @Test
    void theStockLimitIsEnforced() throws Exception {
        Browser b = new Browser();
        assertEquals(409, b.add("p03", 4).status());
        assertEquals(201, b.add("p03", 3).status());
        Res more = b.add("p03", 1);
        assertEquals(409, more.status());
        assertEquals("insufficient_stock", more.body().path("error").asText());
        assertEquals(409, b.add("p17", 1).status(), "out of stock");
        assertEquals(404, b.add("p19", 1).status(), "an inactive product cannot be added");
    }

    @Test
    void changingAndRemovingLines() throws Exception {
        Browser b = new Browser();
        b.add("p01", 1);
        b.add("p02", 1);
        assertEquals(4 * 1999 + 1450, b.json("PATCH", "/api/cart/items/p01", Map.of("quantity", 4)).body().path("subtotal_cents").asLong());
        assertEquals(404, b.json("PATCH", "/api/cart/items/p03", Map.of("quantity", 4)).status());
        assertEquals(1, b.json("PATCH", "/api/cart/items/p01", Map.of("quantity", 0)).body().path("items").size());
        assertEquals(0, b.request("DELETE", "/api/cart/items/p02", null, null).body().path("items").size());
        assertEquals(404, b.request("DELETE", "/api/cart/items/p02", null, null).status());
    }

    @Test
    void cartsAreIsolatedAndAForgedCookieOpensNothing() throws Exception {
        Browser a = new Browser();
        a.add("p01", 2);
        assertEquals(0, new Browser().get("/api/cart").body().path("items").size());
        Browser forger = new Browser();
        forger.jar.put("cart", a.cartId() + ".forged-signature");
        assertEquals(0, forger.get("/api/cart").body().path("items").size());
        forger.jar.put("cart", a.cartId());
        assertEquals(0, forger.get("/api/cart").body().path("items").size());
        assertEquals(2, a.get("/api/cart").body().path("item_count").asInt());
    }

    @Test
    void theAnonymousCartMergesIntoTheUsersCartOnSignIn() throws Exception {
        Browser before = new Browser("user-1");
        before.add("p01", 1);
        before.add("p02", 1);
        Browser b = new Browser();
        b.add("p01", 2);
        b.add("p03", 3);
        b.user = "user-1"; // signs in
        JsonNode merged = b.get("/api/cart").body();
        List<String> got = new ArrayList<>(lines(merged.path("items"), "product_id", "quantity"));
        got.sort(null);
        assertEquals(List.of("p01:3", "p02:1", "p03:3"), got);
        assertFalse(b.jar.containsKey("cart"), "the anonymous cart cookie is cleared");
        assertEquals(7, new Browser("user-1").get("/api/cart").body().path("item_count").asInt());
        b.user = null;
        assertEquals(0, b.get("/api/cart").body().path("items").size());
    }

    @Test
    void checkoutUsesThePriceNowAndRefusesWhatCannotBeSold() throws Exception {
        Browser b = new Browser();
        assertEquals("cart_empty", b.get("/api/cart/checkout").body().path("error").asText());
        b.add("p01", 2);
        b.add("p02", 1);
        store.setPrice("p01", 2499);
        Res r = b.get("/api/cart/checkout");
        assertEquals(200, r.status());
        assertEquals(b.cartId(), r.body().path("cart_id").asText());
        assertEquals("usd", r.body().path("currency").asText());
        assertEquals(List.of("p01:2499:2:4998", "p02:1450:1:1450"),
                lines(r.body().path("lines"), "product_id", "unit_amount_cents", "quantity", "line_total_cents"));
        assertEquals(6448, r.body().path("total_cents").asLong());
        assertEquals(6448, b.get("/api/cart").body().path("subtotal_cents").asLong());
        store.setActive("p02", false);
        Res refused = b.get("/api/cart/checkout");
        assertEquals(409, refused.status());
        assertEquals("product_unavailable", refused.body().path("error").asText());
        assertEquals("p02", refused.body().path("product_id").asText());
    }

    @Test
    void cartChangesNeedJsonAndTheCookieFlags() throws Exception {
        Browser b = new Browser();
        assertEquals(415, b.request("POST", "/api/cart/items", "product_id=p01&quantity=1", "application/x-www-form-urlencoded").status());
        assertEquals(400, b.request("POST", "/api/cart/items", "{not json", "application/json").status());
        b.add("p01", 1);
        String cookie = b.setCookies.stream().filter(c -> c.startsWith("cart=")).findFirst().orElseThrow().toLowerCase();
        assertTrue(cookie.contains("httponly"), cookie);
        assertTrue(cookie.contains("secure"), cookie);
        assertTrue(cookie.contains("samesite=lax"), cookie);
    }
}
