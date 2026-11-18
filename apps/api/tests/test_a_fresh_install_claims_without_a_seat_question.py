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


def _history(engine, *, decisions=0, runs=0, tasks=0, projects=("boss-dogfood",)) -> bool:
    """install_has_history() against temp tables shadowing the four it reads (rolled back)."""
    from boss_engine.config import get_settings

    default = str(getattr(get_settings(), "default_project_id", "") or "boss-dogfood")
    with engine.connect() as db:
        tx = db.begin()
        try:
            for table in ("decisions", "workflow_runs", "code_task_runs"):
                db.execute(text(f"CREATE TEMP TABLE {table} (id TEXT) ON COMMIT DROP"))
            db.execute(text("CREATE TEMP TABLE projects (id TEXT) ON COMMIT DROP"))
            for table, n in (("decisions", decisions), ("workflow_runs", runs), ("code_task_runs", tasks)):
                for i in range(n):
                    db.execute(text(f"INSERT INTO {table} VALUES (:i)"), {"i": f"x{i}"})
            for p in projects:
                db.execute(text("INSERT INTO projects VALUES (:p)"),
                           {"p": default if p == "boss-dogfood" else p})
            return claim.install_has_history(db)
        finally:
            tx.rollback()


def test_history_is_any_decision_run_task_or_extra_project(engine):
    """One rule for the seat question and the shared password (review of #204): a pre-identity
    install that never approved under a default seat still has runs, decisions or projects."""
    assert _history(engine) is False
    assert _history(engine, decisions=1) is True
    assert _history(engine, runs=1) is True
    assert _history(engine, tasks=1) is True
    assert _history(engine, projects=("boss-dogfood", "shop")) is True
