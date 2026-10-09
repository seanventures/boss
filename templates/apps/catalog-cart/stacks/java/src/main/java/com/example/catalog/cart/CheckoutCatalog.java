package com.example.catalog.cart;

import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Component;

/**
 * THE PAYMENTS CONTRACT (spec §3.4): what {@code stripe-checkout} and {@code paypal-checkout}
 * call through their {@code com.example.payments.Catalog} interface. The records and methods
 * mirror that interface component for component, so the app's {@code Catalog} implementation
 * only copies fields across:
 * <pre>
 *   product(id)                  -> Optional[Product(id, name, unitAmountCents, currency, active)]
 *   cartLines(cartId, request)   -> Optional[List[Line(productId, name, unitAmountCents, currency, quantity)]]
 *                                   | Optional.empty() when cartId is not the requester's cart
 * </pre>
 */
@Component
public class CheckoutCatalog {
    public record Product(String id, String name, long unitAmountCents, String currency, boolean active) {}

    public record Line(String productId, String name, long unitAmountCents, String currency, long quantity) {}

    private final CatalogStore store;
    private final CatalogController routes;

    public CheckoutCatalog(CatalogStore store, CatalogController routes) {
        this.store = store;
        this.routes = routes;
    }

    /** Any product (an inactive one with active=false), or empty for an unknown id. */
    public Optional<Product> product(String id) {
        return Optional.ofNullable(id == null ? null : store.product(id))
                .map(p -> new Product(p.id, p.name, p.price_cents, store.currency(), p.active));
    }

    /**
     * The lines of cart {@code cartId}, priced now, ONLY when it is the requester's own cart,
     * found from the request exactly as the routes find it (signed cookie, or the signed-in
     * user's cart); empty for any other id, so a guessed or leaked cart id buys nothing. An empty
     * list for an empty cart. A cart that cannot be sold now (a withdrawn product, a quantity
     * above stock) throws {@link CatalogError} (409 + code), as GET /api/cart/checkout answers.
     */
    public Optional<List<Line>> cartLines(String cartId, HttpServletRequest request) {
        CatalogStore.Cart cart = routes.callersCart(request);
        if (cart == null || cartId == null || !cart.id.equals(cartId)) return Optional.empty();
        return Optional.of(store.cartLines(cart.id));
    }
}
