# Add a product catalog and cart

Add a product catalog (list with search and pages, product detail) and a shopping cart with
server-computed totals to this app, plus the checkout contract the payment templates use.
Follow the reference implementation for this app's stack; adapt it to the app's router, data
layer and UI rather than copying files over the top of the app's own.

## The contract (the template's tests check exactly this, over HTTP)

| Route | Behaviour |
|---|---|
| `GET /api/products?q=&page=&page_size=` | active products only, sorted by name; `q` matches name, description or SKU (case-insensitive, <= 100 chars); `page` >= 1, `page_size` 1-50 (default 12), else 400 `invalid_page`; answers `{items, page, page_size, total, pages}`; an item has `id, sku, name, description, price_cents, currency, image_url, in_stock` (never the stock count) |
| `GET /api/products/:id` | the product, or 404 `product_not_found` when unknown or inactive |
| `GET /api/cart` | `{id, currency, items: [{product_id, sku, name, unit_price_cents, quantity, line_total_cents, available}], item_count, subtotal_cents}`; an empty cart when there is none |
| `POST /api/cart/items` | JSON `{product_id, quantity = 1}` adds to the line; 201 + the cart. 415 unless JSON; 400 `invalid_quantity` unless a whole number 1-99 (the line too); 404 for an inactive/unknown product; 409 `insufficient_stock` above stock |
| `PATCH /api/cart/items/:product_id` | JSON `{quantity}` sets the line (0 removes); 404 `not_in_cart` |
| `DELETE /api/cart/items/:product_id` | removes the line; 404 `not_in_cart` |
| `GET /api/cart/checkout` | THE CHECKOUT CONTRACT: `{cart_id, currency, lines: [{product_id, sku, name, unit_amount_cents, quantity, line_total_cents}], total_cents}` with every price read now; 409 `cart_empty`, 409 `product_unavailable` (+ `product_id`), 409 `insufficient_stock` |

- **Money** is integer cents everywhere; prices and totals come from the server only, a price
  in a request is ignored, and a cart line stores no price.
- **Data**: tables `products`, `carts`, `cart_items` (`schema.sql`), seeded with the 20 products
  of `seed/products.json` (18 active, 2 inactive, one out of stock, one with stock 3; the tests
  rely on this seed).
- **The cart's owner**: an anonymous cart is found by the `cart` cookie (`<cart id>.<HMAC-SHA256
  of the id with CART_SECRET>`, `HttpOnly`, `SameSite=Lax`, `Secure` when `APP_BASE_URL` is
  https); a signed-in user's by their user id. When `login-oauth` is applied, the user is the
  session's (`req.session.userId` / `request.session["user_id"]`), and on the first request
  after sign-in that still carries an anonymous cart cookie, the anonymous cart merges into the
  user's (quantities added, capped at stock) and the cookie is cleared. Without a login every
  cart is anonymous.
- **Payments**: `payments-stripe` and `payments-paypal` call the store's `checkoutLines(cartId)`
  (`checkout_lines(cart_id)` in Python) or `GET /api/cart/checkout`; never compute an amount
  anywhere else.
- **UI**: a product list with a search box and Previous/Next paging, a detail view, and a cart
  drawer with quantity inputs, line totals, the subtotal and Remove, on the app's design tokens;
  every control has an accessible name.

## Per stack

- **node** (Express): `catalog/store.mjs` (products, carts, merge, `checkoutLines`),
  `catalog/routes.mjs` (`createCatalog(config, {getUserId})` returns the router),
  `server.mjs` (how to mount it), `seed/products.json`, `ui/Catalog.jsx` (React: `ProductList`,
  `ProductDetail`, `CartDrawer`, `useCart`). Add the two scripts `pretest:catalog-cart` and
  `test:catalog-cart` to the app's `package.json` unchanged. Copy `tests/template_catalog_cart/`
  to the same path.
- **python** (FastAPI): the `catalog/` package (`store.py`, `routes.py` with
  `create_catalog(config, get_user_id=...)` returning the router), `main.py`,
  `seed/products.json`, `ui/catalog.html` (framework-free list, search, cart). Add the lines of
  `requirements.txt` to the app's requirements. Copy `tests/template_catalog_cart/` to the same
  path.

## The tests are the proof

Keep the template's tests as they are. They pass a test-only `getUserId` / `get_user_id` that
reads a header, to stand in for a signed-in user; the app's real one must come from its session,
never from a request header.
