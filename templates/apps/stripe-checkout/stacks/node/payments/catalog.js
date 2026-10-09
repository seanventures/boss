// Where prices come from: ALWAYS the server. The client sends product ids and quantities (or a
// cart id); any price, amount or currency it sends is ignored.
//
// The checkout contract (shared with the `catalog-cart` template, spec §3.4):
//   catalog.getProduct(id)        -> { id, name, unit_amount_cents, currency, active } | null
//   catalog.cartLineItems(cartId) -> [{ product_id, name, unit_amount_cents, currency, quantity }]
//                                    | null when there is no such cart (for this session)
// When `catalog-cart` is applied, replace this module's functions with its `products` table and
// its cart's checkout function (same shapes). Without it, the app sells the products below with
// a single buy button.
const DEFAULT_PRODUCTS = [
  { id: "starter-pack", name: "Starter pack", unit_amount_cents: 1500, currency: "usd", active: true },
  { id: "pro-pack", name: "Pro pack", unit_amount_cents: 4900, currency: "usd", active: true },
];

function staticCatalog(products = DEFAULT_PRODUCTS) {
  const byId = new Map(products.map((p) => [p.id, { ...p }]));
  return {
    getProduct(id) {
      const p = byId.get(String(id));
      return p ? { ...p } : null;
    },
    // No carts without `catalog-cart`.
    cartLineItems() {
      return null;
    },
  };
}

const MAX_QUANTITY = 99;
const MAX_LINES = 50;

class CheckoutInputError extends Error {}

// Turn the client's request into server-priced line items. Throws CheckoutInputError on anything
// it cannot price. `req` is the HTTP request (a cart provider may need its session cookie).
async function priceLineItems(catalog, body, req) {
  let lines;
  if (body && body.cart_id !== undefined && body.cart_id !== null) {
    const cartLines = await catalog.cartLineItems(String(body.cart_id), req);
    if (!cartLines) throw new CheckoutInputError("no such cart");
    lines = cartLines.map((l) => ({
      product_id: String(l.product_id),
      name: String(l.name),
      unit_amount_cents: l.unit_amount_cents,
      currency: String(l.currency).toLowerCase(),
      quantity: l.quantity,
    }));
  } else {
    const items = body && Array.isArray(body.items) ? body.items : null;
    if (!items || items.length === 0) throw new CheckoutInputError("send cart_id or items");
    if (items.length > MAX_LINES) throw new CheckoutInputError("too many items");
    lines = [];
    for (const item of items) {
      const product = await catalog.getProduct(item && item.product_id);
      if (!product || !product.active) throw new CheckoutInputError("unknown product");
      // Only the id and quantity are read from the client; item.price / item.amount are ignored.
      lines.push({
        product_id: product.id,
        name: product.name,
        unit_amount_cents: product.unit_amount_cents,
        currency: String(product.currency).toLowerCase(),
        quantity: item.quantity,
      });
    }
  }
  if (lines.length === 0) throw new CheckoutInputError("the cart is empty");
  for (const l of lines) {
    if (!Number.isInteger(l.quantity) || l.quantity < 1 || l.quantity > MAX_QUANTITY) {
      throw new CheckoutInputError(`quantity must be a whole number from 1 to ${MAX_QUANTITY}`);
    }
    if (!Number.isInteger(l.unit_amount_cents) || l.unit_amount_cents < 1) {
      throw new CheckoutInputError("a product has no valid price");
    }
  }
  const currencies = new Set(lines.map((l) => l.currency));
  if (currencies.size !== 1) throw new CheckoutInputError("one currency per checkout");
  const currency = [...currencies][0];
  const amountCents = lines.reduce((sum, l) => sum + l.unit_amount_cents * l.quantity, 0);
  if (!Number.isSafeInteger(amountCents)) throw new CheckoutInputError("total too large");
  return { lines, currency, amountCents };
}

module.exports = { staticCatalog, priceLineItems, CheckoutInputError, DEFAULT_PRODUCTS, MAX_QUANTITY };
