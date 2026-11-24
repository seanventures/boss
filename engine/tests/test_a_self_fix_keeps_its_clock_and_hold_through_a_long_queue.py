"""A self-fix that waited 311 minutes for a worker keeps its whole time cap and its budget hold
(R3, BOSSAPP-1431; RCA gap 8).

On a partner's 4-core / 8 GiB host a self-repair was refused after "311 minutes (the most is
60)", all of it queued. `started_at` is now stamped when the build starts; these hold that:
the clock never falls back to `created_at`, and the budget hold reserved at claim (cap + 30
minutes, never renewed) is renewed when the build starts and at every boundary, reserved again
when it already lapsed, and when the budget refuses that the fix stops saying what lapsed and
what to do.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build

from ._self_fix_support import harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def _queued_311_minutes_with_a_lapsed_hold(h, sfx_id: str) -> str:
    with h.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET created_at = now() - interval '311 minutes', "
                       "started_at = NULL WHERE id = :i"), {"i": sfx_id})
        reservation = c.execute(text("SELECT budget_reservation_id FROM self_fixes "
                                     "WHERE id = :i"), {"i": sfx_id}).scalar()
        c.execute(text("UPDATE budget_reservations SET status = 'expired', "
                       "expires_at = now() - interval '221 minutes' WHERE id = :r"),
                  {"r": reservation})
    return str(reservation)


def test_a_fix_queued_past_its_cap_runs_with_a_fresh_hold(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    lapsed = _queued_311_minutes_with_a_lapsed_hold(harness, sfx["id"])
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out.get("refusal") not in ("time", "budget"), out
    with harness.eng.begin() as c:
        row = sf.get(c, sfx["id"])
        hold = c.execute(text("SELECT status FROM budget_reservations WHERE id = :r"),
                         {"r": row["budget_reservation_id"]}).scalar()
    assert row["started_at"] is not None
    assert row["budget_reservation_id"] != lapsed, "the lapsed hold was reserved again"
    assert hold in ("active", "settled", "released")


def test_a_lapsed_hold_the_budget_will_not_renew_stops_and_says_what_to_do(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    _queued_311_minutes_with_a_lapsed_hold(harness, sfx["id"])
    harness.mp.setattr(sf, "_reserve", lambda *a, **k: (None, "over the project's daily cap"))
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["refusal"] == "budget"
    with harness.eng.begin() as c:
        row = sf.get(c, sfx["id"])
    detail = str(row["refusal_detail"])
    assert "budget hold lapsed" in detail and "over the project's daily cap" in detail
    assert "retry the self-fix" in detail


def test_the_time_cap_never_falls_back_to_when_it_was_queued(harness):
    """No `started_at` means the build has not started: `created_at` is never its clock."""
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET created_at = now() - interval '311 minutes', "
                       "started_at = NULL WHERE id = :i"), {"i": sfx["id"]})
    build = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                                 source=harness.mirror)
    build.boundary()                             # raises Stop("time") on the old fallback
