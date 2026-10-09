# Product catalog and cart: setup

Nothing to set up at a provider: the catalog has no outside service. Once the build is merged:

1. Open the app's preview: `{{preview_url}}`. The catalog starts with 20 seed products
   (18 on sale, 2 withdrawn) from `seed/products.json`.
2. `{{preview_url}}/api/products?q=mug` searches; `{{preview_url}}/api/cart` shows your cart.
3. Set `APP_BASE_URL` to the address people use (for the preview, `{{preview_url}}`): when it is
   https the cart cookie is marked `Secure`.
4. Prices are in `CATALOG_CURRENCY` (default `usd`), in integer cents.

## Your own products

Replace the seed file with your products (same fields: `id`, `sku`, `name`, `description`,
`price_cents`, `currency`, `image_url`, `stock`, `active`) or load them into the `products`
table. Withdraw a product by setting `active` to false rather than deleting it: carts that hold
it then show it as unavailable and checkout refuses it.

## With other templates

- **Login** (`login-oauth`): a visitor's cart follows them when they sign in (it merges into
  their account's cart) and is then the same on every device.
- **Payments** (`stripe-checkout`, `paypal-checkout`): checkout takes the cart through the
  payments contract, `cartLineItems(cartId, req)` (Node), `cart_line_items(cart_id, request)`
  (Python) or `CheckoutCatalog.cartLines(cartId, request)` (Java). It answers
  `[{product_id, name, unit_amount_cents, currency, quantity}]` with every price read from the
  catalog at that moment, and only for the shopper's own cart: another visitor's cart id buys
  nothing. A withdrawn or out-of-stock item stops the checkout before any payment is created.
  `GET {{preview_url}}/api/cart/checkout` shows the same cart priced for checkout.
