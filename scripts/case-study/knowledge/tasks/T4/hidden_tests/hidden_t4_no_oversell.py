"""T4 hidden acceptance suite: allocation never takes more stock than is available.

Never shown to agents. run.py copies this file into the Saleor fork at
saleor/warehouse/tests/test_hidden_t4_no_oversell.py and runs it with Saleor's own pytest
setup, after BOSS's fix. On the faulty release (fault.patch) it fails.
"""
import pytest

from saleor.core.exceptions import InsufficientStock
from saleor.order.fetch import OrderLineInfo
from saleor.warehouse.management import allocate_stocks
from saleor.warehouse.models import Allocation, Stock

COUNTRY_CODE = "US"


def _allocate(order_line, quantity, channel, site_settings):
    allocate_stocks([OrderLineInfo(line=order_line, variant=order_line.variant, quantity=quantity)],
                    COUNTRY_CODE, channel, site_settings=site_settings, requestor=None,
                    calculate_stocks_with_shipping_zones=True)


@pytest.mark.parametrize(("in_stock", "requested"), [(5, 6), (1, 2)])
def test_allocating_more_than_stock_is_refused(order_line, stock, channel_USD, site_settings, in_stock, requested):
    stock.quantity = in_stock
    stock.save(update_fields=["quantity"])
    with pytest.raises(InsufficientStock):
        _allocate(order_line, requested, channel_USD, site_settings)
    assert not Allocation.objects.filter(order_line=order_line).exists()


def test_exactly_the_available_quantity_can_be_allocated(order_line, stock, channel_USD, site_settings):
    stock.quantity = 3
    stock.save(update_fields=["quantity"])
    _allocate(order_line, 3, channel_USD, site_settings)
    stock.refresh_from_db()
    assert stock.quantity_allocated == 3


def test_units_allocated_elsewhere_are_not_available(order_line, order_with_lines, stock, channel_USD, site_settings):
    stock.quantity = 4
    stock.quantity_allocated = 0
    stock.save(update_fields=["quantity", "quantity_allocated"])
    other = order_with_lines.lines.first()
    Allocation.objects.create(order_line=other, stock=stock, quantity_allocated=3)
    Stock.objects.filter(pk=stock.pk).update(quantity_allocated=3)
    with pytest.raises(InsufficientStock):
        _allocate(order_line, 2, channel_USD, site_settings)
    _allocate(order_line, 1, channel_USD, site_settings)
    stock.refresh_from_db()
    assert stock.quantity_allocated == stock.quantity == 4


def test_allocation_never_exceeds_stock_across_many_orders(order_line, stock, channel_USD, site_settings):
    stock.quantity = 2
    stock.save(update_fields=["quantity"])
    for _ in range(4):
        try:
            _allocate(order_line, 1, channel_USD, site_settings)
        except InsufficientStock:
            pass
    stock.refresh_from_db()
    assert stock.quantity_allocated <= stock.quantity
