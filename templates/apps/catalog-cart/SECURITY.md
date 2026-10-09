# Product catalog and cart: security note

## What it protects

- **Prices.** The client sends product ids and quantities only. Every price, line total, cart
  subtotal and checkout total is computed on the server from the product's current price, in
  integer cents (no floating point). A price sent by the client is ignored. A cart line stores
  no price, so a price change applies at checkout, and the checkout contract re-reads every
  product.
- **What can be sold.** Inactive products are hidden from the list, search and detail (404),
  cannot be added, and make checkout refuse the cart (409 `product_unavailable`). Quantities
  are whole numbers from 1 to 99 per line and never above stock (409 `insufficient_stock`);
  checkout re-checks stock.
- **Whose cart.** An anonymous cart is found by a cookie holding the cart id and an
  HMAC-SHA256 signature (`CART_SECRET`), compared in constant time: a guessed or altered id
  opens nothing. A signed-in user's cart is found by their user id, never by a cookie, and an
  anonymous cart merges into it on the first request after sign-in (then the cookie is
  cleared). Carts are isolated between sessions.
- **CSRF.** Cart changes accept `application/json` only (415 otherwise), which a cross-site HTML
  form cannot send, and the cookie is `SameSite=Lax`, `HttpOnly`, and `Secure` when
  `APP_BASE_URL` is https.
- **Input bounds.** `page` >= 1, `page_size` 1-50, a search of at most 100 characters (400
  otherwise); JSON bodies are capped at 10 KB. Search is an in-memory substring match in the
  reference; when moved to SQL it must use bound parameters (and escape `%`/`_` in LIKE).
- **Errors** are fixed codes (`invalid_quantity`, `product_not_found`, ...), never stack traces.

## What it does not do

- No payment: that is `payments-stripe` / `payments-paypal`, which take the checkout contract.
- No stock reservation: stock is checked when adding and at checkout, not held. Two buyers can
  both reach checkout for the last item; the payment template must decrement stock in the same
  transaction that marks the order paid.
- No admin screens for products (the store has `setPrice` / `setActive` for the app's own).
- No rate limiting.
- The reference stores are in memory (one process); use the app's database to keep carts across
  restarts and instances.
