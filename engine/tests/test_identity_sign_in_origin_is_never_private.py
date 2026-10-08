"""A sign-in notice never names a private or container address (where.py).

The console showed "New sign-in from 172.18.0.8": the console container's address on the compose
network, not the person's. Only a public address is worth naming; anything else is "a new device".
"""
from __future__ import annotations

import pytest

from boss_engine.identity.where import public_address, sign_in_origin


@pytest.mark.parametrize("ip", [
    "172.18.0.8", "172.16.0.1", "172.31.255.254", "10.0.0.5", "192.168.1.20", "127.0.0.1",
    "100.93.58.20", "169.254.1.1", "::1", "fd7a:115c:a1e0::1", "fe80::1", "::ffff:172.18.0.8",
    "0.0.0.0", "", None, "not-an-ip", "fe80::1%eth0",
])
def test_a_private_internal_or_unknown_address_is_a_new_device(ip):
    assert public_address(ip) is None
    assert sign_in_origin(ip) == "from a new device"


@pytest.mark.parametrize("ip,shown", [
    ("203.0.113.9", "203.0.113.9"), ("8.8.8.8", "8.8.8.8"), ("172.32.0.1", "172.32.0.1"),
    ("2001:4860:4860::8888", "2001:4860:4860::8888"), ("::ffff:8.8.4.4", "8.8.4.4"),
])
def test_a_public_address_is_named(ip, shown):
    assert public_address(ip) == shown
    assert sign_in_origin(ip) == f"from {shown}"
