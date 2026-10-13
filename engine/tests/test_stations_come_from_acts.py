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


def test_a_failed_gate_fails_its_seat():
    acts = [act("review_code", "adversarial_reviewer", "awaiting_input", 5),
            act("security_check", "security_reviewer", "awaiting_input", 5)]
    r = run("blocked", "awaiting_gate_override", fctx={"failing_gates": ["code_review_passed"]},
            state={"adversarial_verdict": "refuted", "security_verdict": "clean"})
    got = line(r, acts)
    # FAILED, never "stuck": the checks failed and wait on the operator's waiver.
    assert got["review"]["status"] == "failed" and got["review"]["word"] == "Failed"
    assert got["review"]["tip"].startswith("Checks failed")
    verdicts = {s["role_key"]: s["verdict"] for s in got["review"]["seats"]}
    assert verdicts == {"adversarial_reviewer": "blocked", "security_reviewer": "approved"}


def test_plan_approval_adds_the_you_station():
    got = line(run("blocked", "awaiting_plan_approval"),
               [act("plan", "chief_architect", minutes_ago=4)])
    assert got["you_plan"]["status"] == "needs_you" and got["you_plan"]["face"] == "you"
    assert got["plan"]["status"] == "done"


def test_cancelled_stops_where_it_was_and_reaches_nothing_after():
    got = line(run("cancelled", "implement", fctx={"cancelled_by": "ceo"}),
               [act("implement", "software_engineer", "skipped", 5)])
    assert (got["build"]["status"], got["build"]["word"]) == ("stopped", "Stopped")
    for k in ("check", "review", "pr", "preview", "ship"):
        assert (got[k]["status"], got[k]["word"], got[k].get("not_reached")) == \
            ("pending", "Not reached", True), k
        assert got[k]["face"] == "none" and got[k]["hollow_role"] is None, k
    # Still planned: a stop never shrinks the count. Plan and Tests were passed (skipped);
    # Build and the five it never reached are the six planned.
    assert fv.k_of_n(list(got.values()))[1] == 6


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


def test_a_queued_build_waits_at_its_first_station_with_boss():
    """Walk D22: a queued build drew an empty line. It waits at its first station for a worker,
    as BOSS's hourglass — never an agent face claiming work."""
    stations = line(run(phase="queued"), [])
    first = next(iter(stations.values()))
    assert first["status"] == "waiting" and first["face"] == "boss"
    assert first["tip"] == "Waiting for a worker" and first.get("role_key") is None
    assert not any(s["status"] == "working" for s in stations.values())


# ── HONEST STAGE LABELS (operator, 2026-10-13) ────────────────────────────────────────────────
# The strip said "Stuck · 29s" on a Check that had FAILED (the app did not start) and
# "Stuck · 3m" on a Review whose act never closed because the build was BLOCKED. Failed says
# failed with the reason; work left open by a stop says Stopped; what the build never got to
# says Not reached; Stuck is only a live build's station gone quiet.


def test_a_failed_check_says_failed_with_the_reason_and_review_says_stopped():
    acts = [act("plan", "chief_architect", minutes_ago=30), act("implement", "software_engineer",
                                                                 minutes_ago=20),
            act("serve_check", None, "error", minutes_ago=8),
            act("review_code", "adversarial_reviewer", "active", minutes_ago=3)]
    r = run("blocked", "review_code", error="app didn't start: port 8000 never answered")
    got = line(r, acts)
    assert (got["check"]["status"], got["check"]["word"]) == ("failed", "Failed")
    assert got["check"]["tip"] == "app didn't start: port 8000 never answered"
    assert (got["review"]["status"], got["review"]["word"]) == ("stopped", "Stopped")
    for k in ("pr", "preview", "ship"):
        assert (got[k]["word"], got[k].get("not_reached")) == ("Not reached", True), k
    assert not [s for s in got.values() if s["status"] == "stuck" or s["word"] == "Stuck"]
    assert not [k for k in ("pr", "preview", "ship") if got[k]["word"] == "Next"]


def test_a_failed_build_fails_its_station_and_reaches_nothing_after():
    acts = [act("implement", "software_engineer", minutes_ago=20),
            act("serve_check", None, "error", minutes_ago=5)]
    r = run("failed", "serve_check", error="app didn't start\ntraceback…")
    got = line(r, acts)
    assert (got["check"]["status"], got["check"]["word"], got["check"]["tip"]) == \
        ("failed", "Failed", "app didn't start")
    for k in ("review", "pr", "preview", "ship"):
        assert got[k]["word"] == "Not reached", k


def test_a_build_stopped_by_its_budget_stops_at_its_station():
    acts = [act("implement", "software_engineer", minutes_ago=20)]
    r = run("blocked", "implement", fctx={"reason_code": "budget_denied"})
    got = line(r, acts)
    here = next(s for s in got.values() if s["status"] in ("stopped", "failed", "stuck"))
    assert (here["status"], here["word"]) == ("stopped", "Stopped")


def test_stuck_is_only_a_live_build_gone_quiet():
    # Live and moving: working, never stuck.
    acts = [act("implement", "software_engineer", "active", minutes_ago=5)]
    moving = run(updated_at=(NOW - timedelta(minutes=1)).isoformat())
    assert line(moving, acts)["build"]["status"] == "working"
    # Live, and nothing for longer than QUIET_AFTER_MINUTES: stuck, saying for how long.
    quiet_for = fv.QUIET_AFTER_MINUTES + 5
    old = [act("implement", "software_engineer", "active", minutes_ago=quiet_for)]
    quiet = run(updated_at=(NOW - timedelta(minutes=quiet_for)).isoformat())
    got = line(quiet, old)
    assert (got["build"]["status"], got["build"]["word"]) == ("stuck", "Stuck")
    assert "No progress for" in got["build"]["tip"]
    # A live build's later stations are still Next, never Not reached.
    assert got["review"]["word"] == "Next" and not got["review"].get("not_reached")


def test_an_errored_attempt_in_a_live_build_says_failed_not_stuck():
    acts = [act("serve_check", None, "error", minutes_ago=6),
            act("repair", "software_engineer", "active", minutes_ago=2)]
    got = line(run(phase="repair"), acts)
    assert got["check"]["status"] == "failed"
    assert got["check"]["tip"] == "This attempt failed; the build went on"
    assert got["build"]["status"] == "working"
