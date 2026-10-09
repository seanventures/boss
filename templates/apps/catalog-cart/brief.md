# Add a product catalog and cart

Add a product catalog (list with search and pages, product detail) and a shopping cart with
server-computed totals to this app, plus the payments contract the payment templates call.
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
| `GET /api/cart/checkout` | the caller's cart priced for checkout (the store's `checkoutLines` / `checkout_lines`): `{cart_id, currency, lines: [{product_id, sku, name, unit_amount_cents, quantity, line_total_cents}], total_cents}` with every price read now; 409 `cart_empty`, 409 `product_unavailable` (+ `product_id`), 409 `insufficient_stock` |

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
- **The payments contract** (spec §3.4; `stripe-checkout` and `paypal-checkout` call exactly
  these names, and the template's tests check them):

  | Stack | Product | Cart lines |
  |---|---|---|
  | node | `catalog.getProduct(id)` | `catalog.cartLineItems(cartId, req)` |
  | python | `catalog.get_product(id)` | `catalog.cart_line_items(cart_id, request)` |
  | java | `CheckoutCatalog.product(id)` → `Optional<Product>` | `CheckoutCatalog.cartLines(cartId, request)` → `Optional<List<Line>>` |

  where `catalog` is what `createCatalog` / `create_catalog` returns. A product is
  `{id, name, unit_amount_cents, currency, active}` (null / `None` / empty when unknown). Cart
  lines are `[{product_id, name, unit_amount_cents, currency, quantity}]` (Java records with
  the same components in camelCase, mirroring `com.example.payments.Catalog`), priced now; `[]`
  for an empty cart; **null / `None` / `Optional.empty()` unless `cartId` is the requester's own
  cart**, found from the request exactly as the routes find it (the signed `cart` cookie, or the
  signed-in user's cart). A cart that cannot be sold now raises the store's `CatalogError`
  (409 + `code`: `product_unavailable`, `insufficient_stock`). Wiring it into a payment
  template: in node and python pass `catalog` as its `catalog`; in java write one `Catalog`
  implementation that copies the records across. Let `CatalogError` propagate: the payment
  templates' adapters turn it into their `CheckoutInputError` / `CheckoutInputException`
  themselves (a 400 with the code, no payment created), so keep its name and its `status` /
  `code` fields.
  Never compute an amount anywhere else.
- **UI**: a product list with a search box and Previous/Next paging, a detail view, and a cart
  drawer with quantity inputs, line totals, the subtotal and Remove, on the app's design tokens;
  every control has an accessible name.

## Per stack

- **node** (Express): `catalog/store.mjs` (products, carts, merge, `checkoutLines`),
  `catalog/routes.mjs` (`createCatalog(config, {getUserId})` returns `{router, getProduct,
  cartLineItems, ...}`),
  `server.mjs` (how to mount it), `seed/products.json`, `ui/Catalog.jsx` (React: `ProductList`,
  `ProductDetail`, `CartDrawer`, `useCart`). Add the two scripts `pretest:catalog-cart` and
  `test:catalog-cart` to the app's `package.json` unchanged. Copy `tests/template_catalog_cart/`
  to the same path.
- **python** (FastAPI): the `catalog/` package (`store.py`, `routes.py` with
  `create_catalog(config, get_user_id=...)` returning a `Catalog` with `router`, `get_product`
  and `cart_line_items`), `main.py`,
  `seed/products.json`, `ui/catalog.html` (framework-free list, search, cart). Add the lines of
  `requirements.txt` to the app's requirements. Copy `tests/template_catalog_cart/` to the same
  path.
- **java** (Spring Boot 3.5): the `cart` package (`CatalogStore` with `checkoutLines`,
  `CatalogController` with the signed cookie and the JSON error answers, `CheckoutCatalog` (the
  payments contract), `Product`,
  `CatalogError`, `CartUserResolver` whose default reads the signed-in principal),
  `src/main/resources/seed/products.json` and `static/catalog.html`. Copy
  `tests/java/com/example/catalog/CatalogCartTemplateTest.java` into the app's test sources
  (`src/test/java/<the app's package>/`, package line adjusted; keep the file name). With
  `login-oauth` applied (Spring Security): permit `/api/products/**` and `/api/cart/**` in its
  `SecurityConfig` (its `/api/**` rule would otherwise make the catalog sign-in only), and
  exclude `/api/cart/**` from Spring's CSRF check (the cart's CSRF defence is JSON-only +
  `SameSite=Lax`, as in the other stacks).

## The tests are the proof

Keep the template's tests as they are. They pass a test-only `getUserId` / `get_user_id` that
reads a header, to stand in for a signed-in user; the app's real one must come from its session,
never from a request header.
