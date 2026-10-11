-- catalog-cart: the tables behind catalog/store.py. Map them onto the app's database layer.
-- Money is integer cents everywhere; a currency is a lowercase ISO 4217 code.
CREATE TABLE IF NOT EXISTS products (
  id          TEXT PRIMARY KEY,
  sku         TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
  currency    TEXT NOT NULL DEFAULT 'usd',
  image_url   TEXT,
  stock       INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
  active      BOOLEAN NOT NULL DEFAULT TRUE
);

-- An anonymous cart is found by the signed `cart` cookie; a signed-in user's by user_id.
CREATE TABLE IF NOT EXISTS carts (
  id         TEXT PRIMARY KEY,
  user_id    TEXT UNIQUE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- No price is stored on a line: totals and checkout always use products.price_cents now.
CREATE TABLE IF NOT EXISTS cart_items (
  cart_id    TEXT NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
  product_id TEXT NOT NULL REFERENCES products(id),
  quantity   INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 99),
  PRIMARY KEY (cart_id, product_id)
);
