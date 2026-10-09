package com.example.catalog.cart;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Stream;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * Products, carts and the checkout contract (schema.sql has the tables). This in-memory store is
 * the reference; back the same methods with the app's database (JPA, JDBC) when adapting.
 * Money is integer cents everywhere. A cart line holds no price: totals and checkout always use
 * the product's price NOW, so a client can never choose what it pays.
 */
@Component
public class CatalogStore {
    public static final int MAX_QUANTITY = 99;

    /** A cart: its id, its owner (null = anonymous) and product id -> quantity, in insertion order. */
    public static final class Cart {
        public final String id = UUID.randomUUID().toString();
        public final String userId;
        public final LinkedHashMap<String, Integer> items = new LinkedHashMap<>();

        Cart(String userId) {
            this.userId = userId;
        }
    }

    private final String currency;
    private final Map<String, Product> products = new LinkedHashMap<>();
    private final Map<String, Cart> carts = new HashMap<>();
    private final Map<String, String> userCarts = new HashMap<>();

    public CatalogStore(@Value("${CATALOG_CURRENCY:usd}") String currency) {
        this.currency = currency.toLowerCase(Locale.ROOT);
        reset(loadSeed());
    }

    public static List<Product> loadSeed() {
        try (InputStream in = CatalogStore.class.getResourceAsStream("/seed/products.json")) {
            return new ObjectMapper().readValue(in, new TypeReference<List<Product>>() {});
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /** Replace the catalog and drop every cart (a fresh start: tests, a demo reset). */
    public synchronized void reset(List<Product> seed) {
        products.clear();
        carts.clear();
        userCarts.clear();
        seed.forEach(p -> products.put(p.id, p));
    }

    public String currency() { return currency; }

    // ---- products ---------------------------------------------------------------------------
    public synchronized Product activeProduct(String id) {
        Product p = products.get(id);
        return p != null && p.active ? p : null;
    }

    public synchronized Map<String, Object> search(String q, int page, int pageSize) {
        String needle = q.trim().toLowerCase(Locale.ROOT);
        List<Product> hits = products.values().stream().filter(p -> p.active)
                .filter(p -> needle.isEmpty() || Stream.of(p.name, p.description, p.sku)
                        .anyMatch(f -> f.toLowerCase(Locale.ROOT).contains(needle)))
                .sorted(Comparator.comparing((Product p) -> p.name).thenComparing(p -> p.id)).toList();
        int start = Math.min((page - 1) * pageSize, hits.size());
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("items", hits.subList(start, Math.min(start + pageSize, hits.size())));
        out.put("page", page);
        out.put("page_size", pageSize);
        out.put("total", hits.size());
        out.put("pages", Math.max(1, (hits.size() + pageSize - 1) / pageSize));
        return out;
    }

    /** An operator's change (the app's admin screens call these). */
    public synchronized void setPrice(String id, long priceCents) {
        if (priceCents < 0) throw new CatalogError(400, "invalid_price");
        products.get(id).price_cents = priceCents;
    }

    public synchronized void setActive(String id, boolean active) {
        products.get(id).active = active;
    }

    // ---- carts --------------------------------------------------------------------------------
    public synchronized Cart newCart(String userId) {
        Cart cart = new Cart(userId);
        carts.put(cart.id, cart);
        if (userId != null) userCarts.put(userId, cart.id);
        return cart;
    }

    public synchronized Cart cart(String id) {
        return id == null ? null : carts.get(id);
    }

    public synchronized Cart cartForUser(String userId, boolean create) {
        Cart cart = cart(userCarts.get(userId));
        return cart != null ? cart : (create ? newCart(userId) : null);
    }

    private static void checkStock(Product p, int quantity) {
        if (quantity > p.stock) throw new CatalogError(409, "insufficient_stock", Map.of("available", p.stock));
    }

    public synchronized void addItem(Cart cart, String productId, int quantity) {
        if (quantity < 1 || quantity > MAX_QUANTITY) throw new CatalogError(400, "invalid_quantity");
        Product p = activeProduct(productId);
        if (p == null) throw new CatalogError(404, "product_not_found");
        int next = cart.items.getOrDefault(productId, 0) + quantity;
        if (next > MAX_QUANTITY) throw new CatalogError(400, "invalid_quantity");
        checkStock(p, next);
        cart.items.put(productId, next);
    }

    public synchronized void setQuantity(Cart cart, String productId, int quantity) {
        if (quantity < 0 || quantity > MAX_QUANTITY) throw new CatalogError(400, "invalid_quantity");
        if (!cart.items.containsKey(productId)) throw new CatalogError(404, "not_in_cart");
        if (quantity == 0) {
            cart.items.remove(productId);
            return;
        }
        Product p = activeProduct(productId);
        if (p == null) throw new CatalogError(409, "product_unavailable", Map.of("product_id", productId));
        checkStock(p, quantity);
        cart.items.put(productId, quantity);
    }

    public synchronized void removeItem(Cart cart, String productId) {
        if (cart.items.remove(productId) == null) throw new CatalogError(404, "not_in_cart");
    }

    /** Signing in: the anonymous cart's lines join the user's cart (quantities added, capped at
     *  stock and MAX_QUANTITY) and the anonymous cart is deleted. */
    public synchronized Cart mergeInto(String userId, String anonCartId) {
        Cart anon = cart(anonCartId);
        if (anon == null || anon.userId != null) return cartForUser(userId, false);
        Cart target = cartForUser(userId, true);
        anon.items.forEach((productId, qty) -> {
            Product p = activeProduct(productId);
            if (p == null) return;
            int merged = Math.min(Math.min(target.items.getOrDefault(productId, 0) + qty, p.stock), MAX_QUANTITY);
            if (merged > 0) target.items.put(productId, merged);
        });
        carts.remove(anon.id);
        return target;
    }

    public synchronized Map<String, Object> view(Cart cart) {
        List<Map<String, Object>> items = new ArrayList<>();
        long subtotal = 0;
        int count = 0;
        if (cart != null) {
            for (Map.Entry<String, Integer> e : cart.items.entrySet()) {
                Product p = products.get(e.getKey());
                long line = Math.multiplyExact(p.price_cents, e.getValue());
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("product_id", p.id);
                item.put("sku", p.sku);
                item.put("name", p.name);
                item.put("unit_price_cents", p.price_cents);
                item.put("quantity", e.getValue());
                item.put("line_total_cents", line);
                item.put("available", p.active && p.stock >= e.getValue());
                items.add(item);
                subtotal = Math.addExact(subtotal, line);
                count += e.getValue();
            }
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", cart == null ? null : cart.id);
        out.put("currency", currency);
        out.put("items", items);
        out.put("item_count", count);
        out.put("subtotal_cents", subtotal);
        return out;
    }

    /**
     * THE CHECKOUT CONTRACT (payments-stripe, payments-paypal): a cart id in, the lines to charge
     * out, every price read from the product now. Refuses an empty cart, a product no longer on
     * sale and a quantity above stock, so a payment template never charges for what cannot ship.
     */
    public synchronized Map<String, Object> checkoutLines(String cartId) {
        Cart cart = cart(cartId);
        if (cart == null || cart.items.isEmpty()) throw new CatalogError(409, "cart_empty");
        List<Map<String, Object>> lines = new ArrayList<>();
        long total = 0;
        for (Map.Entry<String, Integer> e : cart.items.entrySet()) {
            Product p = activeProduct(e.getKey());
            if (p == null) throw new CatalogError(409, "product_unavailable", Map.of("product_id", e.getKey()));
            checkStock(p, e.getValue());
            long line = Math.multiplyExact(p.price_cents, e.getValue());
            Map<String, Object> l = new LinkedHashMap<>();
            l.put("product_id", p.id);
            l.put("sku", p.sku);
            l.put("name", p.name);
            l.put("unit_amount_cents", p.price_cents);
            l.put("quantity", e.getValue());
            l.put("line_total_cents", line);
            lines.add(l);
            total = Math.addExact(total, line);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("cart_id", cart.id);
        out.put("currency", currency);
        out.put("lines", lines);
        out.put("total_cents", total);
        return out;
    }
}
