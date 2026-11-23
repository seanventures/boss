"""The engines gate (scripts/e2e-gate.sh engines) reads what the pipeline RECORDS, without Docker.

The gate went FAIL 6/15 before rc10.33 on three expectations BOSS had changed on purpose, not on
a broken pipeline:

  * the review seats sit ONE merged session (review_panel.py, BOSSAPP-1000, since rc10.14), so
    the engine never sees a `review_code`, `security_check` or `consistency_check` session;
  * the mutation probe's plant is code, not a model (BOSSAPP-1004), so `check_tests` is no
    engine session at all;
  * at the standard bar (the light lane, 2026-11-18) a missing must-have is a follow-up, not a
    fix round; it blocks, and gets its fix round, at production.

These checks keep the gate's readers tied to the pipeline's own names and rules, so the next
deliberate change fails here, in seconds, instead of in a 25-minute gate run before a release.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "tests" / "e2e" / "gate"
GOVERNED = ROOT / "engine" / "boss_engine" / "services" / "governed_code_task_service.py"


def _engines():
    sys.path.insert(0, str(GATE))
    try:
        import engines
    finally:
        sys.path.remove(str(GATE))
    return engines


def _build(script, events=(), state=None):
    return {"script": [{"stage": s, "read_only": ro} for s, ro in script],
            "events": [{"event_type": t, "data": d} for t, d in events],
            "run": {"state": state or {}}}


def test_a_merged_review_session_counts_the_seats_it_answered():
    eng = _engines()
    b = _build([("write_tests", False), ("implement", False), ("review_panel", True),
                ("acceptance_check", True)],
               [("code_task.review_panel_merged",
                 {"seats": ["review_code", "security_check", "consistency_check"],
                  "fallback": [], "hollow": False})],
               {"mutation_caught": False})
    assert eng.stages_run(b) == ["write_tests", "implement", "review_panel", "review_code",
                                 "security_check", "consistency_check", "acceptance_check",
                                 "check_tests"]


def test_an_unanswered_seat_or_a_hollow_session_is_not_counted():
    eng = _engines()
    # The session left security unanswered and it never ran alone: security did not run.
    b = _build([("review_panel", True)],
               [("code_task.review_panel_merged",
                 {"seats": ["review_code", "security_check"], "fallback": ["security_check"],
                  "hollow": False})])
    assert "security_check" not in eng.stages_run(b)
    assert "review_code" in eng.stages_run(b)
    # Its own fallback session is logged by name, and then it counts.
    b["script"].append({"stage": "security_check", "read_only": True})
    assert "security_check" in eng.stages_run(b)
    # A session that never saw the change reviewed nothing.
    hollow = _build([("review_panel", True)],
                    [("code_task.review_panel_merged",
                      {"seats": ["review_code"], "fallback": [], "hollow": True})])
    assert eng.stages_run(hollow) == ["review_panel"]
    # No merged session ran: the event alone does not invent seats.
    assert eng.stages_run(_build([("implement", False)],
                                 [("code_task.review_panel_merged",
                                   {"seats": ["review_code"], "fallback": []})])) == ["implement"]


def test_the_mutation_probe_counts_only_when_it_judged():
    eng = _engines()
    assert "check_tests" in eng.stages_run(_build([], state={"mutation_caught": True}))
    assert "check_tests" in eng.stages_run(_build([], state={"mutation_caught": False}))
    assert "check_tests" not in eng.stages_run(_build([], state={"mutation_caught": None}))
    assert "check_tests" not in eng.stages_run(_build([]))


def test_the_names_the_gate_reads_are_the_pipelines():
    from boss_engine.services import review_panel

    assert review_panel.STAGE == "review_panel"
    assert set(review_panel.SEATS) == {"review_code", "security_check", "consistency_check"}
    src = GOVERNED.read_text()
    assert '"code_task.review_panel_merged"' in src
    for key in ('"seats"', '"fallback"', '"hollow"'):
        assert key in src
    assert '"mutation_caught": mutation_caught' in src
    assert '"code_task.deferred_findings_task"' in src


def test_each_acceptance_scenario_runs_at_the_bar_its_expectation_needs():
    """The fix-round scenario runs where a missing P1 must-have BLOCKS; the light-lane one where
    it is a follow-up. Read from quality_bar itself, so a change to the rule moves the gate."""
    from boss_engine.services import quality_bar

    eng = _engines()
    record = {"verdict": "not_met",
              "items": [{"n": 1, "text": "slug lowercases", "status": "missing"},
                        {"n": 2, "text": "GET answers JSON", "status": "met"}]}
    fix = eng.SCENARIOS["acceptance_missing"]
    light = eng.SCENARIOS["acceptance_missing_light"]
    assert fix["fix_round"] and not quality_bar.acceptance_verdict(record, fix["bar"])[0]
    ok, soft = quality_bar.acceptance_verdict(record, light["bar"])
    assert ok and not light["fix_round"]
    assert any(light["followup"] in line for line in soft)


def test_the_light_lane_expectation_fails_without_its_follow_up():
    eng = _engines()
    obs = {"status": "completed", "pr": True, "fix_round": False, "serves": False,
           "stages": ["write_tests", "implement", "acceptance_check"], "failure": "",
           "tool_names": [], "followups": []}
    assert any("no follow-up names" in m for m in eng.meets("acceptance_missing_light", obs))
    obs["followups"] = ['[{"text": "Must-have 1 (P1) missing: slug lowercases"}]']
    assert eng.meets("acceptance_missing_light", obs) == []
    obs["fix_round"] = True
    assert any("a fix round ran" in m for m in eng.meets("acceptance_missing_light", obs))
