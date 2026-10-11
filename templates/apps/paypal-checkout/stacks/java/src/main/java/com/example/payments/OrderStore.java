package com.example.payments;

import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.function.Supplier;

/**
 * Orders and processed webhook events for the {@code paypal-checkout} template (the same store as
 * {@code stripe-checkout}: if the app has both, keep ONE orders table, told apart by provider).
 *
 * <p>This reference keeps them in memory; ADAPT it to the app's database with the tables in
 * {@code schema.sql}. What must survive the adaptation: {@code processed_events.event_id} is a
 * PRIMARY KEY inserted in the SAME transaction as the order change the event causes; order status
 * only moves along {@link #TRANSITIONS}; amounts are integer minor units (cents).
 */
public class OrderStore {
  public static final Map<String, Set<String>> TRANSITIONS = Map.of(
      "pending", Set.of("paid", "failed", "cancelled", "needs_review"),
      "failed", Set.of("paid", "cancelled", "needs_review"), // a buyer may retry a declined card
      "paid", Set.of("refunded"),
      "cancelled", Set.of("needs_review"), // paid after we cancelled it: a person decides
      "refunded", Set.of(),
      "needs_review", Set.of());

  public static final class TransitionException extends RuntimeException {
    public TransitionException(String message) {
      super(message);
    }
  }

  public static final class Order {
    public String id;
    public String provider;
    public String providerRef; // the PayPal order id this app created (Orders v2)
    public String paymentRef; // the PayPal capture id, recorded when the order is paid
    public String cartId;
    public String status;
    public List<Catalog.Line> items;
    public long amountCents;
    public String currency;
    public String createdAt;
    public String paidAt;
    public String refundedAt;

    Order copy() {
      Order o = new Order();
      o.id = id; o.provider = provider; o.providerRef = providerRef; o.paymentRef = paymentRef;
      o.cartId = cartId; o.status = status; o.items = new ArrayList<>(items); o.amountCents = amountCents;
      o.currency = currency; o.createdAt = createdAt; o.paidAt = paidAt; o.refundedAt = refundedAt;
      return o;
    }
  }

  private final Map<String, Order> orders = new HashMap<>();
  private final Map<String, String> events = new HashMap<>();

  public synchronized Order createOrder(String provider, List<Catalog.Line> items, long amountCents,
      String currency, String cartId) {
    Order o = new Order();
    o.id = UUID.randomUUID().toString();
    o.provider = provider;
    o.cartId = cartId;
    o.status = "pending";
    o.items = new ArrayList<>(items);
    o.amountCents = amountCents;
    o.currency = currency;
    o.createdAt = Instant.now().toString();
    orders.put(o.id, o);
    return o.copy();
  }

  public synchronized Optional<Order> get(String id) {
    Order o = id == null ? null : orders.get(id);
    return Optional.ofNullable(o == null ? null : o.copy());
  }

  public synchronized List<Order> all() {
    List<Order> out = new ArrayList<>();
    orders.values().forEach(o -> out.add(o.copy()));
    return out;
  }

  public synchronized void setProviderRef(String id, String ref) {
    require(id).providerRef = ref;
  }

  public synchronized void setPaymentRef(String id, String ref) {
    require(id).paymentRef = ref;
  }

  public synchronized Optional<Order> findByProviderRef(String provider, String ref) {
    return orders.values().stream()
        .filter(o -> o.provider.equals(provider) && ref.equals(o.providerRef))
        .findFirst().map(Order::copy);
  }

  public synchronized Optional<Order> findByPaymentRef(String provider, String ref) {
    return orders.values().stream()
        .filter(o -> o.provider.equals(provider) && ref.equals(o.paymentRef))
        .findFirst().map(Order::copy);
  }

  /** True when this call changed the order, false when it was already there (idempotent case). */
  public synchronized boolean transition(String id, String to) {
    Order o = require(id);
    if (o.status.equals(to)) {
      return false;
    }
    if (!TRANSITIONS.getOrDefault(o.status, Set.of()).contains(to)) {
      throw new TransitionException("order " + o.status + " cannot become " + to);
    }
    o.status = to;
    if (to.equals("paid")) o.paidAt = Instant.now().toString();
    if (to.equals("refunded")) o.refundedAt = Instant.now().toString();
    return true;
  }

  /**
   * Runs {@code apply} only if {@code eventId} was never processed, and records it. Synchronized,
   * so nothing interleaves; if {@code apply} throws, the event is not recorded (the in-memory
   * reference does not roll back partial order changes: in SQL, do both in one transaction).
   */
  public synchronized boolean processEventOnce(String eventId, String provider, String type, Supplier<Object> apply) {
    if (events.containsKey(eventId)) {
      return false;
    }
    apply.get();
    events.put(eventId, provider + ":" + type);
    return true;
  }

  private Order require(String id) {
    Order o = orders.get(id);
    if (o == null) throw new TransitionException("no such order");
    return o;
  }
}
