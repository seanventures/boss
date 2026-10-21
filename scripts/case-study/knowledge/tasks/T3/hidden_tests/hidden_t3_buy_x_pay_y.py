"""T3 hidden acceptance suite: "buy 3, pay 2" on a category (brief contract).

Never shown to agents. run.py copies this file into the Saleor fork at
saleor/graphql/discount/tests/test_hidden_t3_buy_x_pay_y.py and runs it with Saleor's pytest.
On the pinned SHA it fails (no BUY_X_PAY_Y reward type).
"""
from decimal import Decimal

import graphene

from .....checkout.models import CheckoutLine
from ....tests.utils import get_graphql_content

CREATE = """
mutation Create($input: PromotionCreateInput!) {
  promotionCreate(input: $input) { promotion { id } errors { field code message } }
}
"""
TOTAL = """
query Total($id: ID!) { checkout(id: $id) { totalPrice { gross { amount } } lines { quantity
  totalPrice { gross { amount } } variant { product { category { id } } } } } }
"""


def _promotion(staff_api_client, permission_manage_discounts, channel_USD, category):
    staff_api_client.user.user_permissions.add(permission_manage_discounts)
    content = get_graphql_content(staff_api_client.post_graphql(CREATE, {"input": {
        "name": "Socks: buy 3 pay 2", "type": "CATALOGUE",
        "rules": [{"name": "b3p2", "channels": [graphene.Node.to_global_id("Channel", channel_USD.pk)],
                   "rewardType": "BUY_X_PAY_Y", "buyQuantity": 3, "payQuantity": 2,
                   "cataloguePredicate": {"categoryPredicate": {"ids": [graphene.Node.to_global_id("Category", category.pk)]}}}],
    }}))
    assert content["data"]["promotionCreate"]["errors"] == []


def _fill(checkout, variants_and_qty):
    CheckoutLine.objects.filter(checkout=checkout).delete()
    for variant, qty in variants_and_qty:
        listing = variant.channel_listings.get(channel_id=checkout.channel_id)
        CheckoutLine.objects.create(checkout=checkout, variant=variant, quantity=qty, currency=checkout.currency,
                                    undiscounted_unit_price_amount=listing.price_amount)
    return {v.pk: v.channel_listings.get(channel_id=checkout.channel_id).price_amount for v, _ in variants_and_qty}


def _total(api_client, checkout):
    content = get_graphql_content(api_client.post_graphql(TOTAL, {"id": graphene.Node.to_global_id("Checkout", checkout.pk)}))
    return Decimal(str(content["data"]["checkout"]["totalPrice"]["gross"]["amount"]))


def _expected(prices_by_unit):
    units = sorted(prices_by_unit)
    free = len(units) // 3
    return sum(units) - sum(units[:free])


def test_three_items_across_the_category_make_the_cheapest_free(
        api_client, staff_api_client, permission_manage_discounts, channel_USD, category, checkout, product_list):
    _promotion(staff_api_client, permission_manage_discounts, channel_USD, category)
    variants = [p.variants.first() for p in product_list]
    prices = _fill(checkout, [(v, 1) for v in variants])
    assert _total(api_client, checkout) == _expected([prices[v.pk] for v in variants])


def test_six_items_make_two_free_and_two_items_make_none(
        api_client, staff_api_client, permission_manage_discounts, channel_USD, category, checkout, product_list):
    _promotion(staff_api_client, permission_manage_discounts, channel_USD, category)
    a, b, c = (p.variants.first() for p in product_list)
    prices = _fill(checkout, [(a, 2), (b, 2), (c, 2)])
    units = [prices[a.pk]] * 2 + [prices[b.pk]] * 2 + [prices[c.pk]] * 2
    assert _total(api_client, checkout) == _expected(units)
    prices = _fill(checkout, [(a, 1), (b, 1)])
    assert _total(api_client, checkout) == prices[a.pk] + prices[b.pk]


def test_items_outside_the_category_neither_count_nor_change(
        api_client, staff_api_client, permission_manage_discounts, channel_USD, category, checkout, product_list,
        product_with_single_variant):
    _promotion(staff_api_client, permission_manage_discounts, channel_USD, category)
    from .....product.models import Category

    product_with_single_variant.category = Category.objects.create(name="Shoes", slug="hidden-t3-shoes")
    product_with_single_variant.save(update_fields=["category"])
    other = product_with_single_variant.variants.first()
    a, b = (p.variants.first() for p in product_list[:2])
    prices = _fill(checkout, [(a, 1), (b, 1), (other, 1)])
    assert _total(api_client, checkout) == prices[a.pk] + prices[b.pk] + prices[other.pk]
