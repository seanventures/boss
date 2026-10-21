"""T2 hidden acceptance suite: click & collect pickup slots (brief contract).

Never shown to agents. run.py copies this file into the Saleor fork at
saleor/graphql/checkout/tests/test_hidden_t2_pickup_slots.py and runs it with Saleor's pytest.
On the pinned SHA it fails (the schema has no pickup slots).
"""
from datetime import date, datetime, timedelta, timezone

import graphene

from ....tests.utils import assert_no_permission, get_graphql_content

SLOTS = """
query Slots($warehouseId: ID!, $date: Date!) {
  pickupSlots(warehouseId: $warehouseId, date: $date) { start end capacity remaining }
}
"""
SET_SLOT = """
mutation SetSlot($id: ID!, $warehouseId: ID!, $start: DateTime!) {
  checkoutPickupSlotSet(id: $id, warehouseId: $warehouseId, start: $start) {
    checkout { pickupSlot { start end } }
    errors { field code }
  }
}
"""
CAPACITY = """
mutation Capacity($id: ID!, $capacity: Int!) {
  warehousePickupCapacityUpdate(id: $id, capacityPerHour: $capacity) { errors { field code } }
}
"""
TOMORROW = date.today() + timedelta(days=1)


def _gid(kind, pk):
    return graphene.Node.to_global_id(kind, pk)


def _set_capacity(staff_api_client, permission_manage_products, warehouse, capacity):
    staff_api_client.user.user_permissions.add(permission_manage_products)
    content = get_graphql_content(staff_api_client.post_graphql(
        CAPACITY, {"id": _gid("Warehouse", warehouse.pk), "capacity": capacity}))
    assert content["data"]["warehousePickupCapacityUpdate"]["errors"] == []


def _slots(api_client, warehouse):
    content = get_graphql_content(api_client.post_graphql(
        SLOTS, {"warehouseId": _gid("Warehouse", warehouse.pk), "date": TOMORROW.isoformat()}))
    return content["data"]["pickupSlots"]


def test_slots_are_one_hour_with_the_store_capacity(api_client, staff_api_client, permission_manage_products,
                                                    warehouse_for_cc):
    _set_capacity(staff_api_client, permission_manage_products, warehouse_for_cc, 2)
    slots = _slots(api_client, warehouse_for_cc)
    assert slots
    for slot in slots:
        start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
        assert end - start == timedelta(hours=1)
        assert slot["capacity"] == 2 and slot["remaining"] == 2


def test_customers_cannot_change_capacity(api_client, warehouse_for_cc):
    response = api_client.post_graphql(CAPACITY, {"id": _gid("Warehouse", warehouse_for_cc.pk), "capacity": 99})
    assert_no_permission(response)


DELIVERY = """
mutation Delivery($id: ID!, $method: ID!) {
  checkoutDeliveryMethodUpdate(id: $id, deliveryMethodId: $method) { errors { field code } }
}
"""
PLACE = """
mutation Place($id: ID!) {
  orderCreateFromCheckout(id: $id) { order { id pickupSlot { start end } } errors { field code } }
}
"""


def _second_checkout(first, variant):
    from .....checkout.models import Checkout, CheckoutLine

    other = Checkout.objects.create(channel=first.channel, email=first.email, currency=first.currency,
                                    billing_address=first.billing_address.get_copy())
    listing = variant.channel_listings.get(channel_id=other.channel_id)
    CheckoutLine.objects.create(checkout=other, variant=variant, quantity=1, currency=other.currency,
                                undiscounted_unit_price_amount=listing.price_amount)
    return other


def test_a_placed_order_takes_the_slot_and_a_full_slot_is_refused(
        api_client, staff_api_client, app_api_client, permission_manage_products, permission_handle_checkouts,
        warehouse_for_cc, checkout_with_item_for_cc, product_variant_list):
    _set_capacity(staff_api_client, permission_manage_products, warehouse_for_cc, 1)
    slot = _slots(api_client, warehouse_for_cc)[0]
    wh = _gid("Warehouse", warehouse_for_cc.pk)
    first = _gid("Checkout", checkout_with_item_for_cc.pk)
    assert get_graphql_content(api_client.post_graphql(DELIVERY, {"id": first, "method": wh}))[
        "data"]["checkoutDeliveryMethodUpdate"]["errors"] == []
    picked = get_graphql_content(api_client.post_graphql(SET_SLOT, {"id": first, "warehouseId": wh, "start": slot["start"]}))
    assert picked["data"]["checkoutPickupSlotSet"]["errors"] == []
    assert picked["data"]["checkoutPickupSlotSet"]["checkout"]["pickupSlot"]["start"] == slot["start"]

    placed = get_graphql_content(app_api_client.post_graphql(PLACE, {"id": first}, permissions=[permission_handle_checkouts]))
    result = placed["data"]["orderCreateFromCheckout"]
    assert result["errors"] == []
    assert result["order"]["pickupSlot"]["start"] == slot["start"]       # staff can see the slot on the order
    assert [s for s in _slots(api_client, warehouse_for_cc) if s["start"] == slot["start"]][0]["remaining"] == 0

    second = _gid("Checkout", _second_checkout(checkout_with_item_for_cc, product_variant_list[1]).pk)
    refused = get_graphql_content(api_client.post_graphql(SET_SLOT, {"id": second, "warehouseId": wh, "start": slot["start"]}))
    assert [e["code"] for e in refused["data"]["checkoutPickupSlotSet"]["errors"]] == ["PICKUP_SLOT_FULL"]


def test_a_slot_outside_the_store_hours_is_refused(api_client, staff_api_client, permission_manage_products,
                                                   warehouse_for_cc, checkout_with_item_for_cc):
    _set_capacity(staff_api_client, permission_manage_products, warehouse_for_cc, 3)
    start = datetime(TOMORROW.year, TOMORROW.month, TOMORROW.day, 3, 17, tzinfo=timezone.utc).isoformat()
    result = get_graphql_content(api_client.post_graphql(SET_SLOT, {
        "id": _gid("Checkout", checkout_with_item_for_cc.pk), "warehouseId": _gid("Warehouse", warehouse_for_cc.pk),
        "start": start}))["data"]["checkoutPickupSlotSet"]
    assert result["errors"]
    assert result["checkout"] is None or result["checkout"]["pickupSlot"] is None
