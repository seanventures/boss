# T1 · Reserve stock when an order is placed

**From:** NorthPeak Retail, product owner · **Repository:** a new `northpeak-fulfilment` service, next to our Saleor store

When a customer places an order in our Saleor store, we need the items held for them at once so a
second customer cannot buy the last unit while the first order is being picked. Please build a
small Python service, `northpeak-fulfilment`, that listens for Saleor's "order created" webhook and
reserves stock for every order line.

What matters to us:
- A reservation is per order line, and it lasts 15 minutes unless the order is fulfilled.
- Saleor sometimes delivers the same webhook twice. A repeat must never reserve twice.
- Never reserve more than we have. If a line cannot be fully reserved, reserve none of that line and say so in the response.
- Only Saleor may call it: reject requests whose signature does not verify.

## Contract (from our architect)
- `POST /webhooks/saleor/order-created`, with JSON body `{"order": {"id": str, "lines": [{"id": str, "variant_id": str, "quantity": int}]}}`.
- Header `Saleor-Signature`: hex HMAC-SHA256 of the raw body with the secret in env `NORTHPEAK_WEBHOOK_SECRET`. On a bad signature, respond 401.
- Respond 200 with `{"reserved": [line ids], "rejected": [line ids]}`.
- Postgres via env `DATABASE_URL`, the same database Saleor uses. Create table `stock_reservation(order_line_id TEXT PRIMARY KEY, variant_id TEXT NOT NULL, quantity INT NOT NULL, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)`.
- Available stock for a variant = `SUM(quantity - quantity_allocated)` over Saleor's `warehouse_stock` rows for that `product_variant_id`, minus unexpired `stock_reservation` rows for it.
- `GET /healthz` responds 200. The service listens on env `PORT`.
- A `Dockerfile` at the repository root builds the service. We run it with `PORT`, `DATABASE_URL` and `NORTHPEAK_WEBHOOK_SECRET` set.
