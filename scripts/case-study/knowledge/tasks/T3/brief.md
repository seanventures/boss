# T3 · "Buy 3, pay 2" on a category

**From:** NorthPeak Retail, merchandising · **Repository:** `northpeak-saleor`

We want to run "buy 3, pay 2" on a whole category, for example Socks: for every three items from
the category in the basket, the cheapest one is free.

What matters to us:
- It counts items across products in the category. Three different socks count.
- For every 3 qualifying items, 1 is free: 6 items give 2 free. The free ones are always the cheapest.
- Items from other categories in the same basket are not affected and do not count.
- It does not stack with staff discounts.
- It must show as a promotion in the dashboard like our other promotions.

## Contract
- A new Saleor promotion rule reward type `BUY_X_PAY_Y` with `rewardValue` unused, plus fields `buyQuantity: Int!` and `payQuantity: Int!` on the rule. Create it through the existing `promotionCreate` / `promotionRuleCreate` mutations (catalogue predicate on a category).
- The checkout's and the order's `discounts` and line `unitPrice` reflect the free items. The checkout `totalPrice` equals the sum of what the customer pays.
