"""Loop and duplicate guards stop runaway ambient work, in code (BOSSAPP-908; design §6, §9).

Every single call of the ~$47k retry loop was inside its limit; the run as a whole was not. So:
an item whose cause is too deep is refused; a dismissed or failed problem cools down, longer
each time; each class opens at most N items a day; at most N run at once; and when too many
ambient items fail within the hour the project's ambient work STOPS until a person resumes it.
Every refusal is evented so the scorecard counts it.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services.ambient import board, guards, policy

from ._ambient_support import eng, open_item, project  # noqa: F401


def _failed(c, pid: str, fp: str) -> str:
    item = open_item(c, pid, fingerprint=fp)["item"]
    token = board.claim(c, item["id"], owner="agent:software_engineer")["lease_token"]
    board.start(c, item["id"], owner="agent:software_engineer", token=token)
    board.finish(c, item["id"], actor="agent:software_engineer", token=token, lifecycle="failed",
                 outcome="build failed")
    return item["id"]


def test_an_item_caused_too_deep_by_ambient_work_is_refused(eng, project):
    with eng.begin() as c:
        refused = open_item(c, project, fingerprint="fp:deep", causal_depth=3)
        allowed = open_item(c, project, fingerprint="fp:shallow", causal_depth=2)
    assert refused["outcome"] == "refused" and refused["guard"] == "causal_depth"
    assert allowed["outcome"] == "opened"


def test_a_failed_or_dismissed_problem_cools_down_and_longer_each_time(eng, project):
    with eng.begin() as c:
        first = open_item(c, project, fingerprint="fp:cool")["item"]
        board.dismiss(c, first["id"], actor="ceo", reason="not now")
        once = guards.cooldown_until(c, project, "fp:cool", 24)
        refused = open_item(c, project, fingerprint="fp:cool")
        c.execute(text("UPDATE work_items SET resolved_at = now() - interval '25 hours' "
                       "WHERE id = :id"), {"id": first["id"]})
        reopened = open_item(c, project, fingerprint="fp:cool")["item"]
        board.dismiss(c, reopened["id"], actor="ceo", reason="still not now")
        twice = guards.cooldown_until(c, project, "fp:cool", 24)
        now = c.execute(text("SELECT now()")).scalar()
    assert refused["outcome"] == "refused" and refused["guard"] == "cooldown"
    assert 23.9 < (once - now).total_seconds() / 3600 <= 24
    assert 47.9 < (twice - now).total_seconds() / 3600 <= 48, "the second cooldown doubles"


def test_each_class_opens_at_most_its_daily_cap(eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="ceo", daily_attempt_cap=2)
        outcomes = [open_item(c, project, fingerprint=f"fp:cap-{i}")["outcome"]
                    for i in range(3)]
        other_class = open_item(c, project, fingerprint="fp:cap-docs", task_class="doc_drift")
    assert outcomes == ["opened", "opened", "refused"]
    assert other_class["outcome"] == "opened", "the cap is per class"


def test_at_most_max_concurrent_items_run_at_once(eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="ceo", max_concurrent=1)
        # Backlog work waits for a slot; an incident never does (guards.URGENT_CLASSES).
        a = open_item(c, project, fingerprint="fp:a", task_class="security_patch")["item"]
        b = open_item(c, project, fingerprint="fp:b", task_class="security_patch")["item"]
        ta = board.claim(c, a["id"], owner="agent:sre")["lease_token"]
        tb = board.claim(c, b["id"], owner="agent:qa")["lease_token"]
        board.start(c, a["id"], owner="agent:sre", token=ta)
        try:
            board.start(c, b["id"], owner="agent:qa", token=tb)
            blocked = None
        except board.Conflict as exc:
            blocked = str(exc)
    assert blocked and "1 ambient items already in progress (limit 1)" in blocked


def test_too_many_failures_in_an_hour_trip_the_breaker_and_stop_the_project(eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="ceo", breaker_min_runs=3,
                               breaker_failure_ratio=0.6)
        healthy = open_item(c, project, fingerprint="fp:ok")["item"]
        board.finish(c, healthy["id"], actor="boss", lifecycle="resolved")
        _failed(c, project, "fp:f1")
        assert not policy.is_stopped(c, project)
        _failed(c, project, "fp:f2")
        state = policy.stop_state(c, project)
        tripped = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                                 "event_type = 'ambient.breaker_tripped'"),
                            {"p": project}).scalars().all()
        refused = open_item(c, project, fingerprint="fp:next")
    assert state["stopped"] and "circuit breaker: 2 of 3" in state["reason"]
    assert tripped and tripped[0]["failed"] == 2 and tripped[0]["ended"] == 3
    assert refused["outcome"] == "refused" and refused["guard"] == "stopped"


def test_a_person_resuming_after_a_trip_starts_a_fresh_window(eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="ceo", breaker_min_runs=2)
        _failed(c, project, "fp:f1")
        _failed(c, project, "fp:f2")
        assert policy.is_stopped(c, project)
        policy.resume(c, project_id=project, actor="ceo", reason="fixed the host")
        breaker = guards.check_breaker(c, project)
        opened = open_item(c, project, fingerprint="fp:after")
    assert not breaker["tripped"] and breaker["ended"] == 0
    assert opened["outcome"] == "opened"


def test_every_refusal_is_evented_for_the_scorecard(eng, project):
    with eng.begin() as c:
        open_item(c, project, fingerprint="fp:deep", causal_depth=9)
        guards_seen = c.execute(text(
            "SELECT data->>'guard' FROM events WHERE project_id = :p AND "
            "event_type = 'ambient.guard_refused'"), {"p": project}).scalars().all()
    assert guards_seen == ["causal_depth"]
