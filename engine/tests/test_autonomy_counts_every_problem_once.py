"""THE AUTONOMY METRIC COUNTS EVERY PROBLEM ONCE, AND SAYS WHO SOLVED IT (BOSSAPP-571 R15, §12).

The baseline is the real run wfr_f383649a8b024d97a80c (2026-10-17, its events neutralised): the
deploy step hit two BOSS-side causes, each fixed by a person's own coding agent and installed by
hand — 2 problems, BOSS solved 0. The replays of the same run with self-fix: 2 problems, BOSS
solved 2. Approving is governance, not work; the hands-off rate shows its cost."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import autonomy_metric as am

from .test_workflow_capability_runner import eng, project  # noqa: F401


def ev(kind: str, at: str, actor: str = "boss", **data):
    return {"event_type": kind, "actor": actor, "created_at": f"2026-10-17T{at}:00+00:00",
            "data": {"step_id": "deploy", **data}}


#: wfr_f383…, as it happened (07:57 → 14:25).
REAL_RUN = [
    ev("recovery.decided", "07:57", action="wait_for_boss", signature="deploy:port_held:8000"),
    ev("recovery.resumed", "08:14", why="boss_updated"),
    ev("recovery.decided", "08:26", action="wait_for_boss", signature="deploy:unwired:res-db"),
    ev("recovery.resumed", "14:24", why="boss_updated"),
    ev("workflow.step_succeeded", "14:25"),
]
#: The same run with BOSS fixing itself (Replay A then Replay B).
SELF_FIXED = [
    ev("recovery.decided", "07:57", action="wait_for_boss", signature="deploy:port_held:8000"),
    ev("self_fix.approved", "08:12", actor="usr_owner", self_fix_id="sfx_a"),
    ev("recovery.resumed", "08:14", why="self_fix_installed", self_fix_id="sfx_a"),
    ev("recovery.decided", "08:26", action="wait_for_boss", signature="deploy:unwired:res-db"),
    ev("self_fix.verified", "08:26", self_fix_id="sfx_a", outcome="new_cause"),
    ev("self_fix.approved", "08:40", actor="usr_owner", self_fix_id="sfx_b"),
    ev("recovery.resumed", "08:44", why="self_fix_installed", self_fix_id="sfx_b"),
    ev("workflow.step_succeeded", "08:45"),
]


def test_the_real_run_is_the_baseline_boss_solved_none():
    problems = am.classify(REAL_RUN)
    c = am.counts(problems)
    assert (c["problems"], c["by_boss"], c["by_outside_agent"]) == (2, 0, 2)
    assert c["rate"] == 0.0 and all(p["boss_side"] for p in problems)
    assert am.sentence(c) == "2 problems · BOSS solved 0 · an outside agent solved 2"


def test_the_replays_are_two_problems_solved_by_boss():
    problems = am.classify(SELF_FIXED)
    c = am.counts(problems)
    assert (c["problems"], c["by_boss"]) == (2, 2) and c["rate"] == 1.0
    assert [p["self_fix_id"] for p in problems] == ["sfx_a", "sfx_b"]
    assert c["hands_off"] == 0, "each needed a person's approval: not hands-off"
    assert am.sentence(c) == "2 problems · BOSS solved 2"


def test_one_cause_retried_many_times_is_one_problem():
    events = [ev("recovery.decided", f"09:0{i}", action="retry",
                 signature="deploy:health:timeout") for i in range(4)]
    events.append(ev("workflow.step_succeeded", "09:09"))
    problems = am.classify(events)
    assert len(problems) == 1 and problems[0]["resolved_by"] == am.BY_BOSS
    assert problems[0]["hands_off"] is True


@pytest.mark.parametrize("tail, who", [
    ([ev("recovery.decided", "10:00", action="ask_person", signature="build:boss:x"),
      ev("workflow.step_succeeded", "10:30")], am.BY_PERSON),
    ([ev("recovery.decided", "10:00", action="fix_app", signature="build:app:y"),
      ev("workflow.step_retried", "10:20", actor="usr_owner"),
      ev("workflow.step_succeeded", "10:30")], am.BY_PERSON),
    ([ev("recovery.decided", "10:00", action="fix_app", signature="build:app:y"),
      ev("workflow.step_skipped", "10:20", actor="usr_owner")], am.BY_PERSON),
    ([ev("recovery.decided", "10:00", action="fix_app", signature="build:app:y"),
      ev("workflow.step_retried", "10:10", actor="sys_worker"),
      ev("workflow.step_succeeded", "10:30")], am.BY_BOSS),
    ([ev("recovery.decided", "10:00", action="wait_for_boss", signature="deploy:x")],
     am.OPEN),
])
def test_who_solved_it(tail, who):
    problems = am.classify(tail)
    assert [p["resolved_by"] for p in problems] == [who]


def test_steps_are_counted_apart():
    events = [ev("recovery.decided", "11:00", action="retry", signature="deploy:a"),
              {**ev("recovery.decided", "11:01", action="retry", signature="build:b"),
               "data": {"step_id": "build", "action": "retry", "signature": "build:b"}},
              ev("workflow.step_succeeded", "11:05")]
    problems = am.classify(events, run_ended=False)
    assert {(p["step_id"], p["resolved_by"]) for p in problems} == {("deploy", am.BY_BOSS),
                                                                   ("build", am.OPEN)}
    assert am.sentence(am.counts(problems)) == "2 problems · BOSS solved 1 · 1 open"
    assert am.sentence(am.counts([])) == "No problems"


def test_the_report_reads_the_run_from_the_database(eng, project):  # noqa: F811
    run_id = f"wfr_{uuid.uuid4().hex[:20]}"
    with eng.begin() as c:
        for i, e in enumerate(SELF_FIXED):
            c.execute(text(
                "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, "
                "data, created_at) VALUES (:i, :p, :t, :a, 'workflow_run', :r, "
                "CAST(:d AS JSONB), now() - make_interval(mins => :m))"),
                {"i": f"evt_{uuid.uuid4().hex[:16]}", "p": project, "t": e["event_type"],
                 "a": e["actor"], "r": run_id, "d": __import__("json").dumps(e["data"]),
                 "m": len(SELF_FIXED) - i})
        one = am.run_report(c, project_id=project, run_id=run_id)
        week = am.report(c, project_id=project, window="7d")
    assert one["sentence"] == "2 problems · BOSS solved 2" and one["rate"] == 1.0
    assert week["totals"]["by_boss"] == 2 and week["boss_side"]["rate"] == 1.0
    assert week["runs"][0]["run_id"] == run_id and week["trend"][-1]["by_boss"] == 2
    assert len(week["trend"]) in (1, 2)
