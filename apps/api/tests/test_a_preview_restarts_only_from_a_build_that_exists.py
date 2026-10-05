"""A preview's Restart is offered only when its build still exists (independent walk, item 12).

Restart starts a fresh preview of the same build. When the build's run was gone, the button was
still offered and the press answered 404 "code task run not found". The previews view now offers
Restart only where it can work, and says why where it cannot.
"""
from __future__ import annotations

import pytest

from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)  # pv_live's and pv_fail's builds exist; pv_ended's is gone
    yield
    with engine.begin() as c:
        clear(c, HOME)


def test_restart_is_offered_only_for_a_build_that_exists(client, world):
    body = client.get("/v1/infra/previews", params={"project_id": HOME}).json()
    rows = {r["id"]: r for group in ("live", "failed_24h", "ended_24h") for r in body[group]}
    assert rows["pv_live"]["can"]["restart"] is True
    assert rows["pv_live"].get("restart_why") is None
    assert rows["pv_fail"]["can"]["restart"] is True
    gone = rows["pv_ended"]
    assert gone["can"]["restart"] is False, "Restart offered for a build that no longer exists"
    assert gone["restart_why"] == "Its build no longer exists, so it cannot start again"
