# T2 · Click & collect with pickup slots

**From:** NorthPeak Retail, product owner · **Repository:** `northpeak-saleor` (our Saleor fork)

Customers want to collect online orders from our stores. Offer "click & collect" as a delivery
choice at checkout. The customer picks a store and a one-hour pickup slot, and a slot cannot be
overbooked.

What matters to us:
- Every store has a capacity per one-hour slot (for example, 10 orders between 10:00 and 11:00).
- A customer can only pick a slot that still has room. When an order is placed, the slot's remaining capacity goes down by one.
- The order shows the store and slot so staff can prepare it.
- Staff manage slot capacity per store. Customers cannot.

## Contract
- Build on Saleor's existing collection points (warehouses with click & collect). Do not add a parallel checkout flow.
- GraphQL query `pickupSlots(warehouseId: ID!, date: Date!): [PickupSlot!]!` with `PickupSlot { start: DateTime!, end: DateTime!, capacity: Int!, remaining: Int! }`.
- GraphQL mutation `checkoutPickupSlotSet(id: ID!, warehouseId: ID!, start: DateTime!)` returns `{ checkout { pickupSlot { start end } } errors { field code } }`. A full slot fails with error code `PICKUP_SLOT_FULL`.
- Staff mutation `warehousePickupCapacityUpdate(id: ID!, capacityPerHour: Int!)`, which requires `MANAGE_PRODUCTS`.
- `Order.pickupSlot { start end }` is readable on the order after checkout completes.
