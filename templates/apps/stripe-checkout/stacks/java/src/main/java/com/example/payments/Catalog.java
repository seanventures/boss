package com.example.payments;

import jakarta.servlet.http.HttpServletRequest;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * Where prices come from: ALWAYS the server. The client sends product ids and quantities (or a
 * cart id); any price, amount or currency it sends is ignored.
 *
 * <p>The checkout contract (shared with the {@code catalog-cart} template, spec §3.4): {@link
 * #product} and {@link #cartLines}. When {@code catalog-cart} is applied, implement this interface
 * over its products table and its cart's checkout function. Without it, {@link StaticCatalog}.
 *
 * <p>A cart that cannot be sold now (a product gone, the cart empty, stock): catalog-cart's
 * provider throws its {@code CatalogError} (a 4xx status + a code). {@link #price} turns that into
 * a {@link CheckoutInputException} (the checkout route's 400, before any order or payment-provider
 * call), so a shop combining both templates never answers a 500 for it and needs no glue for it.
 * It is matched by its class name and public {@code status}/{@code code} fields, not imported:
 * this template compiles and works without catalog-cart.
 */
public interface Catalog {
  record Product(String id, String name, long unitAmountCents, String currency, boolean active) {}

  record Line(String productId, String name, long unitAmountCents, String currency, long quantity) {}

  record Priced(List<Line> lines, String currency, long amountCents) {}

  Optional<Product> product(String id);

  /** The cart's lines with server prices, or empty when there is no such cart for this request. */
  Optional<List<Line>> cartLines(String cartId, HttpServletRequest request);

  int MAX_QUANTITY = 99;
  int MAX_LINES = 50;

  final class CheckoutInputException extends RuntimeException {
    public CheckoutInputException(String message) {
      super(message);
    }
  }

  final class StaticCatalog implements Catalog {
    private final Map<String, Product> byId;

    public StaticCatalog(String currency) {
      this(List.of(new Product("starter-pack", "Starter pack", 1500, currency, true),
          new Product("pro-pack", "Pro pack", 4900, currency, true)));
    }

    public StaticCatalog(List<Product> products) {
      Map<String, Product> m = new java.util.HashMap<>();
      products.forEach(p -> m.put(p.id(), p));
      this.byId = Map.copyOf(m);
    }

    @Override
    public Optional<Product> product(String id) {
      return Optional.ofNullable(id == null ? null : byId.get(id));
    }

    @Override
    public Optional<List<Line>> cartLines(String cartId, HttpServletRequest request) {
      return Optional.empty(); // no carts without catalog-cart
    }
  }

  /** catalog-cart's CatalogError (or a subclass) refusing the request: its code, else null. */
  private static String catalogRefusalCode(RuntimeException e) {
    for (Class<?> c = e.getClass(); c != null && c != RuntimeException.class; c = c.getSuperclass()) {
      if (!c.getSimpleName().equals("CatalogError")) continue;
      try {
        Object status = c.getField("status").get(e);
        Object code = c.getField("code").get(e);
        if (status instanceof Integer s && s >= 400 && s <= 499 && code instanceof String k && !k.isEmpty()) {
          return k;
        }
      } catch (ReflectiveOperationException | RuntimeException ignored) {
        // not catalog-cart's shape: leave the error as it is
      }
      return null;
    }
    return null;
  }

  /** Call the catalog; its refusal of the cart is the client's input error, anything else stays
   *  an error of the app (a 500 that must be fixed, not hidden). */
  private static <T> T fromCatalog(java.util.function.Supplier<T> call) {
    try {
      return call.get();
    } catch (CheckoutInputException e) {
      throw e;
    } catch (RuntimeException e) {
      String code = catalogRefusalCode(e);
      if (code != null) throw new CheckoutInputException(code);
      throw e;
    }
  }

  private static boolean isWholeNumber(Object v) {
    return v instanceof Integer || v instanceof Long;
  }

  /** Server-priced line items for the client's JSON body. */
  static Priced price(Catalog catalog, Map<String, Object> body, HttpServletRequest request) {
    List<Line> lines = new ArrayList<>();
    if (body != null && body.get("cart_id") != null) {
      lines.addAll(fromCatalog(() -> catalog.cartLines(String.valueOf(body.get("cart_id")), request))
          .orElseThrow(() -> new CheckoutInputException("no such cart")));
    } else {
      Object raw = body == null ? null : body.get("items");
      if (!(raw instanceof List<?> items) || items.isEmpty()) {
        throw new CheckoutInputException("send cart_id or items");
      }
      if (items.size() > MAX_LINES) throw new CheckoutInputException("too many items");
      for (Object o : items) {
        Map<?, ?> item = o instanceof Map<?, ?> m ? m : Map.of();
        Object pid = item.get("product_id");
        Product p = fromCatalog(() -> catalog.product(pid == null ? null : String.valueOf(pid)))
            .filter(Product::active)
            .orElseThrow(() -> new CheckoutInputException("unknown product"));
        Object q = item.get("quantity");
        if (!isWholeNumber(q)) {
          throw new CheckoutInputException("quantity must be a whole number from 1 to " + MAX_QUANTITY);
        }
        // Only the id and quantity are read from the client; a price or amount is ignored.
        lines.add(new Line(p.id(), p.name(), p.unitAmountCents(), p.currency().toLowerCase(Locale.ROOT),
            ((Number) q).longValue()));
      }
    }
    if (lines.isEmpty()) throw new CheckoutInputException("the cart is empty");
    Set<String> currencies = new HashSet<>();
    long total = 0;
    for (Line l : lines) {
      if (l.quantity() < 1 || l.quantity() > MAX_QUANTITY) {
        throw new CheckoutInputException("quantity must be a whole number from 1 to " + MAX_QUANTITY);
      }
      if (l.unitAmountCents() < 1) throw new CheckoutInputException("a product has no valid price");
      currencies.add(l.currency().toLowerCase(Locale.ROOT));
      total = Math.addExact(total, Math.multiplyExact(l.unitAmountCents(), l.quantity()));
    }
    if (currencies.size() != 1) throw new CheckoutInputException("one currency per checkout");
    return new Priced(lines, currencies.iterator().next(), total);
  }
}
