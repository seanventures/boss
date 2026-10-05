"""Live is what is running; a newer failed attempt is said beside it (operator decision 2026-10-04).

`Live` stays the newest SUCCEEDED deploy of a target — that is what serves traffic. When the
target's newest attempt of any outcome failed after it, the Live deploy carries `failed_after`
{at, ref} on the rail row and on the Fleet tile, so the console can say "Last attempt failed ·
2h ago" next to it. A failure followed by a newer success is history and is not flagged.
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


def _residents(client):
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    return {r["id"]: r for rack in fleet["racks"] for r in rack["residents"]}


def test_a_live_deploy_with_a_newer_failure_says_so(client, world):
    rows = {r["ref"]: r for r in client.get("/v1/infra/deploys", params={"project_id": HOME}).json()["rows"]}
    # t_stack: sd_ok went live, sd_failed is newer and failed.
    assert rows["stack:sd_ok"]["word"] == "Live"
    assert rows["stack:sd_ok"]["failed_after"]["ref"] == "stack:sd_failed"
    assert rows["stack:sd_failed"]["failed_after"] is None, "only the Live row carries it"
    tile = _residents(client)["t_stack"]["last_deploy"]
    assert tile["ref"] == "stack:sd_ok" and tile["failed_after"]["ref"] == "stack:sd_failed"


def test_a_newest_success_is_not_flagged(client, world):
    rows = {r["ref"]: r for r in client.get("/v1/infra/deploys", params={"project_id": HOME}).json()["rows"]}
    live = [r for r in rows.values() if r["word"] == "Live" and r["target_id"] == "t_live"]
    assert live and live[0]["failed_after"] is None
    assert _residents(client)["t_live"]["last_deploy"]["failed_after"] is None


def test_redeploy_and_rollback_are_not_hidden_by_edition(client, world):
    """Operator decision 2026-10-04: they go through the governed path in every edition."""
    rows = client.get("/v1/infra/deploys", params={"project_id": HOME}).json()["rows"]
    delivery_done = [r for r in rows if r["kind"] == "delivery" and r["state"] in ("succeeded", "failed")]
    assert delivery_done and any(r["can"]["redeploy"] for r in delivery_done)
