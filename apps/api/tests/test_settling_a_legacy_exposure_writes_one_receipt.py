"""Choosing settle on a legacy-exposure card calls resolve_legacy_exposure once.

The method already writes one cost receipt, resolves the decision, and emits
budget.legacy_exposure_resolved. A second settle must surface that refusal, not
write another receipt and not come back as an opaque 500.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services.budget import BudgetGovernor
from engine.tests.test_legacy_financial_exposure import legacy
from engine.tests.test_legacy_provider_exposure import history

PROJECT = "boss-dogfood"


def _hold(engine, kind: str) -> dict:
    if kind == "model":
        legacy(engine, PROJECT, status="released")
        key = "legacy_financial_hold"
    else:
        history(engine, PROJECT)
        key = "legacy_provider_hold"
    governor = BudgetGovernor(Settings(_env_file=None))
    with engine.begin() as connection:
        hold = governor.status_for_project(connection, PROJECT)[key]
    assert hold and hold["decision_id"]
    return hold


def _receipts(engine, project_id: str, reservation_id: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM cost_receipts "
                "WHERE project_id=:p AND reservation_id=:r"
            ),
            {"p": project_id, "r": reservation_id},
        ).scalar_one()


@pytest.mark.parametrize("kind", ["model", "provider"])
def test_the_open_card_offers_exactly_the_two_settles(client, engine, kind):
    hold = _hold(engine, kind)
    card = client.get(f"/v1/decisions/{hold['decision_id']}")
    assert card.status_code == 200, card.text
    offers = [
        (a["action"], a.get("settle_at"), a["label"])
        for a in card.json()["allowed_actions"]
    ]
    assert offers == [
        ("settle", "zero", "Settle at $0 (provider refused)"),
        ("settle", "estimate", "Settle at the estimate"),
    ]


@pytest.mark.parametrize("kind,settle_at,actual", [
    ("model", "zero", 0),
    ("model", "estimate", 80000),
    ("provider", "zero", 0),
])
def test_settling_writes_one_receipt_and_a_second_settle_conflicts(
    client, engine, kind, settle_at, actual,
):
    hold = _hold(engine, kind)
    decision_id = hold["decision_id"]
    first = client.post(
        f"/v1/decisions/{decision_id}/settle",
        json={"actor": "ceo", "settle_at": settle_at},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "resolved"
    assert "by ceo" in (body.get("resolution_note") or "")
    assert _receipts(engine, PROJECT, hold["reservation_id"]) == 1
    with engine.connect() as connection:
        event = connection.execute(
            text(
                "SELECT actor, data FROM events "
                "WHERE project_id=:p AND event_type='budget.legacy_exposure_resolved' "
                "AND data->>'decision_id'=:d"
            ),
            {"p": PROJECT, "d": decision_id},
        ).mappings().one()
    assert event["actor"] == "ceo"
    assert event["data"]["settle_at"] == settle_at
    assert event["data"]["actual_usd_micros"] == actual
    again = client.post(
        f"/v1/decisions/{decision_id}/settle",
        json={"actor": "ceo", "settle_at": settle_at},
    )
    assert again.status_code == 409, again.text
    assert "already resolved" in again.json()["detail"].lower()
    assert _receipts(engine, PROJECT, hold["reservation_id"]) == 1


def test_settling_another_decision_writes_nothing(client, engine):
    from apps.api.tests.test_a_decision_offers_only_what_works import _general_decision

    decision_id = _general_decision(engine)
    card = client.get(f"/v1/decisions/{decision_id}")
    assert card.status_code == 200, card.text
    assert "settle" not in {a["action"] for a in card.json()["allowed_actions"]}
    refused = client.post(
        f"/v1/decisions/{decision_id}/settle",
        json={"actor": "ceo", "settle_at": "zero"},
    )
    assert refused.status_code == 409, refused.text
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"),
            {"p": PROJECT},
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT status FROM decisions WHERE id=:i"),
            {"i": decision_id},
        ).scalar_one() == "pending"


def test_a_stranger_cannot_settle(client, engine):
    hold = _hold(engine, "model")
    refused = client.post(
        f"/v1/decisions/{hold['decision_id']}/settle",
        json={"actor": "not-a-member", "settle_at": "zero"},
    )
    assert refused.status_code in (403, 422), refused.text
    assert _receipts(engine, PROJECT, hold["reservation_id"]) == 0
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM decisions WHERE id=:i"),
            {"i": hold["decision_id"]},
        ).scalar_one() == "pending"
