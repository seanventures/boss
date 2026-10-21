# T4 · Customers are being oversold (incident)

**From:** NorthPeak Retail, on-call · **Repository:** `northpeak-saleor` (the release that just went out)

Since this morning's release, customers can buy items we no longer have. Support has three
complaints of orders for out-of-stock sizes, and the warehouse shows negative availability on a
few variants. Please find the cause, fix it, and make sure it cannot quietly come back.

What matters to us:
- Orders must never take more stock than is available, counting units already allocated to other orders and units held by active reservations.
- The fix needs a test that would have caught this.
- Tell us the root cause in one sentence for the incident record.

## Contract
- `saleor.warehouse.management.allocate_stocks` must raise `InsufficientStock` whenever the requested quantity exceeds what is available, and must never leave `Stock.quantity_allocated > Stock.quantity`.
