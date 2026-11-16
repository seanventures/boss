"""Closing a month records who closed it. Spend inside that month is refused.

An open month still takes spend the way budget does today: a reservation, then
one receipt. Closing the same month again is the same row. A refusal is a
receipt with one dedupe key, not a second close and not a reservation.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.month_close import close_month, spend

URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs DATABASE_URL naming a test database",
)

NOVEMBER = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
OCTOBER = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)
DECEMBER = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)


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
        "org": f"org_month_{sfx}",
        "ws": f"ws_month_{sfx}",
        "p": f"p_month_{sfx}",
    }
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": world["org"]})
        c.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
            {"w": world["ws"], "o": world["org"]},
        )
        c.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:p, :w, :p, :p)"
            ),
            {"p": world["p"], "w": world["ws"]},
        )
    return world


def _closes(c, project_id) -> int:
    return int(
        c.execute(
            text("SELECT count(*) FROM month_closes WHERE project_id = :p"),
            {"p": project_id},
        ).scalar_one()
    )


def _reservations(c, project_id) -> int:
    return int(
        c.execute(
            text("SELECT count(*) FROM budget_reservations WHERE project_id = :p"),
            {"p": project_id},
        ).scalar_one()
    )


def _receipts(c, project_id, dedupe_key) -> list[dict]:
    rows = c.execute(
        text(
            "SELECT id, dedupe_key, effect_kind, status, result, error "
            "FROM receipts WHERE project_id = :p AND dedupe_key = :d"
        ),
        {"p": project_id, "d": dedupe_key},
    ).mappings().all()
    return [dict(row) for row in rows]


def test_a_closed_month_refuses_spend_and_an_open_month_accepts_it(eng, project):
    with eng.begin() as c:
        accepted = spend(
            c,
            project["p"],
            spent_on=NOVEMBER,
            amount_usd_micros=1_500_000,
            spend_key="nov-before-close",
        )
        held_before = _reservations(c, project["p"])
        open_receipts = _receipts(c, project["p"], accepted["dedupe_key"])

    assert accepted["status"] == "accepted"
    assert accepted["reservation_id"]
    assert accepted["receipt_id"]
    assert accepted["dedupe_key"] == f"month-close:{project['p']}:nov-before-close"
    assert held_before == 1
    assert len(open_receipts) == 1
    assert open_receipts[0]["id"] == accepted["receipt_id"]
    assert open_receipts[0]["status"] == "succeeded"
    assert open_receipts[0]["result"]["reservation_id"] == accepted["reservation_id"]

    with eng.begin() as c:
        closed = close_month(c, project["p"], "2026-11", "ada")
        again = close_month(c, project["p"], "2026-11", "bea")
        stored = c.execute(
            text(
                "SELECT id, month, closed_by FROM month_closes WHERE project_id = :p"
            ),
            {"p": project["p"]},
        ).mappings().all()
        refused = spend(
            c,
            project["p"],
            spent_on=NOVEMBER,
            amount_usd_micros=2_000_000,
            spend_key="nov-after-close",
        )
        refused_again = spend(
            c,
            project["p"],
            spent_on=datetime(2026, 11, 30, 18, 0, tzinfo=timezone.utc),
            amount_usd_micros=9_000_000,
            spend_key="nov-after-close",
        )
        october = spend(
            c,
            project["p"],
            spent_on=OCTOBER,
            amount_usd_micros=400_000,
            spend_key="oct-still-open",
        )
        december = spend(
            c,
            project["p"],
            spent_on=DECEMBER,
            amount_usd_micros=800_000,
            spend_key="dec-still-open",
        )
        refusal_rows = _receipts(c, project["p"], refused["dedupe_key"])
        held_after = _reservations(c, project["p"])

    assert closed["month"] == "2026-11"
    assert closed["closed_by"] == "ada"
    assert closed["project_id"] == project["p"]
    assert again["id"] == closed["id"]
    assert again["closed_by"] == "ada"
    assert again["month"] == "2026-11"
    assert len(stored) == 1
    assert stored[0]["id"] == closed["id"]
    assert stored[0]["month"] == "2026-11"
    assert stored[0]["closed_by"] == "ada"

    assert refused["status"] == "refused"
    assert refused["reservation_id"] is None
    assert refused["month"] == "2026-11"
    assert refused["closed_by"] == "ada"
    assert refused["dedupe_key"] == f"month-close:{project['p']}:nov-after-close"
    assert refused_again["receipt_id"] == refused["receipt_id"]
    assert refused_again["dedupe_key"] == refused["dedupe_key"]
    assert len(refusal_rows) == 1
    assert refusal_rows[0]["effect_kind"] == "month_close.spend_refused"
    assert refusal_rows[0]["status"] == "failed"
    assert refusal_rows[0]["error"]["month"] == "2026-11"

    assert october["status"] == "accepted"
    assert october["reservation_id"]
    assert december["status"] == "accepted"
    assert december["reservation_id"]
    assert december["dedupe_key"] != refused["dedupe_key"]
    # The refused November spend did not reserve. The three accepted spends did.
    assert held_after == 3
    assert _closes_from(eng, project["p"]) == 1


def _closes_from(eng, project_id) -> int:
    with eng.begin() as c:
        return _closes(c, project_id)
