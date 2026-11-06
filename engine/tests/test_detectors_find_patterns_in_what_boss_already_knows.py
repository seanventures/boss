"""Detectors find patterns in what BOSS already knows, and say what they rest on (BOSSAPP-911;
design §14.1-§14.2).

* Each detector reads the store BOSS's own writers fill (in their shapes) and finds its
  pattern only when the numbers cross its bar: a hot file, a repeated incident cause, a changed
  file with no test, a review finding across builds, design drift, a cost jump, a flaky test, a
  slow stage, repeated person interventions.
* Below the bar it is silent; a threshold scale (what "too small" rejects taught it) raises the
  bar; a broken detector is reported, never raised, and the others still run.
* Findings about one file from several detectors group into ONE recommendation.
"""
from __future__ import annotations

from datetime import timedelta

from boss_engine.services.ambient import learning
from boss_engine.services.ambient import roadmap_demo as demo
from boss_engine.services.ambient._common import now

from ._learning_support import eng, project  # noqa: F401

DAY = timedelta(days=1)


def _found(c, pid, key, scale=1.0):
    out = learning.detect(c, pid, only=[key], tuning={key: scale})[key]
    assert out["error"] is None, out["error"]
    return out["findings"]


def _hot_checkout(c, pid, prs=8, incidents=2):
    f = demo.put_file(c, pid, "shop/checkout.py", lines=1200, symbols=("pay", "reserve"))
    demo.put_callers(c, pid, f["symbols"]["pay"], 6)
    for n in range(prs):
        demo.put_pr(c, pid, 100 + n, [f["path"]], at=now() - (20 - n) * DAY)
    incs = [demo.put_incident(c, pid, title="Double charge", suspect_file=f["path"],
                              cause_key="checkout.double_charge", at=now() - d * DAY)
            for d in range(1, incidents + 1)]
    return f, incs


def test_a_file_many_prs_change_that_incidents_point_at_is_a_hotspot(eng, project):
    with eng.begin() as c:
        f, incs = _hot_checkout(c, project)
        demo.put_file(c, project, "shop/quiet.py", lines=2000)
        demo.put_pr(c, project, 900, ["shop/quiet.py"])
        found = _found(c, project, "hotspot")
    assert [x.subject for x in found] == ["file:shop/checkout.py"]
    hot = found[0]
    assert hot.facts == {"prs": 8, "lines": 1200, "incidents": 2, "fan_in": 6}
    assert hot.blast_radius == 6 and hot.severity == "high"
    kinds = {e["kind"] for e in hot.evidence}
    assert kinds == {"kg_node", "incident"} and {e["ref"] for e in hot.evidence} >= set(incs)


def test_the_same_root_cause_twice_is_a_hardening_recommendation(eng, project):
    with eng.begin() as c:
        for d in (3, 9):
            demo.put_incident(c, project, title="Webhook timeout", cause_key="payment.timeout",
                              at=now() - d * DAY)
        demo.put_incident(c, project, title="One-off DNS blip", cause_key="dns.blip")
        found = _found(c, project, "incident_cause")
        stricter = _found(c, project, "incident_cause", scale=2.0)
    assert len(found) == 1 and found[0].facts["incidents"] == 2
    assert found[0].title.startswith("Stop repeat incidents: Webhook timeout")
    assert stricter == []


def test_a_changed_file_with_no_test_named_for_it_is_a_test_gap(eng, project):
    with eng.begin() as c:
        for base, path in ((100, "shop/cart.py"), (200, "shop/order.py")):
            demo.put_file(c, project, path)
            for n in range(4):
                demo.put_pr(c, project, base + n, [path])
        demo.put_file(c, project, "tests/test_order.py")
        found = _found(c, project, "missing_tests")
    assert [x.subject for x in found] == ["file:shop/cart.py"]
    assert found[0].facts["prs"] == 4 and found[0].evidence[0]["kind"] == "kg_node"


def test_the_same_review_finding_across_builds_becomes_a_rule(eng, project):
    with eng.begin() as c:
        runs = []
        for n in range(3):
            comment = demo.review_comment(f"Payment retry in gateway_{n}.py:4{n} has no "
                                          "idempotency key")
            runs.append(demo.put_build(c, project, review_comments=[comment, comment]))
        demo.put_build(c, project, review_comments=[demo.review_comment("Rename this var")])
        found = _found(c, project, "review_repeat")
    assert len(found) == 1
    assert found[0].facts["builds"] == 3          # a comment repeated in one run counts once
    assert {e["ref"] for e in found[0].evidence} == set(runs)
    assert "idempotency" in found[0].title


def test_builds_that_drift_from_the_approved_prd_ask_for_a_design_call(eng, project):
    with eng.begin() as c:
        for _ in range(2):
            demo.put_build(c, project, consistency="drift", decision_id="dec_prd_1",
                           consistency_summary="Cancel no longer releases stock.")
        demo.put_build(c, project, consistency="consistent", decision_id="dec_prd_2")
        found = _found(c, project, "design_drift")
    assert len(found) == 1 and found[0].facts["builds"] == 2
    assert learning.DETECTORS["design_drift"].shape == "prd"
    assert found[0].evidence[0] == {"kind": "decision", "ref": "dec_prd_1",
                                    "note": "the PRD decision"}


def test_a_workflow_whose_cost_jumped_this_week_is_a_cost_recommendation(eng, project):
    with eng.begin() as c:
        for n in range(4):
            demo.put_scorecard(c, project, f"old_{project}_{n}", cost_usd=10 + n,
                               at=now() - (20 + n) * DAY)
            demo.put_scorecard(c, project, f"new_{project}_{n}", cost_usd=30 + n,
                               at=now() - DAY)
            demo.put_scorecard(c, project, f"flat_{project}_{n}", cost_usd=10,
                               workflow_key="docs", at=now() - (1 if n % 2 else 20) * DAY)
        found = _found(c, project, "cost_spike")
    assert [x.key for x in found] == [("feature_build",)]
    assert found[0].facts["ratio"] >= 2.5 and found[0].facts["runs"] == 4


def test_a_test_failing_outside_the_change_in_three_builds_is_flaky(eng, project):
    with eng.begin() as c:
        flake = {"environment": [{"test_id": "tests/test_hooks.py::test_retry"}]}
        for _ in range(3):
            demo.put_build(c, project, baseline=flake)
        demo.put_build(c, project,
                       baseline={"pre_existing": [{"test_id": "tests/test_x.py::test_once"}]})
        found = _found(c, project, "flaky_test")
    assert [x.key for x in found] == [("tests/test_hooks.py::test_retry",)]
    assert found[0].subject == "file:tests/test_hooks.py"


def test_a_stage_whose_median_is_far_over_its_target_is_slow(eng, project):
    with eng.begin() as c:
        for n in range(5):
            run = demo.put_build(c, project)
            demo.put_profile(c, project, run, {"design": 9.5, "implement": 15.0})
        found = _found(c, project, "slow_stage")
    assert [x.key for x in found] == [("design",)]
    assert found[0].facts["median_min"] == 9.5 and found[0].facts["target_min"] == 5.0


def test_people_steering_the_same_stage_again_and_again_is_friction(eng, project):
    with eng.begin() as c:
        runs = [demo.put_build(c, project) for _ in range(3)]
        for n in range(6):
            demo.put_event(c, project, "code_task.steered", actor="ceo", entity_type="code_task",
                           entity_id=runs[n % 3], data={"stage": "write_tests"})
        for n in range(6):   # BOSS's own steers are not friction
            demo.put_event(c, project, "code_task.steered", actor="agent:captain",
                           entity_type="code_task", entity_id=runs[n % 3],
                           data={"stage": "implement"})
        found = _found(c, project, "user_friction")
    assert [x.key for x in found] == [("steer", "write_tests")]
    assert found[0].facts["count"] == 6 and found[0].facts["runs"] == 3


def test_a_broken_detector_is_reported_and_the_others_still_run(eng, project):
    def broken(*_a, **_k):
        raise RuntimeError("store unavailable")

    learning.detector("broken_probe", title="Broken", kind="process", shape="ticket",
                      source="kg", evidence_source="code", squad=())(broken)
    try:
        with eng.begin() as c:
            _hot_checkout(c, project)
            out = learning.detect(c, project, only=["broken_probe", "hotspot"])
    finally:
        learning.DETECTORS.pop("broken_probe", None)
    assert "store unavailable" in out["broken_probe"]["error"]
    assert out["hotspot"]["error"] is None and len(out["hotspot"]["findings"]) == 1


def test_findings_about_one_file_group_into_one_recommendation(eng, project):
    with eng.begin() as c:
        _hot_checkout(c, project)
        found = learning.detect(c, project, only=["hotspot", "incident_cause",
                                                  "missing_tests"])
    groups = learning.group(found, project_id=project)
    assert len(groups) == 1
    g = groups[0]
    assert {m.detector for m in g.members} == {"hotspot", "incident_cause", "missing_tests"}
    assert g.source_kinds == ["code", "incident"]
    assert len(g.evidence) == len({(e["kind"], e["ref"]) for e in g.evidence})
