"""P0: a self-fix's budget is reserved before it starts and capped while it runs (BOSSAPP-571
R12, spec §8.4). BOSS's spend on itself is its own category, never the step's recovery budget;
the month has a ceiling; spent → refused: budget, and the recommendation stays."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build

from ._self_fix_support import harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_the_budget_is_reserved_under_its_own_category(harness):
    out = harness.propose()
    sfx = out["self_fix"]
    with harness.eng.begin() as c:
        reservation = c.execute(text(
            "SELECT category, amount_usd_micros, status FROM budget_reservations WHERE id = :i"),
            {"i": sfx["budget_reservation_id"]}).mappings().first()
    assert reservation["category"] == "self_fix" and reservation["status"] == "active"
    assert reservation["amount_usd_micros"] == 1000 * 10_000 == sfx["budget_usd_micros"]


def test_a_refused_budget_means_no_self_fix(harness):
    harness.mp.setattr(sf, "_reserve", lambda *a, **k: (None, "over the project's cap"))
    out = harness.propose()
    assert out["outcome"] == "skipped" and "cap" in out["why"]


def test_the_month_has_a_ceiling(harness):
    with harness.eng.begin() as c:
        sf.set_switch(c, on=True, actor="test", settings_patch={"self_fix_month_cents": 500})
    out = harness.propose()
    assert out["outcome"] == "skipped" and "this month" in out["why"]


def test_spending_the_budget_mid_build_refuses_it(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET budget_usd_micros = 1 WHERE id = :i"),
                  {"i": sfx["id"]})
    build = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                                 source=harness.mirror)
    build.cost_micros = 5
    out = build.run()
    assert out["refusal"] == "budget"
    with harness.eng.begin() as c:
        row = sf.get(c, sfx["id"])
        rec = c.execute(text("SELECT status FROM boss_recommendations WHERE id = :i"),
                        {"i": row["recommendation_id"]}).scalar()
    assert row["status"] == sf.REFUSED and rec == "open"


def test_the_time_cap_refuses_it(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET started_at = now() - interval '2 hours' "
                       "WHERE id = :i"), {"i": sfx["id"]})
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["refusal"] == "time"


def test_a_finished_fix_holds_what_it_cost_not_its_reservation(harness):
    """Live install, 2026-10-24: three refused fixes that cost $15.47 held $30 of a $50 month,
    so a fourth fix found 'no room'. A finished fix spends nothing more: it counts its cost."""
    sfx = harness.propose()["self_fix"]
    reserved = int(sfx["budget_usd_micros"])
    with harness.eng.begin() as c:
        assert sf.month_spent(c) == reserved  # still able to spend: the whole reservation
        c.execute(text("UPDATE self_fixes SET status = 'refused', cost_micros = 1000000 "
                       "WHERE id = :i"), {"i": sfx["id"]})
        assert sf.month_spent(c) == 1_000_000
        c.execute(text("UPDATE self_fixes SET status = 'not_resolved' WHERE id = :i"),
                  {"i": sfx["id"]})
        assert sf.month_spent(c) == 1_000_000
        c.execute(text("UPDATE self_fixes SET status = 'awaiting_approval' WHERE id = :i"),
                  {"i": sfx["id"]})
        assert sf.month_spent(c) == reserved  # repair rounds may still spend
