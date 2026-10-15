"""A step can wait between attempts, give up on an attempt that runs too long, and send the run
down an error path instead of stopping it (#512 phase 2).

Three optional fields on a step that does work, each a promise the runtime keeps:

* `retry_backoff` — {initial_seconds, multiplier, max_seconds}. After attempt n fails, the step
  is not leased again before now + min(initial * multiplier^(n-1), max_seconds).
* `timeout_seconds` — an attempt leased longer than this is ended as failed with reason
  `step_timeout`. It counts as an attempt; then it backs off and retries, or fails for good.
* `on_error` — when the step fails for good, it stays failed, `workflow.error_path_taken` is
  written, and the run goes on at that step. When it SUCCEEDS, that step is skipped unless a live
  path still reaches it — the rule a condition's path not taken follows (#550).

A step with none of them behaves exactly as it always did.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_run_actions, workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _start,
    _steps,
    eng,
    project,
)

WORKER = "worker-a"


# ── helpers ────────────────────────────────────────────────────────────────────────────────

def _problems(steps, entry="a"):
    _, problems = workflows.normalise_workflow_spec(
        {"entry_step": entry, "steps": steps}, check_agents=False)
    return problems


def _publish(c, project_id, steps, entry="a"):
    key = f"err_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product",
    )
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry, "steps": steps}, actor="test",
    )


def _run(c, project_id, steps, entry="a"):
    version = _publish(c, project_id, steps, entry)
    return _start(c, project_id, version["id"], {})["id"]


def _claim(c, project_id, run_id, worker=WORKER):
    return workflows.claim_step(c, project_id=project_id, run_id=run_id, worker_id=worker,
                                lease_seconds=600)


def _row(c, run_id, step_id):
    return c.execute(text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:r "
                          "AND step_id=:s AND iteration=0"),
                     {"r": run_id, "s": step_id}).mappings().one()


def _events(c, run_id, event_type, step_id=None):
    rows = c.execute(text("SELECT data FROM events WHERE entity_id=:r AND event_type=:t "
                          "ORDER BY created_at, id"),
                     {"r": run_id, "t": event_type}).scalars().all()
    return [r for r in rows if step_id is None or r.get("step_id") == step_id]


def _run_status(c, run_id):
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                     {"r": run_id}).scalar()


def _statuses(c, run_id):
    return {k: v["status"] for k, v in _steps(c, run_id).items()}


def _due_now(c, run_id, step_id):
    """Time passes: the step's backoff is over."""
    c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                   "WHERE workflow_run_id=:r AND step_id=:s"), {"r": run_id, "s": step_id})


def _overdue(c, run_id, step_id):
    """Time passes: the step's current attempt has run past its timeout."""
    c.execute(text("UPDATE workflow_step_runs "
                   "SET attempt_timeout_at = now() - interval '1 second' "
                   "WHERE workflow_run_id=:r AND step_id=:s"), {"r": run_id, "s": step_id})


def _seconds_until_due(c, run_id, step_id):
    return c.execute(text("SELECT EXTRACT(EPOCH FROM run_after - now()) FROM workflow_step_runs "
                          "WHERE workflow_run_id=:r AND step_id=:s AND iteration=0"),
                     {"r": run_id, "s": step_id}).scalar()


def _fail(c, project_id, step, worker=WORKER, retryable=True):
    return workflows.fail_step(c, project_id=project_id, step_run_id=step["id"],
                               worker_id=worker, error="it broke", retryable=retryable)


def _finish(c, project_id, run_id, step_id):
    claimed = _claim(c, project_id, run_id)
    assert claimed is not None and claimed["step_id"] == step_id, (
        step_id, claimed and claimed["step_id"], _statuses(c, run_id))
    workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, output={step_id: True}, evidence=[])


def _claims_of(c, run_id, step_id):
    return len(_events(c, run_id, "workflow.step_leased", step_id))


# ── the validator ──────────────────────────────────────────────────────────────────────────

def _details(problems):
    return [p["detail"] for p in problems]


def test_a_spec_with_all_three_fields_is_valid_and_normalised():
    spec, problems = workflows.normalise_workflow_spec({"entry_step": "a", "steps": [
        {"id": "a", "kind": "task", "next": ["b"], "on_error": " e ", "timeout_seconds": 30,
         "retry_backoff": {"initial_seconds": 5}},
        {"id": "b", "kind": "task"},
        {"id": "e", "kind": "task"},
    ]}, check_agents=False)
    assert problems == []
    a = spec["steps"][0]
    assert a["on_error"] == "e"
    assert a["timeout_seconds"] == 30
    assert a["retry_backoff"] == {"initial_seconds": 5, "multiplier": 2, "max_seconds": 3600}


def test_a_spec_without_the_new_fields_normalises_as_before():
    spec, problems = workflows.normalise_workflow_spec({"entry_step": "a", "steps": [
        {"id": "a", "kind": "task", "next": ["b"]}, {"id": "b", "kind": "task"},
    ]}, check_agents=False)
    assert problems == []
    for step in spec["steps"]:
        assert not {"on_error", "timeout_seconds", "retry_backoff"} & set(step)


def test_empty_values_are_the_field_left_out():
    spec, problems = workflows.normalise_workflow_spec({"entry_step": "a", "steps": [
        {"id": "a", "kind": "task", "on_error": "", "timeout_seconds": None,
         "retry_backoff": None},
    ]}, check_agents=False)
    assert problems == []
    assert not {"on_error", "timeout_seconds", "retry_backoff"} & set(spec["steps"][0])


def test_an_error_path_to_a_missing_step_is_refused_by_name():
    details = _details(_problems([{"id": "a", "kind": "task", "on_error": "nowhere"}]))
    assert details == ["step 'a' on_error goes to step 'nowhere', which does not exist. "
                       "Pick a step in this workflow."]


def test_an_error_path_to_itself_is_refused():
    problems = _problems([{"id": "a", "kind": "task", "next": ["b"]},
                          {"id": "b", "kind": "task", "on_error": "b"}])
    assert [(p["step_id"], p["field"]) for p in problems] == [("b", "on_error")]
    assert "on_error goes to itself" in problems[0]["detail"]


def test_an_error_path_to_the_start_is_refused():
    problems = _problems([{"id": "a", "kind": "task", "next": ["b"]},
                          {"id": "b", "kind": "task", "on_error": "a"}])
    assert [(p["step_id"], p["field"]) for p in problems] == [("b", "on_error")]
    assert "start step 'a'" in problems[0]["detail"]


def test_an_error_path_that_loops_back_is_refused():
    problems = _problems([{"id": "a", "kind": "task", "next": ["b"]},
                          {"id": "b", "kind": "task", "next": ["c"]},
                          {"id": "c", "kind": "task", "on_error": "b"}])
    assert [(p["step_id"], p["field"]) for p in problems] == [("c", "on_error")]
    assert problems[0]["detail"] == (
        "step 'c' on_error goes to step 'b', which leads back to 'c': an error path cannot "
        "loop. Use max_attempts to retry, or bounded_loop to repeat.")


def test_a_step_reached_only_by_an_error_path_is_part_of_the_workflow():
    assert _problems([{"id": "a", "kind": "task", "next": ["b"], "on_error": "e"},
                      {"id": "b", "kind": "task"},
                      {"id": "e", "kind": "task", "next": ["b"]}]) == []


def test_steps_without_attempts_cannot_have_error_handling():
    problems = _problems([
        {"id": "a", "kind": "task", "next": ["t"]},
        {"id": "t", "kind": "timer", "wait_seconds": 1, "next": ["g"], "on_error": "a",
         "timeout_seconds": 5},
        {"id": "g", "kind": "decision", "retry_backoff": {"initial_seconds": 1}},
    ])
    assert sorted((p["step_id"], p["field"]) for p in problems) == [
        ("g", "retry_backoff"), ("t", "on_error"), ("t", "timeout_seconds")]
    assert all("has no attempts of its own" in p["detail"] for p in problems)


def test_error_paths_into_and_out_of_loop_bodies_and_joins_are_refused():
    problems = _problems([
        {"id": "a", "kind": "task", "next": ["loop"], "on_error": "body"},
        {"id": "loop", "kind": "bounded_loop", "body": "body", "max_iterations": 2,
         "expression": {"path": "context.again", "op": "eq", "value": True}, "next": ["p"]},
        {"id": "body", "kind": "task", "on_error": "x"},
        {"id": "p", "kind": "parallel", "branches": ["x", "y"], "join": "j"},
        {"id": "x", "kind": "task", "next": ["j"], "on_error": "j"},
        {"id": "y", "kind": "task", "next": ["j"]},
        {"id": "j", "kind": "join", "wait_for": ["x", "y"]},
    ])
    assert sorted(p["step_id"] for p in problems) == ["a", "body", "x"]
    assert all(p["field"] == "on_error" for p in problems)


def test_bad_backoff_and_timeout_values_are_each_said():
    details = _details(_problems([{
        "id": "a", "kind": "task", "timeout_seconds": 0,
        "retry_backoff": {"initial_seconds": 4000, "multiplier": 0.5, "max_seconds": -1,
                          "jitter": True}}]))
    assert details == [
        "step 'a' retry_backoff initial_seconds must be a whole number of seconds between 0 "
        "and 3600.",
        "step 'a' retry_backoff multiplier must be a number between 1 and 10.",
        "step 'a' retry_backoff max_seconds must be a whole number of seconds between 0 and "
        "86400.",
        "step 'a' retry_backoff has unknown field(s): jitter.",
        "step 'a' timeout_seconds must be a whole number of seconds between 1 and 86400.",
    ]
    assert "max_attempts" not in " ".join(details)
    assert _details(_problems([{"id": "a", "kind": "task", "timeout_seconds": 86401}]))
    assert _details(_problems([{"id": "a", "kind": "task", "retry_backoff": 5}]))


def test_the_backoff_schedule_is_capped():
    step = {"retry_backoff": {"initial_seconds": 10, "multiplier": 3, "max_seconds": 100}}
    assert [workflows.retry_backoff_seconds(step, n) for n in range(1, 6)] == [
        10, 30, 90, 100, 100]
    assert workflows.retry_backoff_seconds({}, 3) == 0
    assert workflows.retry_backoff_seconds(
        {"retry_backoff": {"initial_seconds": 1, "multiplier": 10, "max_seconds": 86400}},
        25) == 86400


def test_an_error_step_cannot_bind_the_failed_steps_output():
    spec, _ = workflows.normalise_workflow_spec({"entry_step": "a", "steps": [
        {"id": "a", "kind": "task", "next": ["b"]},
        {"id": "b", "kind": "task", "next": ["c"], "on_error": "e"},
        {"id": "c", "kind": "task"},
        {"id": "e", "kind": "task"},
    ]}, check_agents=False)
    before = workflows._runs_before(spec)
    assert before["e"] == {"a"}, "b failed: what came before it is certain, its output is not"
    assert before["c"] == {"a", "b"}


# ── backoff ────────────────────────────────────────────────────────────────────────────────

def test_a_failed_attempt_is_not_leased_again_before_its_backoff(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "max_attempts": 3,
                                    "retry_backoff": {"initial_seconds": 60, "multiplier": 2,
                                                      "max_seconds": 3600}}])
        first = _claim(c, project, run_id)
        _fail(c, project, first)
        assert _row(c, run_id, "a")["status"] == "ready"
        assert 55 <= _seconds_until_due(c, run_id, "a") <= 65
        assert _claim(c, project, run_id) is None, "leased before its backoff was over"

        _due_now(c, run_id, "a")
        second = _claim(c, project, run_id)
        assert second is not None and second["attempt"] == 2
        _fail(c, project, second)
        assert 115 <= _seconds_until_due(c, run_id, "a") <= 125, "the backoff doubles"

        scheduled = _events(c, run_id, "workflow.step_retry_scheduled", "a")
        assert [(e["attempt"], e["backoff_seconds"]) for e in scheduled] == [(1, 60), (2, 120)]


def test_the_backoff_never_waits_longer_than_its_cap(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "max_attempts": 4,
                                    "retry_backoff": {"initial_seconds": 100, "multiplier": 10,
                                                      "max_seconds": 150}}])
        for _ in range(3):
            _due_now(c, run_id, "a")
            _fail(c, project, _claim(c, project, run_id))
        scheduled = _events(c, run_id, "workflow.step_retry_scheduled", "a")
        assert [e["backoff_seconds"] for e in scheduled] == [100, 150, 150]
        assert 145 <= _seconds_until_due(c, run_id, "a") <= 155


def test_a_callers_longer_delay_still_wins_over_the_backoff(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task",
                                    "retry_backoff": {"initial_seconds": 5}}])
        step = _claim(c, project, run_id)
        workflows.fail_step(c, project_id=project, step_run_id=step["id"], worker_id=WORKER,
                            error="later", retry_delay_seconds=300)
        assert 295 <= _seconds_until_due(c, run_id, "a") <= 305


def test_a_step_without_backoff_retries_at_once_as_before(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "max_attempts": 2}])
        first = _claim(c, project, run_id)
        assert first["attempt_timeout_at"] is None
        _fail(c, project, first)
        again = _claim(c, project, run_id)
        assert again is not None and again["attempt"] == 2, "no backoff: due at once"
        _fail(c, project, again)
        assert _row(c, run_id, "a")["status"] == "deadlettered"
        assert _run_status(c, run_id) == "deadlettered", "stopped at the step, as before"
        assert c.execute(text("SELECT count(*) FROM deadletters WHERE source_id=:s"),
                         {"s": first["id"]}).scalar() == 1


# ── timeouts ───────────────────────────────────────────────────────────────────────────────

def test_the_attempts_clock_starts_when_it_is_leased(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "timeout_seconds": 90}])
        claimed = _claim(c, project, run_id)
        left = c.execute(text("SELECT EXTRACT(EPOCH FROM attempt_timeout_at - now()) "
                              "FROM workflow_step_runs WHERE id=:i"),
                         {"i": claimed["id"]}).scalar()
        assert 85 <= left <= 95
        leased = _events(c, run_id, "workflow.step_leased", "a")[-1]
        assert leased["timeout_seconds"] == 90


def test_an_attempt_that_runs_too_long_is_ended_and_retried(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "max_attempts": 2,
                                    "timeout_seconds": 30}])
        first = _claim(c, project, run_id, worker="slow")
        _overdue(c, run_id, "a")
        with pytest.raises(workflows.WorkflowLeaseError):
            workflows.heartbeat_step(c, project_id=project, step_run_id=first["id"],
                                     worker_id="slow", lease_seconds=600)

        second = _claim(c, project, run_id, worker="fresh")
        assert second is not None and second["attempt"] == 2, "the timeout counts as an attempt"
        timed_out = _events(c, run_id, "workflow.step_timed_out", "a")
        assert len(timed_out) == 1
        assert timed_out[0]["reason"] == "step_timeout"
        assert timed_out[0]["timeout_seconds"] == 30 and timed_out[0]["attempt"] == 1
        assert timed_out[0]["worker_id"] == "slow"
        retry = _events(c, run_id, "workflow.step_retry_scheduled", "a")
        assert retry and retry[0]["reason"] == "step_timeout"

        # The slow worker finishing late is refused; the fresh one's work counts.
        with pytest.raises(workflows.WorkflowLeaseError):
            workflows.complete_step(c, project_id=project, step_run_id=first["id"],
                                    worker_id="slow", output={}, evidence=[])
        workflows.complete_step(c, project_id=project, step_run_id=second["id"],
                                worker_id="fresh", output={"ok": True}, evidence=[])
        assert _run_status(c, run_id) == "completed"


def test_a_timeout_on_the_last_attempt_fails_the_step_for_good(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "max_attempts": 1,
                                    "timeout_seconds": 30, "next": ["b"]},
                                   {"id": "b", "kind": "task"}])
        claimed = _claim(c, project, run_id)
        _overdue(c, run_id, "a")
        assert _claim(c, project, run_id) is None
        row = _row(c, run_id, "a")
        assert row["status"] == "deadlettered"
        assert row["output"]["failure_reason"] == "step_timeout"
        assert row["output"]["reason"] == "step_timeout"
        assert "Timed out" in row["last_error"]
        assert _run_status(c, run_id) == "deadlettered", "stopped at the step, as before"
        dead = _events(c, run_id, "workflow.step_deadlettered", "a")
        assert dead and dead[0]["reason"] == "step_timeout"
        assert c.execute(text("SELECT count(*) FROM deadletters WHERE source_id=:s"),
                         {"s": claimed["id"]}).scalar() == 1
        assert _row(c, run_id, "b")["status"] == "pending"


def test_completing_after_the_timeout_counts_as_the_timeout(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, [{"id": "a", "kind": "task", "max_attempts": 2,
                                    "timeout_seconds": 30}])
        claimed = _claim(c, project, run_id)
        _overdue(c, run_id, "a")
        result = workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                         worker_id=WORKER, output={"late": True}, evidence=[])
        assert result["status"] == "ready", "too late: the attempt timed out instead"
        assert result["output"].get("failure_reason") == "step_timeout"
        assert _events(c, run_id, "workflow.step_timed_out", "a")
        assert not _events(c, run_id, "workflow.step_succeeded", "a")


# ── error paths ────────────────────────────────────────────────────────────────────────────

def _with_error_path(max_attempts=2, **a):
    """a → b; a fails into e; e → f."""
    return [
        {"id": "a", "kind": "task", "next": ["b"], "on_error": "e",
         "max_attempts": max_attempts, **a},
        {"id": "b", "kind": "task"},
        {"id": "e", "kind": "task", "next": ["f"]},
        {"id": "f", "kind": "task"},
    ]


def test_the_run_goes_on_at_the_error_step_when_attempts_run_out(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _with_error_path(max_attempts=2))
        first = _claim(c, project, run_id)
        _fail(c, project, first)
        _fail(c, project, _claim(c, project, run_id))

        a = _row(c, run_id, "a")
        assert a["status"] == "failed", "the failure stays on the step"
        assert a["last_error"] == "it broke"
        assert a["output"]["error_path_taken"] == "e"
        assert _run_status(c, run_id) == "running", "the run did not stop"
        taken = _events(c, run_id, "workflow.error_path_taken")
        assert len(taken) == 1
        assert {k: taken[0][k] for k in ("step_id", "error_step_id", "reason", "attempts")} == {
            "step_id": "a", "error_step_id": "e", "reason": "attempts_exhausted", "attempts": 2}
        assert _events(c, run_id, "workflow.step_failed", "a")
        assert not _events(c, run_id, "workflow.step_deadlettered", "a")
        assert c.execute(text("SELECT count(*) FROM deadletters WHERE source_id=:s"),
                         {"s": first["id"]}).scalar() == 0, "handled, so nobody is paged"

        _finish(c, project, run_id, "e")
        _finish(c, project, run_id, "f")
        statuses = _statuses(c, run_id)
        assert _run_status(c, run_id) == "completed"
        assert statuses["b"] == "skipped"
        assert _row(c, run_id, "b")["output"]["reason"] == "path_not_reached"


def test_a_failure_no_retry_can_fix_takes_the_error_path_at_once(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _with_error_path(max_attempts=3))
        _fail(c, project, _claim(c, project, run_id), retryable=False)
        taken = _events(c, run_id, "workflow.error_path_taken")
        assert [(t["reason"], t["attempts"]) for t in taken] == [("not_retryable", 1)]
        assert _row(c, run_id, "e")["status"] == "ready"


def test_a_timeout_on_the_last_attempt_takes_the_error_path(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _with_error_path(max_attempts=1, timeout_seconds=10))
        _claim(c, project, run_id)
        _overdue(c, run_id, "a")
        claimed = _claim(c, project, run_id)
        assert claimed is not None and claimed["step_id"] == "e"
        taken = _events(c, run_id, "workflow.error_path_taken")
        assert [(t["reason"], t["attempts"]) for t in taken] == [("step_timeout", 1)]
        assert _row(c, run_id, "a")["output"]["failure_reason"] == "step_timeout"


def test_a_step_that_failed_into_its_error_path_is_nobodys_decision(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _with_error_path(max_attempts=1))
        _fail(c, project, _claim(c, project, run_id))
        run = workflows.get_run(c, project, run_id)
        assert workflow_run_actions.stopped_for_operator(run, run["steps"]) == []


def test_the_error_path_is_not_taken_when_the_step_succeeds(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _with_error_path())
        _finish(c, project, run_id, "a")
        e = _row(c, run_id, "e")
        assert e["status"] == "skipped"
        assert e["output"]["reason"] == "error_path_not_taken"
        _finish(c, project, run_id, "b")
        assert _run_status(c, run_id) == "completed"
        assert _statuses(c, run_id)["f"] == "skipped"
        assert not _events(c, run_id, "workflow.error_path_taken")


def test_an_error_step_the_success_path_also_reaches_runs_once(eng, project):
    steps = [
        {"id": "a", "kind": "task", "next": ["b"], "on_error": "c", "max_attempts": 1},
        {"id": "b", "kind": "task", "next": ["c"]},
        {"id": "c", "kind": "task"},
    ]
    with eng.begin() as c:
        run_id = _run(c, project, steps)
        _finish(c, project, run_id, "a")
        assert _row(c, run_id, "c")["status"] == "pending", "b still leads to it"
        kept = _events(c, run_id, "workflow.step_not_skipped", "c")
        assert kept and kept[0]["error_path_of"] == "a"
        _finish(c, project, run_id, "b")
        _finish(c, project, run_id, "c")
        assert _run_status(c, run_id) == "completed"
        assert _claims_of(c, run_id, "c") == 1

    with eng.begin() as c:
        run_id = _run(c, project, steps)
        _fail(c, project, _claim(c, project, run_id))
        _finish(c, project, run_id, "c")
        assert _run_status(c, run_id) == "completed"
        assert _claims_of(c, run_id, "c") == 1
        assert _statuses(c, run_id)["b"] == "skipped"


def test_an_error_step_another_live_step_may_fail_into_is_kept(eng, project):
    """Two parallel branches share an error step: one succeeding does not skip it while the
    other is still running and may yet fail into it."""
    steps = [
        {"id": "a", "kind": "parallel", "branches": ["x", "y"], "join": "j"},
        {"id": "x", "kind": "task", "next": ["j"], "on_error": "e", "max_attempts": 1},
        {"id": "y", "kind": "task", "next": ["j"], "on_error": "e", "max_attempts": 1},
        {"id": "j", "kind": "join", "wait_for": ["x", "y"], "success_policy": "any"},
        {"id": "e", "kind": "task"},
    ]
    with eng.begin() as c:
        run_id = _run(c, project, steps)
        x = _claim(c, project, run_id)
        y = _claim(c, project, run_id)
        assert {x["step_id"], y["step_id"]} == {"x", "y"}
        workflows.complete_step(c, project_id=project, step_run_id=x["id"], worker_id=WORKER,
                                output={"x": True}, evidence=[])
        assert _row(c, run_id, "e")["status"] == "pending"
        _fail(c, project, y)
        assert _row(c, run_id, "e")["status"] == "ready"
        _finish(c, project, run_id, "e")
        assert _run_status(c, run_id) == "completed"


def test_an_error_path_taken_while_paused_starts_on_resume(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _with_error_path(max_attempts=1))
        claimed = _claim(c, project, run_id)
        workflows.pause_run(c, project, run_id, "test")
        _fail(c, project, claimed)
        assert _row(c, run_id, "e")["status"] == "pending"
        assert _run_status(c, run_id) == "paused"
        workflows.resume_run(c, project, run_id, "test")
        assert _row(c, run_id, "e")["status"] == "ready"
        _finish(c, project, run_id, "e")
        _finish(c, project, run_id, "f")
        assert _run_status(c, run_id) == "completed"
