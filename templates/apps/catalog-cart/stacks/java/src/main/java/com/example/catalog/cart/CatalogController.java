package com.example.catalog.cart;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseCookie;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * The catalog-cart routes:
 * <pre>
 *   GET    /api/products?q=&page=&page_size=  search the active products (page_size <= 50)
 *   GET    /api/products/{id}                 one active product (404 otherwise)
 *   GET    /api/cart                          the caller's cart with server-computed totals
 *   POST   /api/cart/items                    {product_id, quantity} adds to the cart
 *   PATCH  /api/cart/items/{product_id}       {quantity} sets it (0 removes)
 *   DELETE /api/cart/items/{product_id}       removes the line
 *   GET    /api/cart/checkout                 the checkout contract for the caller's cart
 * </pre>
 * An anonymous cart is found by a signed {@code cart} cookie; a signed-in user's
 * ({@link CartUserResolver}) by user, and the anonymous cart merges into it on the first request
 * after sign-in.
 */
@RestController
public class CatalogController {
    private static final String COOKIE = "cart";
    private static final int MAX_PAGE_SIZE = 50;
    private static final int MAX_QUERY = 100;

    private final CatalogStore store;
    private final CartUserResolver users;
    private final byte[] secret;
    private final boolean secureCookies;

    public CatalogController(CatalogStore store, CartUserResolver users,
                             @Value("${CART_SECRET:}") String cartSecret,
                             @Value("${APP_BASE_URL:http://localhost:3000}") String baseUrl) {
        if (cartSecret.trim().length() < 32) {
            throw new IllegalStateException("CART_SECRET must be set to at least 32 characters (BOSS generates one)");
        }
        this.store = store;
        this.users = users;
        this.secret = cartSecret.trim().getBytes(StandardCharsets.UTF_8);
        this.secureCookies = baseUrl.startsWith("https://");
    }

    // ---- the signed cart cookie -----------------------------------------------------------------
    private String sign(String id) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(id.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private String readCookie(HttpServletRequest req) {
        if (req.getCookies() == null) return null;
        for (Cookie c : req.getCookies()) {
            if (!COOKIE.equals(c.getName())) continue;
            int i = c.getValue().lastIndexOf('.');
            if (i < 1) return null;
            String id = c.getValue().substring(0, i);
            boolean ok = MessageDigest.isEqual(c.getValue().substring(i + 1).getBytes(StandardCharsets.UTF_8),
                    sign(id).getBytes(StandardCharsets.UTF_8));
            return ok ? id : null;
        }
        return null;
    }

    private void cookie(HttpServletResponse res, String value, Duration maxAge) {
        res.addHeader(HttpHeaders.SET_COOKIE, ResponseCookie.from(COOKIE, value).httpOnly(true).secure(secureCookies)
                .sameSite("Lax").path("/").maxAge(maxAge).build().toString());
    }

    private CatalogStore.Cart resolve(HttpServletRequest req, HttpServletResponse res, boolean create) {
        String userId = users.userId(req);
        String anonId = readCookie(req);
        if (userId != null) {
            CatalogStore.Cart cart = null;
            if (anonId != null) {
                cart = store.mergeInto(userId, anonId); // the first request after signing in
                cookie(res, "", Duration.ZERO);
            }
            return cart != null ? cart : store.cartForUser(userId, create);
        }
        CatalogStore.Cart cart = store.cart(anonId);
        if (cart != null && cart.userId != null) cart = null; // a cookie never opens a user's cart
        if (cart == null && create) {
            cart = store.newCart(null);
            cookie(res, cart.id + "." + sign(cart.id), Duration.ofDays(30));
        }
        return cart;
    }

    private static Integer wholeNumber(String raw, int fallback) {
        if (raw == null) return fallback;
        return raw.matches("\\d{1,6}") ? Integer.valueOf(raw) : null;
    }

    private static int quantity(JsonNode body, String field, Integer fallback) {
        JsonNode q = body.get(field);
        if (q == null && fallback != null) return fallback;
        if (q == null || !q.isInt()) throw new CatalogError(400, "invalid_quantity");
        return q.intValue();
    }

    private static Map<String, Object> publicProduct(Product p) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", p.id);
        out.put("sku", p.sku);
        out.put("name", p.name);
        out.put("description", p.description);
        out.put("price_cents", p.price_cents);
        out.put("currency", p.currency);
        out.put("image_url", p.image_url);
        out.put("in_stock", p.stock > 0);
        return out;
    }

    // ---- routes -------------------------------------------------------------------------------
    @GetMapping("/api/products")
    public ResponseEntity<Object> products(@RequestParam(required = false) String q,
                                           @RequestParam(required = false) String page,
                                           @RequestParam(name = "page_size", required = false) String pageSize) {
        Integer p = wholeNumber(page, 1);
        Integer size = wholeNumber(pageSize, 12);
        if (p == null || p < 1 || size == null || size < 1 || size > MAX_PAGE_SIZE) throw new CatalogError(400, "invalid_page");
        String query = q == null ? "" : q;
        if (query.length() > MAX_QUERY) throw new CatalogError(400, "query_too_long");
        Map<String, Object> result = new LinkedHashMap<>(store.search(query, p, size));
        @SuppressWarnings("unchecked")
        List<Product> items = (List<Product>) result.get("items");
        result.put("items", items.stream().map(CatalogController::publicProduct).toList());
        return ResponseEntity.ok(result);
    }

    @GetMapping("/api/products/{id}")
    public Map<String, Object> product(@PathVariable String id) {
        Product p = store.activeProduct(id);
        if (p == null) throw new CatalogError(404, "product_not_found");
        return publicProduct(p);
    }

    @GetMapping("/api/cart")
    public Map<String, Object> cart(HttpServletRequest req, HttpServletResponse res) {
        return store.view(resolve(req, res, false));
    }

    @PostMapping(path = "/api/cart/items", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<Map<String, Object>> add(@RequestBody JsonNode body, HttpServletRequest req, HttpServletResponse res) {
        JsonNode id = body.get("product_id");
        if (id == null || !id.isTextual()) throw new CatalogError(400, "invalid_product");
        int qty = quantity(body, "quantity", 1);
        CatalogStore.Cart cart = resolve(req, res, true);
        store.addItem(cart, id.textValue(), qty);
        return ResponseEntity.status(HttpStatus.CREATED).body(store.view(cart));
    }

    @PatchMapping(path = "/api/cart/items/{productId}", consumes = MediaType.APPLICATION_JSON_VALUE)
    public Map<String, Object> set(@PathVariable String productId, @RequestBody JsonNode body,
                                   HttpServletRequest req, HttpServletResponse res) {
        CatalogStore.Cart cart = resolve(req, res, false);
        if (cart == null) throw new CatalogError(404, "not_in_cart");
        store.setQuantity(cart, productId, quantity(body, "quantity", null));
        return store.view(cart);
    }

    @DeleteMapping("/api/cart/items/{productId}")
    public Map<String, Object> remove(@PathVariable String productId, HttpServletRequest req, HttpServletResponse res) {
        CatalogStore.Cart cart = resolve(req, res, false);
        if (cart == null) throw new CatalogError(404, "not_in_cart");
        store.removeItem(cart, productId);
        return store.view(cart);
    }

    @GetMapping("/api/cart/checkout")
    public Map<String, Object> checkout(HttpServletRequest req, HttpServletResponse res) {
        CatalogStore.Cart cart = resolve(req, res, false);
        return store.checkoutLines(cart == null ? null : cart.id);
    }

    // ---- errors: fixed codes, never a stack trace ----------------------------------------------
    @ExceptionHandler(CatalogError.class)
    public ResponseEntity<Map<String, Object>> refused(CatalogError e) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", e.code);
        body.putAll(e.extra);
        return ResponseEntity.status(e.status).body(body);
    }

    /** Cart changes are JSON only: a cross-site HTML form cannot send application/json, so with
     *  the SameSite=Lax cookie this is the CSRF defence for the cart. */
    @ExceptionHandler(HttpMediaTypeNotSupportedException.class)
    public ResponseEntity<Map<String, Object>> notJson() {
        return ResponseEntity.status(415).body(Map.of("error", "json_required"));
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Map<String, Object>> badBody() {
        return ResponseEntity.status(400).body(Map.of("error", "invalid_body"));
    }
}
