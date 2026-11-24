"""A person's step Retry after a failed build starts a new build of the same work.

northgate-gateway, 2026-11-16 (wfr_b4dbf0868f224963b793): both attempts of the build lineage
died on a BOSS bug (IdleInTransactionSessionTimeout, fixed since), so each ended `failed` with
failure_class `infra` on the local substrate. A person pressed the step's Retry. The step
resumed the lineage (code_task_service.retry_code_task) with no note, and the circuit breaker
for blind infra retries refused it: "circuit breaker: 2 attempts in this lineage have failed
with infrastructure errors (last: '(psycopg.errors.IdleInTransactionSessionTimeout) …') …".

That refusal quoted the old database error, so model_failures.is_storage_failure read the
whole sentence as BOSS's database failing: the step said "BOSS couldn't save this to its
database", retried itself three times in a second and was dead-lettered. No build was started.

A person's Retry IS the "I know, proceed" the breaker waits for; a refusal that quotes a
database error is not a database error; and a refusal that cannot change is said once.
"""
from __future__ import annotations

import json
import types
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.services import jobs as jobs_service
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _publish, _start_consented, staffed)

SPEC = {"entry_step": "build", "steps": [
    {"id": "build", "kind": "capability", "next": [],
     "binding": {"capability": "build", "inputs": {"title": "Gateway", "instructions": "build it",
                                                   "repository": "acme/web"}}}]}

#: What the first attempt's IdleInTransactionSessionTimeout left as the build's error.
OLD_DB_ERROR = ("(psycopg.errors.IdleInTransactionSessionTimeout) terminating connection due "
                "to idle-in-transaction timeout\n[SQL: \n INSERT INTO events (id, project_id) ")


class _Jobs:
    settings = None

    def create_job(self, db, *, project_id, task_id, kind, priority, payload):
        return jobs_service.enqueue_job(db, project_id, kind, payload, task_id=task_id,
                                        priority=priority)


@pytest.fixture(autouse=True)
def _ports(monkeypatch):
    from boss_engine.services import code_task_service as cts

    monkeypatch.setattr(cts, "_engine_ports", lambda: types.SimpleNamespace(
        persistence=types.SimpleNamespace(transaction=lambda e: e.begin(),
                                          list_runs=lambda *a, **k: [])))


def _crun(c, project, run_id, *, attempt, root, parent=None):
    crun = f"crun_{uuid.uuid4().hex[:12]}"
    fc = {"attempt": attempt, "root_run_id": root or crun, "workflow_run_id": run_id,
          "failure_class": "infra", "execution_substrate": "local_subprocess",
          "saved_workspace": {"files": [{"path": "main.go", "content": "package main"}],
                              "file_count": 1}}
    if parent:
        fc["parent_run_id"] = parent
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
        "state, finalize_ctx, error) VALUES (:id, :p, NULL, 'failed', 'done', 'Gateway', "
        "CAST(:spec AS JSONB), '{}'::jsonb, CAST(:fc AS JSONB), :err)"),
        {"id": crun, "p": project, "err": OLD_DB_ERROR,
         "spec": json.dumps({"instructions": "build it", "acceptance_criteria": [],
                             "test_commands": [], "owner": "acme", "repo": "web",
                             "base_branch": "main"}),
         "fc": json.dumps(fc)})
    return crun


def _failed_lineage_and_retry(env, run_id: str, *, actor: str = ALICE) -> tuple[str, str]:  # noqa: F811
    """The northgate step: two infra-failed attempts, the step's output still holding the
    second one's `await`, `status: failed` and the recovery record; then a person's Retry."""
    with env.engine.begin() as c:
        first = _crun(c, env.project, run_id, attempt=1, root=None)
        second = _crun(c, env.project, run_id, attempt=2, root=first, parent=first)
        output = {
            "capability": "build", "tool": "create_code_task", "run_id": first,
            "await": {"id": second, "kind": "code_task"}, "code_task_run_id": second,
            "status": "failed", "verdict": "awaiting", "awaited_status": "failed",
            "recovery": {"role": "engineering_captain", "agent": "Helm", "windows": 1,
                         "decisions": [{"n": 1, "action": "ask_person",
                                        "after": "Press Retry on this step when it is done; "
                                                 "the recovery starts again."}]},
            "recovery_question": OLD_DB_ERROR,
        }
        c.execute(text(
            "UPDATE workflow_step_runs SET status='failed', last_error=:e, "
            "output=CAST(:o AS JSONB) WHERE workflow_run_id=:r AND step_id='build'"),
            {"r": run_id, "o": json.dumps(output), "e": "the code_task it started failed"})
        c.execute(text("UPDATE workflow_runs SET status='failed' WHERE id=:r"), {"r": run_id})
        workflows.retry_failed_step(c, project_id=env.project, run_id=run_id, step_id="build",
                                    actor=actor)
    return first, second


def _ctx():
    return types.SimpleNamespace(engine=None, settings=get_settings(), jobs=_Jobs())


def _resume(env, run_id):  # noqa: F811
    ctx = _ctx()
    ctx.engine = env.engine
    with lineage.acting(lineage.SYSTEM_ACTING), env.engine.begin() as c:
        return runner._retried_build(c, ctx, env.project, {"id": "build"}, {"run_id": run_id},
                                     {"title": "Gateway", "instructions": "build it"})


def _started(env):  # noqa: F811
    version = _publish(env, SPEC, "retry-after-infra")
    return _start_consented(env, version, RUN_INPUT)["id"]


def test_a_persons_retry_after_two_infra_failures_starts_the_next_attempt(env, staffed):  # noqa: F811
    run_id = _started(env)
    first, second = _failed_lineage_and_retry(env, run_id)
    resumed, _, _ = _resume(env, run_id)
    assert resumed is not None, "the person's Retry started nothing"
    new_run = resumed["run_id"]
    assert new_run not in (first, second)
    assert resumed["resumed_from_code_task_run_id"] == second
    with env.engine.begin() as c:
        fc = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:i"),
                       {"i": new_run}).scalar()
        queued = c.execute(text("SELECT count(*) FROM jobs WHERE kind='code.task' "
                                "AND payload->>'run_id' = :r"), {"r": new_run}).scalar()
    # Linked as the retry, in the same lineage: same root, the failed attempt as its parent.
    assert fc["root_run_id"] == first and fc["parent_run_id"] == second
    assert int(fc["attempt"]) == 3
    assert queued == 1


def test_the_retried_step_waits_on_the_new_build_with_the_stale_await_cleared(
        env, staffed):  # noqa: F811
    run_id = _started(env)
    _, second = _failed_lineage_and_retry(env, run_id)
    ctx = _ctx()
    ctx.engine = env.engine
    with lineage.acting(lineage.SYSTEM_ACTING), env.engine.begin() as c:
        runner.tick(c, ctx, project_id=env.project)
    with env.engine.begin() as c:
        step = c.execute(text("SELECT status, last_error, output FROM workflow_step_runs "
                              "WHERE workflow_run_id=:r AND step_id='build'"),
                         {"r": run_id}).mappings().one()
        dead = c.execute(text("SELECT count(*) FROM deadletters WHERE payload->>"
                              "'workflow_run_id' = :r"), {"r": run_id}).scalar()
    assert dead == 0, step["last_error"]
    assert step["status"] in {"awaiting", "running", "waiting"}, (step["status"],
                                                                  step["last_error"])
    output = step["output"]
    awaited = (output.get("await") or {}).get("id")
    assert awaited and awaited != second, output.get("await")
    assert output.get("resumed_from_code_task_run_id") == second
    # Round 1's verdict is gone: nothing on the step still says the build failed.
    assert output.get("status") != "failed"
    assert "recovery" not in output and "awaited_status" not in output


def test_an_automatic_retry_with_no_note_still_meets_the_circuit_breaker(env, staffed):  # noqa: F811
    """The breaker is unchanged for BOSS itself: an agent's blind retry is still refused —
    once, in the breaker's own words, never as "BOSS couldn't save this to its database"."""
    run_id = _started(env)
    _failed_lineage_and_retry(env, run_id, actor="agent:engineering_captain")
    with pytest.raises(runner.SettledCapabilityError, match="circuit breaker"):
        _resume(env, run_id)


@pytest.fixture()
def _dead_letters_cleared(env):  # noqa: F811
    yield
    with env.engine.begin() as c:   # before env's teardown removes the project they reference
        c.execute(text("DELETE FROM deadletters WHERE project_id = :p"), {"p": env.project})


def test_a_real_database_failure_is_recorded_on_the_step_and_its_dead_letter(
        env, staffed, monkeypatch, _dead_letters_cleared):  # noqa: F811
    """When the retry DOES fail on the database, the sentence is the short one — and the
    failure itself (type, message, stack) is on the step's output and on the dead letter."""
    from sqlalchemy.exc import OperationalError

    from boss_engine.services import code_task_service as cts

    def broken(*_a, **_k):
        raise OperationalError("INSERT INTO code_task_runs (id) VALUES (%(id)s)", {"id": "x"},
                               Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(cts, "retry_code_task", broken)
    run_id = _started(env)
    _failed_lineage_and_retry(env, run_id)
    ctx = _ctx()
    ctx.engine = env.engine
    for _ in range(4):
        with lineage.acting(lineage.SYSTEM_ACTING), env.engine.begin() as c:
            c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second'"
                           " WHERE workflow_run_id = :r AND status = 'ready'"), {"r": run_id})
            runner.tick(c, ctx, project_id=env.project)
    with env.engine.begin() as c:
        step = c.execute(text("SELECT status, last_error, output FROM workflow_step_runs "
                              "WHERE workflow_run_id=:r AND step_id='build'"),
                         {"r": run_id}).mappings().one()
        dead = c.execute(text("SELECT reason, payload FROM deadletters WHERE payload->>"
                              "'workflow_run_id' = :r"), {"r": run_id}).mappings().one()
    assert step["status"] == "deadlettered"
    # A lost connection is said as that, with its way out (BOSSAPP-1428), never "couldn't save".
    assert step["last_error"].startswith("BOSS lost its connection to its database"), step
    assert "`boss doctor`" in step["last_error"]
    assert "server log" not in step["last_error"]
    record = step["output"]["error_record"]
    assert record["type"] == "sqlalchemy.exc.OperationalError"
    assert "server closed the connection" in record["message"]
    assert "Traceback" in record["stack"] and "broken" in record["stack"]
    assert dead["payload"]["error"]["type"] == "sqlalchemy.exc.OperationalError"
    assert "Traceback" in dead["payload"]["error"]["stack"]
