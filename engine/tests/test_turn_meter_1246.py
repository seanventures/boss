"""BOSSAPP-1246: elapsed work and settled spend on a builder turn.

Settled money is the sum of existing cost receipts for that turn. A missing
receipt is not $0. More credit pauses for a check-in. Declining does not
reserve budget. Approving reserves once and writes one receipt. A repeat does
not double-spend. A refusal is a visible error and a dead letter.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.turn_meter import (
    answer_credit,
    open_turn,
    read_meter,
    request_credit,
)

URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs DATABASE_URL naming a test database",
)

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def project(eng):
    sfx = uuid4().hex[:8]
    world = {
        "org": f"org_1246_{sfx}",
        "ws": f"ws_1246_{sfx}",
        "p": f"p_1246_{sfx}",
    }
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": world["org"]})
        c.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
            {"w": world["ws"], "o": world["org"]},
        )
        c.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"
            ),
            {"p": world["p"], "w": world["ws"]},
        )
        c.execute(
            text(
                "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
                "VALUES (:i, 'project', :p, :cap, :cap, 0)"
            ),
            {"i": f"bp_1246_{sfx}", "p": world["p"], "cap": 50_000_000},
        )
    return world


def _receipt(c, world, turn_id, *, actual, included=False, estimated=100):
    receipt_id = f"rcpt_{uuid4().hex[:12]}"
    c.execute(
        text(
            "INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, "
            "effect_kind, effect_scope, payload_hash, status) VALUES ("
            ":id, :o, :w, :p, :d, 'model_call', CAST(:scope AS JSONB), 'hash', 'succeeded')"
        ),
        {
            "id": receipt_id,
            "o": world["org"],
            "w": world["ws"],
            "p": world["p"],
            "d": f"existing:{receipt_id}",
            "scope": '{"turn_id": "%s"}' % turn_id,
        },
    )
    c.execute(
        text(
            "INSERT INTO cost_receipts (id, project_id, receipt_id, category, "
            "skill_or_model_id, estimated_usd_micros, actual_usd_micros, status, "
            "included_in_subscription) VALUES ("
            ":id, :p, :r, 'model', 'fixture', :est, :act, :st, :inc)"
        ),
        {
            "id": f"cr_{uuid4().hex[:12]}",
            "p": world["p"],
            "r": receipt_id,
            "est": estimated,
            "act": actual,
            "st": "actual" if actual is not None else "estimated",
            "inc": included,
        },
    )


def _reservations(c, project_id) -> int:
    return int(
        c.execute(
            text(
                "SELECT count(*) FROM budget_reservations "
                "WHERE project_id = :p AND status = 'active'"
            ),
            {"p": project_id},
        ).scalar_one()
    )


def _receipts_for(c, project_id, dedupe_key) -> int:
    return int(
        c.execute(
            text(
                "SELECT count(*) FROM receipts WHERE project_id = :p AND dedupe_key = :d"
            ),
            {"p": project_id, "d": dedupe_key},
        ).scalar_one()
    )


def test_elapsed_work_is_the_clock_and_unset_spend_is_not_zero(eng, project):
    with eng.begin() as c:
        turn = open_turn(c, project["p"], turn_id=f"trn_clock_{project['p']}", started_at=T0)
        view = read_meter(c, project["p"], turn["turn_id"], now=T0 + timedelta(seconds=90))
    assert view["elapsed_ms"] == 90_000
    assert view["settled_usd_micros"] is None
    assert view["settled_known"] is False


def test_settled_spend_sums_existing_receipts_only(eng, project):
    with eng.begin() as c:
        turn = open_turn(c, project["p"], turn_id=f"trn_sum_{project['p']}", started_at=T0)
        _receipt(c, project, turn["turn_id"], actual=250_000)
        _receipt(c, project, turn["turn_id"], actual=50_000)
        _receipt(c, project, turn["turn_id"], actual=999_999, included=True)
        _receipt(c, project, turn["turn_id"], actual=None, estimated=8_000_000)
        _receipt(c, project, f"trn_other_{project['p']}", actual=777_000)
        view = read_meter(c, project["p"], turn["turn_id"], now=T0)
    assert view["settled_usd_micros"] == 300_000
    assert view["settled_known"] is True


def test_included_receipt_settles_at_zero_without_inventing_list_price(eng, project):
    with eng.begin() as c:
        turn = open_turn(c, project["p"], turn_id=f"trn_inc_{project['p']}", started_at=T0)
        _receipt(c, project, turn["turn_id"], actual=999_999, included=True)
        view = read_meter(c, project["p"], turn["turn_id"], now=T0)
    assert view["settled_known"] is True
    assert view["settled_usd_micros"] == 0


def test_credit_checkin_pauses_before_any_reservation(eng, project):
    with eng.begin() as c:
        turn = open_turn(c, project["p"], started_at=T0)
        before = _reservations(c, project["p"])
        view = request_credit(
            c, project["p"], turn["turn_id"], "slice-1", 1_000_000, now=T0
        )
        after = _reservations(c, project["p"])
    assert view["status"] == "paused_for_credit"
    assert view["checkin"]["status"] == "pending"
    assert view["checkin"]["reservation_id"] is None
    assert after == before


def test_declined_checkin_does_not_reserve_budget(eng, project):
    with eng.begin() as c:
        turn = open_turn(c, project["p"], started_at=T0)
        request_credit(c, project["p"], turn["turn_id"], "slice-no", 2_000_000, now=T0)
        view = answer_credit(
            c, project["p"], turn["turn_id"], "slice-no", "decline", now=T0
        )
        again = answer_credit(
            c, project["p"], turn["turn_id"], "slice-no", "approve", now=T0
        )
        held = _reservations(c, project["p"])
    assert view["checkin"]["status"] == "declined"
    assert view["checkin"]["reservation_id"] is None
    assert again["checkin"]["status"] == "declined"
    assert again["checkin"]["reservation_id"] is None
    assert held == 0


def test_approved_checkin_reserves_once_and_one_receipt(eng, project):
    with eng.begin() as c:
        turn = open_turn(c, project["p"], started_at=T0)
        request_credit(c, project["p"], turn["turn_id"], "slice-yes", 1_500_000, now=T0)
        first = answer_credit(
            c, project["p"], turn["turn_id"], "slice-yes", "approve", now=T0
        )
        second = answer_credit(
            c, project["p"], turn["turn_id"], "slice-yes", "approve", now=T0
        )
        replay = request_credit(
            c, project["p"], turn["turn_id"], "slice-yes", 1_500_000, now=T0
        )
        dedupe = first["checkin"]["dedupe_key"]
        held = _reservations(c, project["p"])
        written = _receipts_for(c, project["p"], dedupe)
        # The reservation is not settled money. Nothing invented a dollar.
        meter = read_meter(c, project["p"], turn["turn_id"], now=T0)
    assert first["checkin"]["status"] == "approved"
    assert first["checkin"]["reservation_id"]
    assert first["checkin"]["receipt_id"]
    assert second["checkin"]["receipt_id"] == first["checkin"]["receipt_id"]
    assert second["checkin"]["reservation_id"] == first["checkin"]["reservation_id"]
    assert replay["checkin"]["receipt_id"] == first["checkin"]["receipt_id"]
    assert held == 1
    assert written == 1
    assert dedupe == f"turn-meter:{project['p']}:{turn['turn_id']}:slice-yes"
    assert meter["settled_usd_micros"] is None


def test_a_refused_reservation_is_visible_and_dead_lettered(eng, project):
    with eng.begin() as c:
        c.execute(
            text(
                "UPDATE budget_policies SET daily_cap_usd_micros = 1000, "
                "monthly_cap_usd_micros = 1000 WHERE scope_id = :p"
            ),
            {"p": project["p"]},
        )
        turn = open_turn(c, project["p"], started_at=T0)
        request_credit(c, project["p"], turn["turn_id"], "too-much", 5_000_000, now=T0)
        view = answer_credit(
            c, project["p"], turn["turn_id"], "too-much", "approve", now=T0
        )
        held = _reservations(c, project["p"])
        letters = int(
            c.execute(
                text(
                    "SELECT count(*) FROM deadletters WHERE project_id = :p "
                    "AND source_kind = 'builder_turn'"
                ),
                {"p": project["p"]},
            ).scalar_one()
        )
    assert view["checkin"]["status"] == "failed"
    assert view["checkin"]["reservation_id"] is None
    assert view["error"]
    assert view["checkin"]["deadletter_id"]
    assert held == 0
    assert letters == 1
