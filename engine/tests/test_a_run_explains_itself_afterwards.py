"""A finished run says what it cost, what it delivered, and what the machine took from it.

Answering "what went wrong" on an 85-minute build took two hours of reading container logs, job
leases, sandbox file mtimes and gate events by hand — and every fact needed was already in the
database. Nothing assembled them.

THREE SCORES, NEVER ONE. Mixing them is how "BOSS is regressing" came to be said about a
machine that was thrashing 21 GB of swap. Delivery is what the pipeline produced; efficiency is
what it spent getting there; host is whether the machine let it run — reported separately so it
can never drag down the other two.
"""

from __future__ import annotations

from boss_engine.services.run_retrospective import (
    findings, score_delivery, score_efficiency,
)

SHIPPED_CLEAN = {"tests_passed": True, "adversarial_verdict": "pass",
                 "security_verdict": "clean", "consistency_verdict": "consistent"}

# The real run: shipped, tests green, but the primary reviewer never judged.
TODAYS_RUN = {"tests_passed": True, "adversarial_verdict": "not_run",
              "security_verdict": "clean", "consistency_verdict": "drift"}


def test_a_clean_run_scores_good():
    s = score_delivery(SHIPPED_CLEAN, pr_number=209, review_comments_posted=True)
    assert s["grade"] == "good" and s["problems"] == []


def test_a_reviewer_that_never_judged_is_a_delivery_problem():
    s = score_delivery(TODAYS_RUN, pr_number=209, review_comments_posted=True)
    assert "adversarial" in " ".join(s["problems"])


def test_a_pull_request_with_no_findings_is_a_delivery_problem():
    s = score_delivery(SHIPPED_CLEAN, pr_number=209, review_comments_posted=False)
    assert any("no review findings" in p for p in s["problems"])


def test_shipping_with_green_tests_is_fair_not_poor():
    """It works and it shipped; what failed was the governance around it."""
    assert score_delivery(TODAYS_RUN, pr_number=209,
                          review_comments_posted=False)["grade"] == "fair"


def test_not_shipping_is_poor():
    assert score_delivery(TODAYS_RUN, pr_number=None,
                          review_comments_posted=False)["grade"] == "poor"


def test_efficiency_is_measured_on_WORKING_time_not_elapsed():
    """The caller subtracts host stalls before this sees the number — otherwise the machine's
    lost minutes are charged to the pipeline, which is the whole confusion being fixed."""
    s = score_efficiency(working_minutes=32.0, cost_usd=11.76,
                         near_cap_stages=[], comparable_minutes=35.0)
    assert s["grade"] == "good"
    assert s["vs_project_median"] < 1.0


def test_a_stage_at_its_turn_cap_is_flagged():
    s = score_efficiency(working_minutes=30.0, cost_usd=6.0,
                         near_cap_stages=["implement"], comparable_minutes=30.0)
    assert any("turn cap" in p for p in s["problems"])


def test_no_history_means_no_comparison_rather_than_a_guess():
    s = score_efficiency(working_minutes=85.0, cost_usd=11.0,
                         near_cap_stages=[], comparable_minutes=None)
    assert s["vs_project_median"] is None and s["grade"] == "good"


def test_the_host_finding_says_it_is_not_the_pipelines_fault():
    host = {"state": "critical", "stalled_seconds": 3136.0,
            "detail": "the host stopped scheduling BOSS for minutes at a time"}
    out = findings(score_delivery(SHIPPED_CLEAN, pr_number=1, review_comments_posted=True),
                   score_efficiency(working_minutes=32, cost_usd=1, near_cap_stages=[]),
                   host, advisory_gates=[])
    host_finding = next(f for f in out if f["area"] == "host")
    assert host_finding["severity"] == "blocker"
    assert "not the pipeline" in host_finding["action"]


def test_a_reviewer_that_never_ran_is_a_blocker_not_a_warning():
    out = findings(score_delivery(TODAYS_RUN, pr_number=209, review_comments_posted=True),
                   score_efficiency(working_minutes=32, cost_usd=1, near_cap_stages=[]),
                   {"state": "ok"}, advisory_gates=[])
    assert any(f["severity"] == "blocker" and "verdict" in f["finding"] for f in out)


def test_advisory_gates_are_surfaced_with_what_they_actually_mean():
    out = findings(score_delivery(SHIPPED_CLEAN, pr_number=1, review_comments_posted=True),
                   score_efficiency(working_minutes=32, cost_usd=1, near_cap_stages=[]),
                   {"state": "ok"},
                   advisory_gates=["code_review_passed", "consistent"])
    gov = next(f for f in out if f["area"] == "governance")
    assert "never ran" in gov["action"]


def test_each_finding_carries_its_OWN_action():
    """One blanket remedy per area is how a retrospective becomes noise: 'raise the turn
    budget' answers a truncated stage and is a non-sequitur for a run that was simply larger."""
    out = findings(score_delivery(SHIPPED_CLEAN, pr_number=1, review_comments_posted=True),
                   score_efficiency(working_minutes=90, cost_usd=1,
                                    near_cap_stages=["implement"], comparable_minutes=30.0),
                   {"state": "ok"}, advisory_gates=[])
    efficiency = [f for f in out if f["area"] == "efficiency"]
    assert len({f["action"] for f in efficiency}) == len(efficiency)


def test_a_healthy_run_produces_no_findings_at_all():
    out = findings(score_delivery(SHIPPED_CLEAN, pr_number=1, review_comments_posted=True),
                   score_efficiency(working_minutes=30, cost_usd=1, near_cap_stages=[],
                                    comparable_minutes=32.0),
                   {"state": "ok"}, advisory_gates=[])
    assert out == []
