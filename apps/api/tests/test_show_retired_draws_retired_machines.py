"""Show retired shows retired machines (P-I-45).

The console's "Show retired" toggled a client filter over racks the fleet view had already
dropped, so it revealed nothing. The view now sends a retired machine as a rack with the Retired
word, no residents and nothing to press; the console hides it until asked.
"""
from __future__ import annotations

import pytest

from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    yield
    with engine.begin() as c:
        clear(c, HOME)


def test_the_fleet_sends_retired_machines_marked_retired(client, world):
    body = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    racks = {r["id"]: r for r in body["racks"]}
    assert "h_retired" in racks, "a retired machine is never sent, so Show retired shows nothing"
    retired = racks["h_retired"]
    assert retired["state"] == {"word": "Retired", "tone": "none"}
    assert retired["retired"] is True
    assert retired["residents"] == [] and retired["stage_action"] is None
    assert body["retired_count"] == 1
    # Last, after every live machine.
    assert body["racks"][-1]["id"] == "h_retired"
