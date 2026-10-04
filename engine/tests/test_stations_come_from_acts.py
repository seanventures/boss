"""The Line draws who really worked each station (factory spec §4.3, §8.1).

A face is an act's role. A pending station shows a hollow face only when its role is certain,
never drawn as working. Machinery (Check without a qa act, PR, Preview) is BOSS's glyph, never
an agent. A lean build's skipped stations say so. A repair round is counted on the edge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.services import factory_view as fv

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def run(status="running", phase="implement", **kw):
    return {"id": "crun_x", "project_id": "p", "status": status, "phase": phase, "title": "T",
            "spec": kw.pop("spec", {}), "state": kw.pop("state", {}),
            "finalize_ctx": kw.pop("fctx", {}), "error": None,
            "created_at": (NOW - timedelta(minutes=30)).isoformat(),
            "updated_at": (NOW - timedelta(minutes=1)).isoformat(), **kw}


def act(step, role, status="complete", minutes_ago=10, seat=None):
    start = NOW - timedelta(minutes=minutes_ago)
    return {"act_id": f"act_{step}_{seat}_{minutes_ago}", "kind": "stage", "step_id": step,
            "role_key": role, "status": status, "seat": seat, "how": "next_stage",
            "handed_by_kind": "boss", "label": step, "started_at": start,
            "ended_at": None if status == "active" else start + timedelta(minutes=1)}


def line(r, acts, **kw):
    return {s["key"]: s for s in fv.stations_for(r, acts=acts, now=NOW, **kw)}


def test_an_operator_named_engineer_stands_at_build():
    acts = [act("plan", "chief_architect", minutes_ago=20), act("write_tests", "qa", minutes_ago=15),
            act("implement", "frontend_engineer", "active", minutes_ago=5)]
    got = line(run(), acts)
    assert got["build"]["face"] == "agent"
    assert got["build"]["role_key"] == "frontend_engineer"
    assert got["build"]["status"] == "working"
    assert got["plan"]["status"] == "done" and got["plan"]["role_key"] == "chief_architect"


def test_a_pending_review_is_hollow_and_never_working():
    got = line(run(), [act("implement", "software_engineer", "active", minutes_ago=5)])
    review = got["review"]
    assert review["status"] == "pending"
    assert review["face"] == "hollow"
    assert review["hollow_role"] == "adversarial_reviewer"
    assert review["role_key"] is None


def test_machinery_is_boss_never_an_agent():
    got = line(run(), [act("implement", "software_engineer", "active", minutes_ago=5)])
    for key in ("check", "pr", "preview"):
        assert got[key]["face"] in ("boss", "none"), key
        assert got[key]["role_key"] is None, key
    done = line(run("completed", "done", pull_request_url="https://x/pull/9",
                    pull_request_number=9),
                [act("implement", "software_engineer", minutes_ago=9)])
    assert done["check"]["face"] == "boss" and done["check"]["role_key"] is None
    assert done["pr"]["links"] == [{"label": "#9 ↗", "url": "https://x/pull/9"}]


def test_a_lean_build_skips_tests_and_review():
    r = run("completed", "done", state={"profile": "lean", "steps_run": ["implement"]},
            pull_request_url="https://x/pull/4", pull_request_number=4)
    got = line(r, [act("implement", "software_engineer", minutes_ago=9)])
    assert got["tests"]["status"] == "skipped"
    assert got["tests"]["tip"] == "Lean profile skips Tests"
    assert got["review"]["status"] == "skipped"
    k, n = fv.k_of_n(list(got.values()))
    # Plan, Tests and Review did not run: five stations were planned, not eight.
    assert n == 5 and k <= n


def test_a_repair_round_increments_rounds():
    acts = [act("implement", "software_engineer", minutes_ago=20),
            act("review_code", "adversarial_reviewer", minutes_ago=15),
            act("repair", "software_engineer", "active", minutes_ago=3)]
    got = line(run(phase="convergence"), acts)
    assert got["build"]["rounds"] == 1
    assert got["build"]["loop_with"] == "adversarial_reviewer"


def test_each_review_seat_has_its_own_face():
    acts = [act("implement", "software_engineer", minutes_ago=20),
            act("review_code", "adversarial_reviewer", "active", 5, seat=1),
            act("review_code", "adversarial_reviewer", "active", 5, seat=2),
            act("security_check", "security_reviewer", "active", 5)]
    got = line(run(phase="review_panel"), acts)
    seats = got["review"]["seats"]
    assert [s["role_key"] for s in seats] == ["adversarial_reviewer", "adversarial_reviewer",
                                              "security_reviewer"]
    assert [s["seat"] for s in seats] == [1, 2, 3]


def test_a_failed_gate_turns_its_seat_stuck():
    acts = [act("review_code", "adversarial_reviewer", "awaiting_input", 5),
            act("security_check", "security_reviewer", "awaiting_input", 5)]
    r = run("blocked", "awaiting_gate_override", fctx={"failing_gates": ["code_review_passed"]},
            state={"adversarial_verdict": "refuted", "security_verdict": "clean"})
    got = line(r, acts)
    assert got["review"]["status"] == "stuck"
    verdicts = {s["role_key"]: s["verdict"] for s in got["review"]["seats"]}
    assert verdicts == {"adversarial_reviewer": "blocked", "security_reviewer": "approved"}


def test_plan_approval_adds_the_you_station():
    got = line(run("blocked", "awaiting_plan_approval"),
               [act("plan", "chief_architect", minutes_ago=4)])
    assert got["you_plan"]["status"] == "needs_you" and got["you_plan"]["face"] == "you"
    assert got["plan"]["status"] == "done"


def test_cancelled_skips_what_is_left():
    got = line(run("cancelled", "implement", fctx={"cancelled_by": "ceo"}),
               [act("implement", "software_engineer", "skipped", 5)])
    assert got["build"]["status"] == "skipped"
    assert all(got[k]["status"] == "skipped" for k in ("check", "review", "pr", "preview", "ship"))


def test_k_never_passes_n():
    for r, acts in [(run(), []), (run("completed", "done"), [])]:
        k, n = fv.k_of_n(fv.stations_for(r, acts=acts, now=NOW))
        assert 0 <= k <= n


def test_a_skipped_station_says_why_while_the_build_runs():
    """D15: a running build's passed Plan said "No Plan act". It says the run's own reason."""
    got = line(run(spec={"risk_tier": 1}), [act("implement", "software_engineer", "active", 2)])
    assert got["plan"]["status"] == "skipped"
    assert got["plan"]["tip"] == "Small change: no planning step"
    plain = line(run(), [act("implement", "software_engineer", "active", 2)])
    assert plain["plan"]["tip"] == "This build skipped Plan"


def test_a_pending_check_does_not_claim_boss_before_it_runs():
    """D15: a pending Check drew the BOSS glyph, then flipped to QA's face when QA ran it."""
    got = line(run(), [act("implement", "software_engineer", "active", 2)])
    assert got["check"]["status"] == "pending" and got["check"]["face"] == "none"
    assert got["pr"]["face"] == "boss"
