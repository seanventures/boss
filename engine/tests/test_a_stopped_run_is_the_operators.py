"""A run stopped at a failed step is waiting for its operator — not finished, and not stuck.

THE REVIEW (of 534a1b9 and its fix c8de36b, 2026-10-02). "A failed step stops the run" made
`failed` mean "stopped, waiting for you" on the run card and under "Needs you". Three places
still read it the old way, as "over":

1. STOP AND REMOVE. Stop was not offered and `cancel_run` returned early for a stopped run. The
   reviewer's probe: a fork with branch `a` dead-lettered and branch `b` still leased. The run
   offered {recheck, rerun, remove, retry_step, skip_step} and no Stop; `cancel_run` returned
   'deadlettered' and `b` stayed leased — the 2026-09-02 shape, a cancel that does not stop the
   work. Remove would have deleted the run's records while `b` went on, and the only way to
   abandon the run was to delete it.
2. THE SENTENCE OUTLIVED THE STOP. A retried build reopened the failed step through
   `reopen_failed_step`, which wrote `status='waiting'` itself and left
   "Stopped at a: a broke. Try it again, or skip it and carry on." on the run — through the
   wait and after the run COMPLETED, a red instruction with no buttons under a finished run.
3. A FAN-OUT COULD NOT TOLERATE ONE FAILED SCOPE. The bundled `agent_group_fan_out_join` joins
   its scope workers on `success_policy: any` and promises to "make partial failures visible";
   one failed worker stopped the whole run.

Each is enforced here for every state a run stops in, and every writer of a run's status is
enumerated so the next one cannot leave a stale stop behind.
"""
from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from boss_engine.services import workflow_run_actions as actions_mod
from boss_engine.services import workflows

from .test_a_run_never_completes_past_a_failure import (
    WORKER,
    _claim,
    _fork_join,
    _run,
    _stop,
    _succeed,
)
from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _publish,
    _start,
    _steps,
    eng,
    project,
)

ROOT = Path(__file__).resolve().parents[2]
STOPPED = sorted(actions_mod.STOPPED_RUN_STATES)


def _offered(c, project_id, run_id):
    return {a["key"] + (f":{a['step_id']}" if "step_id" in a else ""): a
            for a in workflows.get_run(c, project_id, run_id)["actions"]}


def _stopped_with_a_live_branch(c, project_id, state):
    """The reviewer's probe: b leased by a worker, a stopped in `state`."""
    run = _fork_join(c, project_id)
    b = _claim(c, project_id, run["id"], "b")
    _stop(c, project_id, run["id"], "a", state)
    assert _run(c, run["id"])["status"] in actions_mod.STOPPED_RUN_STATES
    return run["id"], b


# ── 1. Stop and Remove on a stopped run ─────────────────────────────────────────────────────

def test_the_states_a_run_stops_in_are_the_states_its_steps_stop_it_in():
    """`_stop_run_at` writes the run's status from the step's, so the run's stopped states are
    exactly the step's. A new stopped step state without a run state would be filed as active."""
    assert actions_mod.STOPPED_RUN_STATES == set(workflows.STOPPED_STEP_STATES)
    assert actions_mod.STOPPED_RUN_STATES <= actions_mod.FINISHED_RUN_STATES
    assert not actions_mod.STOPPED_RUN_STATES & actions_mod.CLOSED_RUN_STATES


@pytest.mark.parametrize("state", STOPPED)
def test_a_stopped_run_offers_stop_and_holds_remove_while_a_branch_is_working(
        eng, project, state):
    """Stop is offered on a stopped run; Remove is shown but refused while `b` is still working,
    and says what to do instead."""
    with eng.begin() as c:
        run_id, _b = _stopped_with_a_live_branch(c, project, state)
        offered = _offered(c, project, run_id)
        assert offered["stop"]["enabled"] is True
        assert offered["stop_and_rerun"]["enabled"] is True
        assert "rerun" not in offered, "Run again beside a stopped run leaves two runs"
        assert offered["remove"]["enabled"] is False
        assert "'b' is still working" in offered["remove"]["reason"]
        with pytest.raises(actions_mod.RunActionRefused, match="Stop the run first"):
            workflows.delete_run(c, project, run_id, "ceo")


@pytest.mark.parametrize("state", STOPPED)
def test_stopping_a_stopped_run_stops_the_branch_still_working(eng, project, state):
    """THE PROBE, FIXED. `cancel_run` cancels the stopped run and the live branch under it — a
    worker that then tries to finish or park `b` is refused, so nothing it started is recorded
    as the run's — and clears the stop's sentence. Then the run can be removed."""
    with eng.begin() as c:
        run_id, b = _stopped_with_a_live_branch(c, project, state)
        result = workflows.cancel_run(c, project, run_id, "ceo", reason="abandoning it")
        assert result["status"] == "cancelled"
        steps, run = _steps(c, run_id), _run(c, run_id)
        assert steps["b"]["status"] == "cancelled", "the branch still working was not stopped"
        assert steps["a"]["status"] == state, "the failure stays on the record"
        assert run["blocked_reason"] is None
        with pytest.raises(workflows.WorkflowLeaseError):
            workflows.park_step_awaiting(c, project_id=project, step_run_id=b["id"],
                                         worker_id=WORKER,
                                         output={"await": {"kind": "code_task", "id": "crun_y"}})
        assert _run(c, run_id)["status"] == "cancelled", "a late park revived the run"
        assert workflows.delete_run(c, project, run_id, "ceo")["deleted"] is True


@pytest.mark.parametrize("state", STOPPED)
def test_stopping_a_stopped_run_cancels_the_work_its_branch_started(eng, project, state):
    """A branch parked on a discovery under a stopped run: Stop reaches through to it."""
    did = f"drun_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        run = _fork_join(c, project)
        b = _claim(c, project, run["id"], "b")
        c.execute(text("INSERT INTO discovery_runs (id, project_id, status, phase, title) "
                       "VALUES (:i, :p, 'running', 'vet', 'x')"), {"i": did, "p": project})
        workflows.park_step_awaiting(c, project_id=project, step_run_id=b["id"],
                                     worker_id=WORKER,
                                     output={"await": {"kind": "discovery", "id": did}})
        _stop(c, project, run["id"], "a", state)
        assert _run(c, run["id"])["status"] == state, "parking b un-stopped the run"
        workflows.cancel_run(c, project, run["id"], "ceo", reason="abandoning it")
        work = c.execute(text("SELECT status FROM discovery_runs WHERE id=:i"),
                         {"i": did}).scalar()
        c.execute(text("DELETE FROM discovery_runs WHERE id=:i"), {"i": did})
    assert work == "cancelled"


@pytest.mark.parametrize("state", STOPPED)
def test_a_stopped_run_with_nothing_live_can_be_removed_as_before(eng, project, state):
    """The behaviour that worked keeps working: clearing out a dead run needs no Stop first."""
    with eng.begin() as c:
        run = _fork_join(c, project)
        _succeed(c, project, run["id"], "b")
        _stop(c, project, run["id"], "a", state)
        assert _offered(c, project, run["id"])["remove"]["enabled"] is True
        assert workflows.delete_run(c, project, run["id"], "ceo")["deleted"] is True


# ── 2. The stop's sentence goes with the stop ───────────────────────────────────────────────

def _one_build_step(c, project_id):
    _, version = _publish(c, project_id, f"b_{uuid.uuid4().hex[:6]}",
                          [{"id": "a", "kind": "task", "max_attempts": 1}], "a")
    return _start(c, project_id, version["id"], {})["id"]


def test_a_run_revived_by_a_retried_build_drops_the_stopped_sentence(eng, project):
    """The reviewer's sequence on main: STOPPED {failed, 'Stopped at a: a broke. Try it
    again…'} → REOPENED {waiting, same sentence} → DONE {completed, same sentence}."""
    with eng.begin() as c:
        run_id = _one_build_step(c, project)
        _stop(c, project, run_id, "a", "failed")
        stopped = _run(c, run_id)
        assert stopped["status"] == "failed"
        assert stopped["blocked_reason"].startswith("Stopped at a: a broke.")
        step_run_id = _steps_ids(c, run_id)["a"]

        workflows.reopen_failed_step(c, project_id=project, step_run_id=step_run_id,
                                     handle={"kind": "code_task", "id": "crun_retry"},
                                     note="the build was retried")
        reopened = _run(c, run_id)
        assert reopened["status"] == "waiting"
        assert reopened["blocked_reason"] is None, reopened
        assert reopened["error"] is None

        workflows.force_complete_waiting_step(c, project_id=project, step_run_id=step_run_id,
                                              output={"status": "succeeded"})
        done = _run(c, run_id)
    assert done["status"] == "completed"
    assert done["blocked_reason"] is None, done


def test_a_branch_repointed_onto_a_retried_build_drops_the_sentence_and_the_run_stops_again_at_the_other(
        eng, project):
    """Repointing used to set `waiting` with the sentence left on. Through the one door out of a
    stop, the sentence goes — and once the repointed branch finishes, the run stops again at the
    failure nobody has decided about, with that step's own sentence."""
    with eng.begin() as c:
        run = _fork_join(c, project)
        b = _claim(c, project, run["id"], "b")
        workflows.park_step_awaiting(c, project_id=project, step_run_id=b["id"],
                                     worker_id=WORKER,
                                     output={"await": {"kind": "code_task", "id": "crun_b1"}})
        _stop(c, project, run["id"], "a", "deadlettered")
        workflows.repoint_waiting_step(c, project_id=project, step_run_id=b["id"],
                                       handle={"kind": "code_task", "id": "crun_b2"},
                                       note="the build was retried")
        moving = _run(c, run["id"])
        assert moving["status"] == "waiting" and moving["blocked_reason"] is None, moving
        workflows.force_complete_waiting_step(c, project_id=project, step_run_id=b["id"],
                                              output={"status": "succeeded"})
        again = _run(c, run["id"])
    assert again["status"] == "deadlettered"
    assert again["blocked_reason"].startswith("Stopped at a: a broke.")


def test_nothing_revives_a_run_that_is_over(eng, project):
    """The one door out of a stop never opens a closed run: a reopen arriving after Stop leaves
    the run cancelled."""
    with eng.begin() as c:
        run_id = _one_build_step(c, project)
        _stop(c, project, run_id, "a", "failed")
        workflows.cancel_run(c, project, run_id, "ceo", reason="done with it")
        workflows.reopen_failed_step(c, project_id=project,
                                     step_run_id=_steps_ids(c, run_id)["a"],
                                     handle={"kind": "code_task", "id": "crun_late"},
                                     note="a late retry")
        assert _run(c, run_id)["status"] == "cancelled"
        assert workflows._set_moving_again(c, run_id) == "cancelled"
        assert _run(c, run_id)["status"] == "cancelled"


def _steps_ids(c, run_id):
    return {r["step_id"]: r["id"] for r in c.execute(
        text("SELECT id, step_id FROM workflow_step_runs WHERE workflow_run_id=:r"),
        {"r": run_id}).mappings()}


def test_try_again_locks_the_run_before_it_reads_it():
    """Try again checked the run unlocked and then set it running; a Stop committing in between
    was undone. It now takes the run first — the order Skip, Stop and Remove lock in — and the
    check itself (`require`) holds the run row, so every gated operation is serialized."""
    import inspect

    retry = inspect.getsource(workflows.retry_failed_step)
    assert retry.index("_run_row(connection, run_id, lock=True)") \
        < retry.index("_latest_step_row(")
    gate = inspect.getsource(actions_mod.require)
    assert gate.index("FOR UPDATE") < gate.index("load_runs(")
    moving = inspect.getsource(workflows._set_moving_again)
    assert "status IN :movable" in moving, "the write itself must refuse a run that was stopped"


# ── Every writer of a run's status ──────────────────────────────────────────────────────────

_STOP_STATES = set(workflows.STOPPED_STEP_STATES) | {"blocked"}
_UPDATE_RUN_STATUS = re.compile(r"UPDATE\s+workflow_runs\b.*?\bSET\b.*?\bstatus\s*=", re.S | re.I)


def _sql_strings(path: Path):
    """Every string literal (and f-string's literal text) in a module, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(v.value for v in node.values
                                       if isinstance(v, ast.Constant))


def _status_guard(where: str) -> set[str] | None:
    """The run states a WHERE clause lets the UPDATE touch, or None when it does not say."""
    found = re.search(r"\bstatus\s+NOT\s+IN\s*\(([^)]*)\)", where, re.I)
    if found:
        return {"<anything but>"} | {"not:" + s for s in re.findall(r"'([a-z_]+)'", found[1])}
    found = re.search(r"\bstatus\s+IN\s*\(([^)]*)\)", where, re.I)
    if found:
        return set(re.findall(r"'([a-z_]+)'", found[1]))
    found = re.search(r"\bstatus\s*=\s*'([a-z_]+)'", where, re.I)
    if found:
        return {found[1]}
    if re.search(r"\bstatus\s+IN\s+:movable", where):
        return set(workflows.MOVABLE_RUN_STATES) - {"failed", "deadlettered", "blocked"} \
            | {"<one door>"}
    return None


def _leaves_a_stop_behind(sql: str) -> str | None:
    """Why this run-status write could leave a stopped run's sentence behind, or None."""
    parts = re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.I)
    head, where = parts[0], (parts[1] if len(parts) > 1 else "")
    target = re.search(r"\bstatus\s*=\s*'([a-z_]+)'", head, re.I)
    if target and target[1] in _STOP_STATES:
        return None                               # writing a stop, not leaving one
    if re.search(r"\bblocked_reason\s*=", head, re.I):
        return None                               # clears (or rewrites) the sentence itself
    guard = _status_guard(where)
    if guard is None:
        return "it does not say which run states it may move, and sets no blocked_reason"
    if "<one door>" in guard:
        return None
    if "<anything but>" in guard:
        excluded = {g[4:] for g in guard if g.startswith("not:")}
        missing = set(workflows.STOPPED_STEP_STATES) - excluded
        return f"its NOT IN guard lets it move a run that is {sorted(missing)}" if missing else None
    moved = guard & set(workflows.STOPPED_STEP_STATES)
    return f"it may move a run that is {sorted(moved)}" if moved else None


def test_every_writer_of_a_runs_status_clears_the_stop_it_leaves():
    """ENUMERATED, so the next writer cannot reintroduce the stale sentence. Every statement in
    the engine and the API that sets `workflow_runs.status` must do one of: write a stop itself;
    set `blocked_reason` in the same statement; or carry a status guard that can never match a
    stopped run. `reopen_failed_step` and `repoint_waiting_step` wrote `status='waiting'` with
    none of these — the incident."""
    sources = sorted((ROOT / "engine" / "boss_engine").rglob("*.py")) \
        + sorted((ROOT / "apps" / "api" / "app").rglob("*.py")) \
        + sorted((ROOT / "apps" / "worker").rglob("*.py"))
    seen, offenders = 0, []
    for path in sources:
        if "/tests/" in str(path):
            continue
        for line, value in _sql_strings(path):
            if not _UPDATE_RUN_STATUS.search(value):
                continue
            seen += 1
            why = _leaves_a_stop_behind(value[value.upper().index("UPDATE"):])
            if why:
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {why}")
    assert seen >= 15, f"found only {seen} run-status writers — the scan is not reading them"
    assert not offenders, "\n".join(offenders)


def test_the_scan_catches_the_shape_that_caused_the_incident():
    """The scan is only worth its enumeration if it fails on the bug it was written for."""
    assert _leaves_a_stop_behind(
        "UPDATE workflow_runs SET status='waiting', error=NULL, updated_at=now() WHERE id=:id")
    assert _leaves_a_stop_behind(
        "UPDATE workflow_runs SET status='running' WHERE id=:id AND status IN ('failed')")
    assert not _leaves_a_stop_behind(
        "UPDATE workflow_runs SET status='waiting' WHERE id=:id AND status IN ('running','waiting')")


# ── 3. A fan-out tolerates a failed scope ───────────────────────────────────────────────────

def _fan_out_template():
    """The bundled template's own steps — the scope workers, their `any` join and what follows —
    with the steps that need real agent-group state left out."""
    spec = yaml.safe_load((ROOT / "packages" / "specpack" / "workflows" /
                           "agent_group_fan_out_join.yaml").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in spec["steps"]}
    keep = ["execute_scopes", "scope_worker_a", "scope_worker_b", "scope_worker_c",
            "join_scope_results", "record_results_and_failures"]
    steps = []
    for step_id in keep:
        step = {k: v for k, v in by_id[step_id].items() if k != "input"}
        if step["kind"] == "task":
            step["max_attempts"] = 1
        if step_id == "record_results_and_failures":
            step.pop("next", None)
        steps.append(step)
    assert by_id["join_scope_results"]["success_policy"] == "any"
    return steps


def _start_fan_out(c, project_id):
    _, version = _publish(c, project_id, f"fan_{uuid.uuid4().hex[:6]}", _fan_out_template(),
                          "execute_scopes")
    run = _start(c, project_id, version["id"], {})
    return run["id"]


def _fail(c, project_id, run_id, step_id):
    claimed = _claim(c, project_id, run_id, step_id)
    workflows.fail_step(c, project_id=project_id, step_run_id=claimed["id"], worker_id=WORKER,
                        error=f"{step_id} broke", retryable=False)


@pytest.mark.parametrize("first_fails", [True, False], ids=["fails-first", "fails-after-join"])
def test_one_failed_scope_worker_does_not_stop_the_fan_out(eng, project, first_fails):
    """`success_policy: any` means what it says. The failed worker stays failed and visible —
    with the join it was tolerated by — and the run goes on to record results and completes."""
    with eng.begin() as c:
        run_id = _start_fan_out(c, project)
        for step_id in ("scope_worker_a", "scope_worker_b", "scope_worker_c"):
            _claim(c, project, run_id, step_id)
        if first_fails:
            _fail(c, project, run_id, "scope_worker_a")
            assert _run(c, run_id)["status"] not in STOPPED
        _succeed(c, project, run_id, "scope_worker_b")
        if not first_fails:
            _fail(c, project, run_id, "scope_worker_a")
        _succeed(c, project, run_id, "scope_worker_c")
        _succeed(c, project, run_id, "record_results_and_failures")
        steps, run = _steps(c, run_id), _run(c, run_id)
    assert run["status"] == "completed", run
    assert steps["scope_worker_a"]["status"] == "deadlettered"
    assert steps["scope_worker_a"]["output"].get("tolerated_by_join") == "join_scope_results"


def test_a_fan_out_whose_every_worker_failed_stops_at_the_last_one(eng, project):
    """`any` still needs one. With every branch failed nothing can release the join, and the
    run stops — never completes past three failures."""
    with eng.begin() as c:
        run_id = _start_fan_out(c, project)
        for step_id in ("scope_worker_a", "scope_worker_b", "scope_worker_c"):
            _claim(c, project, run_id, step_id)
        for step_id in ("scope_worker_a", "scope_worker_b", "scope_worker_c"):
            _fail(c, project, run_id, step_id)
        run = _run(c, run_id)
    assert run["status"] == "deadlettered"
    assert run["blocked_reason"].startswith("Stopped at scope_worker_c")


def test_an_all_join_still_stops_at_any_failed_branch(eng, project):
    """The tolerance is the `any` join's alone: the default join keeps stopping the run."""
    with eng.begin() as c:
        run = _fork_join(c, project)
        _claim(c, project, run["id"], "b")
        _stop(c, project, run["id"], "a", "deadlettered")
        assert _run(c, run["id"])["status"] == "deadlettered"


# ── What a later step is told about a skipped one ───────────────────────────────────────────

def _two_steps(c, project_id):
    _, version = _publish(c, project_id, f"xy_{uuid.uuid4().hex[:6]}",
                          [{"id": "x", "kind": "task", "next": ["y"]},
                           {"id": "y", "kind": "task"}], "x")
    return _start(c, project_id, version["id"], {})["id"]


def _set_row(c, run_id, step_id, iteration, status, output):
    c.execute(text("UPDATE workflow_step_runs SET status=:s, output=CAST(:o AS JSONB), "
                   "created_at=clock_timestamp() "
                   "WHERE workflow_run_id=:r AND step_id=:st AND iteration=:i"),
              {"s": status, "o": __import__("json").dumps(output), "r": run_id,
               "st": step_id, "i": iteration})


def test_a_branch_the_run_never_took_is_not_an_operators_skip(eng, project):
    """The runner treated every skipped row as "Skip and proceed", so a step reading from a
    branch the condition did not select was failed with "step x was skipped… Retry x, or skip
    this step too" — and Retry on x is refused, because x never failed (review of c8de36b)."""
    from boss_engine.services import workflow_capability_runner as runner

    with eng.begin() as c:
        run_id = _two_steps(c, project)
        _set_row(c, run_id, "x", 0, "skipped",
                 {"skipped": True, "reason": "condition_not_selected"})
        context = runner._run_context(c, run_id)
        assert "x" not in (context.get("skipped") or {})
        with pytest.raises(runner.CapabilityExecutionError) as raised:
            runner._resolve_inputs({"v": {"from_step": "x", "field": "f"}}, context)
    assert not isinstance(raised.value, runner.SettledCapabilityError)
    assert "was skipped" not in str(raised.value)
    assert "did not produce 'f'" in str(raised.value)


def test_a_loop_body_skipped_then_run_again_is_read_as_what_it_produced(eng, project):
    """Skipped by the operator in iteration 0, succeeded in iteration 1: the later step gets the
    value, not "it was skipped". The latest attempt wins, as it does for the run's actions."""
    from boss_engine.services import workflow_capability_runner as runner

    with eng.begin() as c:
        run_id = _two_steps(c, project)
        _set_row(c, run_id, "x", 0, "skipped",
                 {"skipped_by_operator": True, "skip_reason": "flaky", "skipped_by": "ceo"})
        workflows._insert_step_run(c, project_id=project, run_id=run_id,
                                   step={"id": "x", "kind": "task"}, iteration=1,
                                   status="pending")
        _set_row(c, run_id, "x", 1, "succeeded", {"f": "the real value"})
        context = runner._run_context(c, run_id)
        resolved = runner._resolve_inputs({"v": {"from_step": "x", "field": "f"}}, context)
    assert resolved == {"v": "the real value"}
    assert "x" not in (context.get("skipped") or {})


def test_an_operators_skip_after_a_success_is_what_a_later_step_is_told(eng, project):
    """And the other way round: the latest attempt was skipped by an operator, so the earlier
    value is not handed on as though it were current."""
    from boss_engine.services import workflow_capability_runner as runner

    with eng.begin() as c:
        run_id = _two_steps(c, project)
        _set_row(c, run_id, "x", 0, "succeeded", {"f": "stale"})
        workflows._insert_step_run(c, project_id=project, run_id=run_id,
                                   step={"id": "x", "kind": "task"}, iteration=1,
                                   status="pending")
        _set_row(c, run_id, "x", 1, "skipped",
                 {"skipped_by_operator": True, "skip_reason": "not needed", "skipped_by": "ceo"})
        context = runner._run_context(c, run_id)
        with pytest.raises(runner.SettledCapabilityError, match="skipped by ceo"):
            runner._resolve_inputs({"v": {"from_step": "x", "field": "f"}}, context)


# ── The Command Center's "Stopped" list is the same rule ────────────────────────────────────

def _run_statuses():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS workflow_runs"):]
    check = block[block.index("CHECK (status IN ("):block.index("))")]
    return sorted(set(re.findall(r"'([a-z_]+)'", check)))


@pytest.mark.parametrize("status", _run_statuses())
def test_the_briefing_lists_a_failed_step_exactly_where_the_run_offers_an_answer(
        eng, project, status):
    """For EVERY run status the database allows: a dead-lettered step is under the Command
    Center's "Stopped" exactly when its run offers Try again or Skip on it, and the briefing's
    actions are exactly those answers. The probe that found this: a COMPLETED run's dead-lettered
    step listed with "Re-check what stopped it", which that run refuses."""
    from boss_engine.services import factory_briefing

    with eng.begin() as c:
        run = _fork_join(c, project)
        _succeed(c, project, run["id"], "b")
        _stop(c, project, run["id"], "a", "deadlettered")
        c.execute(text("UPDATE workflow_runs SET status=:s WHERE id=:r"),
                  {"s": status, "r": run["id"]})
        listed = [i for i in factory_briefing._stuck_steps(c, project)
                  if i["run_id"] == run["id"]]
        offered = {a["key"]: a for a in workflows.get_run(c, project, run["id"])["actions"]
                   if a.get("step_id") == "a" and a["enabled"]}
    tools = {"retry_step": "retry_workflow_step", "skip_step": "skip_workflow_step"}
    if not offered:
        assert listed == [], f"a {status} run's step is listed, but the run offers no answer"
        return
    assert len(listed) == 1, f"a {status} run offers {sorted(offered)} on 'a' but is not listed"
    assert {a["tool"] for a in listed[0]["actions"]} == {tools[k] for k in offered}
    assert all(a["args"] == {"run_id": run["id"], "step_id": "a"} for a in listed[0]["actions"])


def test_a_completed_runs_old_failure_is_not_under_stopped(eng, project):
    """The legacy `failed_continued` rows the schema rewrite turned into `failed` sit on runs
    that completed long ago; they are history, not something that wants you."""
    from boss_engine.services import factory_briefing

    with eng.begin() as c:
        run = _fork_join(c, project)
        _succeed(c, project, run["id"], "b")
        _stop(c, project, run["id"], "a", "failed")
        c.execute(text("UPDATE workflow_runs SET status='completed' WHERE id=:r"),
                  {"r": run["id"]})
        listed = [i for i in factory_briefing._stuck_steps(c, project)
                  if i["run_id"] == run["id"]]
    assert listed == []
