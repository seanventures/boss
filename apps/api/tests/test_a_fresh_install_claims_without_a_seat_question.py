"""A FRESH INSTALL CLAIMS WITHOUT "WHICH SEAT WERE YOU?" (stranger test, rc10.27).

The claim form asks which legacy seat (ceo, cto, ...) the first Owner was only when the install
really has history under that seat: an upgrade where `ceo` approved decisions. A fresh install has
none, so `/v1/auth/options` sends no seats and the console does not ask.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.identity import claim


def _with_decisions(engine, rows):
    """legacy_seats() against a decisions table holding exactly `rows` (a temp table shadows the
    real one inside this transaction, which is rolled back: nothing real is touched)."""
    with engine.connect() as db:
        tx = db.begin()
        try:
            db.execute(text("CREATE TEMP TABLE decisions (approved_by TEXT, delegated_by TEXT, "
                            "revision_requested_by TEXT, executed_by TEXT) ON COMMIT DROP"))
            for r in rows:
                db.execute(text("INSERT INTO decisions VALUES (:a, :d, :r, :e)"),
                           {"a": r.get("approved_by"), "d": r.get("delegated_by"),
                            "r": r.get("revision_requested_by"), "e": r.get("executed_by")})
            return claim.legacy_seats(db)
        finally:
            tx.rollback()


def test_a_fresh_install_offers_no_seat(engine):
    assert _with_decisions(engine, []) == []


def test_decisions_by_agents_or_people_are_not_seats(engine):
    assert _with_decisions(engine, [{"approved_by": "usr_abc", "executed_by": "agent:builder"}]) == []


def test_an_upgrade_offers_only_the_seats_with_history_in_seat_order(engine):
    rows = [{"approved_by": "cto"}, {"executed_by": "ceo"}, {"revision_requested_by": "cto"},
            {"delegated_by": "release_manager"}]
    assert _with_decisions(engine, rows) == ["ceo", "cto", "release_manager"]
