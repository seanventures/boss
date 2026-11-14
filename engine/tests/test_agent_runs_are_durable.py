"""AP-P2 (BOSSAPP-1053): durable threads, checkpoints, one interrupt primitive, execution on
workers — the acceptance tests of docs/design/AGENT_PLATFORM.md §5 Phase 2, on Postgres.

  (a) kill -9 the worker mid tool batch: on restart only the unfinished calls run again, and an
      irreversible call that started without a recorded result PARKS as an unknown outcome.
  (b) a deploy's drain mid run: the run stops at a step boundary with a checkpoint, its job goes
      back to the queue without spending an attempt, and the next lease resumes from there.
  (c) parallel interrupts from two tools resume in one decision batch whatever order they were
      answered in; an argument changed after the approval is refused by the hash binding.
  (d) every double-text policy: enqueue, reject, interrupt, rollback.
  (e) a failed run forked at step k with an edited tool result: the branch completes and the
      original checkpoints never change.

Plus the enforcing lints: every write tool declares an idempotency class; every production
kernel.run is given a checkpoint store; admission writes the run and its job in one transaction.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine import kernel
from boss_engine.kernel.durable import PgCheckpointStore, fork, measure
from boss_engine.kernel.interrupts import MemoryInterrupts, interrupt
from boss_engine.kernel.scripted import Call, Calls, Fail, Say, ScriptedEngine
from boss_engine.kernel.toolset import Tool
from boss_engine.kernel.types import ToolSchema

from ._durable_support import OBJ, count, ledger_lines, policies, toolset

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_checkpoints LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("the test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'durable runs', :s)"), {"i": pid, "w": wid, "s": pid})
        # The person who answers the decisions (a project member who may resolve them).
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid})
    yield pid
    with eng.begin() as c:
        runs = [r[0] for r in c.execute(text("SELECT id FROM agent_runs WHERE project_id = :p"),
                                        {"p": pid}).all()]
        c.execute(text("DELETE FROM agent_checkpoints WHERE run_id = ANY(:r) OR run_id LIKE :k"),
                  {"r": runs, "k": f"krun_{pid}%"})
        c.execute(text("DELETE FROM trace_spans WHERE project_id = :p"), {"p": pid})
        for table in ("jobs", "decisions", "events", "agent_runs", "agent_threads",
                      "project_members"):
            with c.begin_nested():
                c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _spec(project, run_id, engine_script, tools, store, **kw):
    return kernel.RunSpec(engine=ScriptedEngine(engine_script), tools=tools, policies=policies(),
                          project_id=project, system="You are a billing agent.",
                          messages=[{"role": "user", "content": "charge and notify"}],
                          run_id=run_id, persist=True, store=store, **kw)


def _tool_messages(state) -> dict:
    return {m["tool_call_id"]: m["content"] for m in state.messages if m.get("role") == "tool"}


# ════ (a) kill -9 mid tool batch ═══════════════════════════════════════════════════════════════

def test_a_killed_worker_reruns_only_the_unfinished_calls_and_parks_the_irreversible_one(
        eng, project, tmp_path):
    run_id, ledger = f"krun_{project}_crash", tmp_path / "effects.log"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "engine"), str(ROOT)])}
    child = subprocess.Popen([sys.executable, str(HERE / "_durable_crash_child.py"),
                              str(eng.url.render_as_string(hide_password=False)), project, run_id,
                              str(ledger)], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60
    while count(ledger, "charge_card") == 0:
        assert child.poll() is None, child.stdout.read().decode()[-3000:]
        assert time.monotonic() < deadline, "the child never reached charge_card"
        time.sleep(0.05)
    os.kill(child.pid, signal.SIGKILL)  # mid-batch: read_a done, charge_card in flight
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL

    store = PgCheckpointStore(eng)
    crashed = store.load(run_id)
    assert crashed is not None and crashed.status == "running" and crashed.batch, \
        "the model turn and its calls were checkpointed before any tool ran"
    writes = store.writes(crashed.checkpoint_id)
    assert writes["call_read"]["status"] == "ok"          # finished: its result was written
    assert writes["call_charge"]["status"] == "started"   # in flight when killed
    assert "call_notify" not in writes                     # never started
    assert ledger_lines(ledger) == ["read_a", "charge_card"]

    # The worker comes back: the run resumes from its checkpoint.
    from boss_engine.kernel.ledger import DbSink

    ports = MemoryInterrupts()
    resumed = kernel.resume(_spec(project, run_id, [Say("should not be asked yet")],
                                  toolset(ledger), PgCheckpointStore(eng), interrupts=ports,
                                  parallel_interrupts=True, sink=DbSink(eng)))
    assert count(ledger, "read_a") == 1, "a finished call is REPLAYED, never re-run"
    assert count(ledger, "charge_card") == 1, "an irreversible call that may have happened is " \
                                              "never fired twice"
    assert count(ledger, "notify_b") == 1, "the call that never started runs now"
    assert resumed.status == "suspended" and resumed.stopped == "unknown_outcome"
    parked = resumed.pending["interrupts"]
    assert [i["kind"] for i in parked] == ["unknown_outcome"]
    assert parked[0]["call_id"] == "call_charge" and ports.opened[0]["id"] == parked[0]["id"]

    # A person says it happened: the run finishes, charge_card still ran exactly once.
    done = kernel.resume(_spec(project, run_id, [Say("done: charged once, notified once")],
                               toolset(ledger), PgCheckpointStore(eng), interrupts=ports,
                               parallel_interrupts=True, sink=DbSink(eng)),
                         answers={parked[0]["id"]: {"decision_id": parked[0]["decision_id"],
                                                    "happened": True}})
    assert done.status == "completed", done.error
    assert count(ledger, "charge_card") == 1 and count(ledger, "read_a") == 1
    final = PgCheckpointStore(eng).load(run_id)
    seen = _tool_messages(final)
    assert set(seen) == {"call_read", "call_charge", "call_notify"}
    assert "120" in seen["call_read"]  # the replayed result is the one the first process got

    # THE TRACE (AP-P4's trace middleware, through the checkpoint): one run, one trace, the
    # pre-crash step and the post-resume step under it, one interrupt span for the parked call.
    with eng.begin() as c:
        spans = [dict(r) for r in c.execute(text(
            "SELECT span_id, trace_id, parent_span_id, kind, name, status, step, attributes "
            "FROM trace_spans WHERE run_id = :r ORDER BY started_at"), {"r": run_id}
        ).mappings().all()]
    assert len({s["trace_id"] for s in spans}) == 1, "the resumed run continues the same trace"
    runs = [s for s in spans if s["kind"] == "run"]
    assert len(runs) == 1 and runs[0]["status"] == "ok"
    assert runs[0]["attributes"].get("resumed_from_checkpoint"), "the resume is visible"
    steps = {s["step"]: s for s in spans if s["kind"] == "step"}
    assert {1, 2} <= set(steps), "the pre-crash step and the post-resume step"
    assert all(s["parent_span_id"] == runs[0]["span_id"] for s in steps.values())
    tools = [s["name"] for s in spans if s["kind"] == "tool"]
    assert tools.count("read_a") == 1 and tools.count("notify_b") == 1, tools
    assert "charge_card" not in tools, "killed mid-call: it never returned, so no tool span " \
                                       "claims a result — its interrupt span says so instead"
    held = [s for s in spans if s["kind"] == "interrupt"]
    assert [s["name"] for s in held] == ["unknown_outcome charge_card"]
    assert held[0]["status"] == "ok" and held[0]["attributes"]["interrupt_id"] == parked[0]["id"]


# ════ (b) a deploy drains the run; no attempt is spent ═════════════════════════════════════════

def _scripted_runner(scripts: list, ledger: Path, drain_after_read: threading.Event):
    """A worker runner for a scripted agent; each execution takes the next script."""
    from boss_engine.services import agent_run_worker

    engines: list = []

    def build(connection, settings, run):
        engine = ScriptedEngine(scripts.pop(0))
        engines.append(engine)

        def read_and_get_drained(args: dict) -> dict:
            with ledger.open("a") as f:
                f.write("read_a\n")
            drain_after_read.set()  # SIGTERM arrives while this step's tool runs
            return {"ok": True, "balance": 120}

        tools = toolset(ledger, extra=())
        tools._tools["read_a"] = Tool(ToolSchema("read_a", "read the balance", OBJ),
                                      read_and_get_drained)
        messages = [m for m in (run.get("messages") or []) if m.get("role") != "system"]
        return kernel.RunSpec(engine=engine, tools=tools, policies=policies(), project_id="",
                              system="You are a billing agent.", messages=messages)

    return agent_run_worker.kernel_runner(build), engines


def test_a_drained_run_resumes_from_its_checkpoint_without_spending_an_attempt(
        eng, project, tmp_path, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services import agent_run_worker, thread_runs
    from boss_engine.services.jobs import JobLeaseService

    ledger, drained = tmp_path / "effects.log", threading.Event()
    runner, engines = _scripted_runner(
        [[Call("read_a", {"x": "a"}, id="c1"), Call("notify_b", {"x": "b"}, id="c2"),
          Say("never reached: drained")],
         [Call("notify_b", {"x": "b"}, id="c2"), Say("done after the deploy")]],
        ledger, drained)
    monkeypatch.setitem(agent_run_worker.RUNNERS, "scripted_p2", runner)
    with eng.begin() as c:
        thread = thread_runs.ensure_thread(c, project_id=project, actor="ceo")
        admitted = thread_runs.admit(c, project_id=project, thread_id=thread, messages=[
            {"role": "user", "content": "charge and notify"}],
            finalize_ctx={"runner": "scripted_p2"})
    run_id, job_id = admitted["run_id"], admitted["job_id"]
    jobs = JobLeaseService(get_settings())

    def lease(worker):
        with eng.begin() as c:
            return jobs.lease_owned_job(c, project_id=project, job_id=job_id,
                                        worker_id=worker, lease_ttl_seconds=60)

    first = lease("worker-1")
    out = agent_run_worker.execute(eng, get_settings(), kind="agent.run", job_id=job_id,
                                   run_id=run_id, worker_id="worker-1",
                                   lease_token=first["lease_token"], drain=drained.is_set)
    assert out["job_deferred"] and out["status"] == "drained"
    with eng.begin() as c:
        job = c.execute(text("SELECT status, attempt FROM jobs WHERE id = :j"),
                        {"j": job_id}).mappings().one()
        status = c.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        c.execute(text("UPDATE jobs SET run_after = now() WHERE id = :j"), {"j": job_id})
    assert job["status"] == "queued" and job["attempt"] == 0, "the drain gave the attempt back"
    assert status == "drained"
    assert len(engines[0].requests) == 1, "stopped at the step boundary after step 1"

    second = lease("worker-2")
    assert second["attempt"] == 1, "the run's second execution is still its first attempt"
    out = agent_run_worker.execute(eng, get_settings(), kind="agent.run", job_id=job_id,
                                   run_id=run_id, worker_id="worker-2",
                                   lease_token=second["lease_token"], drain=lambda: False)
    assert out["status"] == "completed", out
    assert count(ledger, "read_a") == 1, "step 1 was not repeated"
    first_request = engines[1].requests[0]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1"
               for m in first_request), "the resumed run continued from the checkpoint"
    with eng.begin() as c:
        assert c.execute(text("SELECT run_state FROM agent_threads WHERE id = :t"),
                         {"t": thread}).scalar() == "idle"


def test_a_worker_that_lost_its_lease_cannot_write_a_checkpoint(eng, project, tmp_path,
                                                                monkeypatch):
    """Fencing: the lease is checked before every checkpoint write."""
    from boss_engine.config import get_settings
    from boss_engine.services import agent_run_worker, thread_runs
    from boss_engine.services.jobs import JobLeaseService, LeaseNotFoundError

    runner, _ = _scripted_runner([[Say("anything")]], tmp_path / "x.log", threading.Event())
    monkeypatch.setitem(agent_run_worker.RUNNERS, "scripted_p2", runner)
    with eng.begin() as c:
        thread = thread_runs.ensure_thread(c, project_id=project, actor="ceo")
        admitted = thread_runs.admit(c, project_id=project, thread_id=thread,
                                     messages=[{"role": "user", "content": "hi"}],
                                     finalize_ctx={"runner": "scripted_p2"})
    with pytest.raises(LeaseNotFoundError):
        agent_run_worker.execute(eng, get_settings(), kind="agent.run",
                                 job_id=admitted["job_id"], run_id=admitted["run_id"],
                                 worker_id="worker-x", lease_token="lease_not_ours",
                                 jobs=JobLeaseService(get_settings()))
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM agent_checkpoints WHERE run_id = :r"),
                         {"r": admitted["run_id"]}).scalar() == 0


# ════ (c) parallel interrupts, one decision batch, bound to the arguments ══════════════════════

def _two_approvals(ledger: Path):
    def ask(name):
        def run(args: dict) -> dict:
            return interrupt("approval", {"summary": f"{name} {args.get('x')}?"})

        def approved(args: dict) -> dict:
            with ledger.open("a") as f:
                f.write(f"{name}:{args.get('x')}\n")
            return {"ok": True, "did": name}
        return run, approved

    send_run, send_ok = ask("send_invoice")
    refund_run, refund_ok = ask("refund")
    return toolset(ledger, extra=(
        Tool(ToolSchema("send_invoice", "send it", OBJ, kind="write", idempotency="keyed"),
             send_run, send_ok),
        Tool(ToolSchema("refund", "refund it", OBJ, kind="write", idempotency="irreversible"),
             refund_run, refund_ok)))


def _suspend_on_two(eng, project, run_id, ledger):
    from boss_engine.kernel.ledger import DbSink
    from boss_engine.services.interrupts import DecisionInterrupts

    with eng.begin() as c:
        result = kernel.run(_spec(
            project, run_id, [Calls(Call("send_invoice", {"x": "INV-1"}, id="s1"),
                                    Call("refund", {"x": "R-9"}, id="r1"))],
            _two_approvals(ledger), PgCheckpointStore(c), sink=DbSink(eng),
            interrupts=DecisionInterrupts(c, project_id=project), parallel_interrupts=True))
    assert result.status == "suspended"
    items = result.pending["interrupts"]
    assert [i["tool"] for i in items] == ["send_invoice", "refund"]
    assert len({i["id"] for i in items}) == 2 and all(i["decision_id"] for i in items)
    return items


def test_parallel_interrupts_resume_in_one_decision_batch_whatever_the_order(
        eng, project, tmp_path):
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService
    from boss_engine.services.interrupts import answers_for_run

    run_id, ledger = f"krun_{project}_par", tmp_path / "effects.log"
    items = _suspend_on_two(eng, project, run_id, ledger)
    assert ledger_lines(ledger) == [], "nothing ran before a person answered"
    with eng.begin() as c:
        rows = c.execute(text("SELECT id, interrupt_id, recommendation FROM decisions "
                              "WHERE id = ANY(:d)"), {"d": [i["decision_id"] for i in items]}
                         ).mappings().all()
    assert {r["interrupt_id"] for r in rows} == {i["id"] for i in items}
    assert all(r["recommendation"]["args_sha256"] for r in rows), "each bound to its arguments"

    def queued_resumes():
        with eng.begin() as c:
            return c.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p "
                                  "AND kind = 'agent.resume'"), {"p": project}).scalar()

    # Answered in the REVERSE order they were raised: the second (refund) first.
    service = DecisionService(get_settings())
    with eng.begin() as c:
        service.transition(c, items[1]["decision_id"], action="approve", actor="ceo", note=None)
    assert queued_resumes() == 0, "one answer of two releases nothing"
    with eng.begin() as c:
        service.transition(c, items[0]["decision_id"], action="approve", actor="ceo", note=None)
    assert queued_resumes() == 1, "the last answer queues ONE resume for the whole batch"

    with eng.begin() as c:
        answers = answers_for_run(c, PgCheckpointStore(c).load(run_id).pending)
        reordered = dict(reversed(list(answers.items())))
        from boss_engine.kernel.ledger import DbSink

        done = kernel.resume(_spec(project, run_id, [Say("both done")], _two_approvals(ledger),
                                   PgCheckpointStore(c), parallel_interrupts=True,
                                   sink=DbSink(eng)),
                             answers=reordered)
    assert done.status == "completed", done.error
    assert ledger_lines(ledger) == ["send_invoice:INV-1", "refund:R-9"], \
        "both ran once, in the order the model asked for them"
    with eng.begin() as c:
        held = c.execute(text(
            "SELECT name, status, attributes->>'interrupt_id' FROM trace_spans "
            "WHERE run_id = :r AND kind = 'interrupt' ORDER BY started_at"), {"r": run_id}).all()
    assert [(n, st) for n, st, _ in held] == [("approval send_invoice", "ok"),
                                               ("approval refund", "ok")], \
        "one interrupt span per interrupt(), each closed by the one resume"
    assert {i for _, _, i in held} == {i["id"] for i in items}


def test_an_argument_changed_after_approval_is_refused_and_nothing_runs(eng, project, tmp_path):
    from boss_engine.config import get_settings
    from boss_engine.services.argument_binding import CHANGED
    from boss_engine.services.decisions import DecisionService
    from boss_engine.services.interrupts import answers_for_run

    run_id, ledger = f"krun_{project}_bind", tmp_path / "effects.log"
    items = _suspend_on_two(eng, project, run_id, ledger)
    with eng.begin() as c:
        for item in items:
            DecisionService(get_settings()).transition(c, item["decision_id"], action="approve",
                                                       actor="ceo", note=None)
        # After the approvals, the held refund's amount is changed where the run keeps it.
        c.execute(text("""
            UPDATE agent_checkpoints SET state = jsonb_set(state,
                   '{pending,interrupts,1,args,x}', '"R-9000"')
             WHERE checkpoint_id = (SELECT checkpoint_id FROM agent_checkpoints
                                     WHERE run_id = :r ORDER BY seq DESC LIMIT 1)"""),
                  {"r": run_id})
    with eng.begin() as c:
        answers = answers_for_run(c, PgCheckpointStore(c).load(run_id).pending)
        denied = kernel.resume(_spec(project, run_id, [Say("must not be asked")],
                                     _two_approvals(ledger), PgCheckpointStore(c),
                                     parallel_interrupts=True), answers=answers)
    assert denied.status == "denied" and denied.error == CHANGED
    assert ledger_lines(ledger) == [], "the batch is released whole or not at all"


# ════ (d) double-texting ═══════════════════════════════════════════════════════════════════════

def _thread(eng, project, policy):
    from boss_engine.services import thread_runs

    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo", double_text=policy)
        first = thread_runs.admit(c, project_id=project, thread_id=tid,
                                  messages=[{"role": "user", "content": "first"}])
    return tid, first


def _runs(eng, tid):
    with eng.begin() as c:
        return {r["id"]: dict(r) for r in c.execute(text(
            "SELECT id, status, job_id FROM agent_runs WHERE thread_id = :t"), {"t": tid}
        ).mappings().all()}


def test_double_text_enqueue_waits_its_turn(eng, project):
    from boss_engine.services import thread_runs

    tid, first = _thread(eng, project, "enqueue")
    with eng.begin() as c:
        second = thread_runs.admit(c, project_id=project, thread_id=tid,
                                   messages=[{"role": "user", "content": "second"}])
    assert first["status"] == "queued" and first["job_id"]
    assert second["status"] == "pending" and second["job_id"] is None
    with eng.begin() as c:
        c.execute(text("UPDATE agent_runs SET status = 'completed' WHERE id = :r"),
                  {"r": first["run_id"]})
        started = thread_runs.finish(c, first["run_id"], "completed")
    runs = _runs(eng, tid)
    assert started and runs[second["run_id"]]["status"] == "queued"
    with eng.begin() as c:
        assert c.execute(text("SELECT active_run_id FROM agent_threads WHERE id = :t"),
                         {"t": tid}).scalar() == second["run_id"]


def test_double_text_reject_refuses_and_writes_nothing(eng, project):
    from boss_engine.services import thread_runs

    tid, first = _thread(eng, project, "reject")
    with pytest.raises(thread_runs.ThreadBusy):
        with eng.begin() as c:
            thread_runs.admit(c, project_id=project, thread_id=tid,
                              messages=[{"role": "user", "content": "second"}])
    assert list(_runs(eng, tid)) == [first["run_id"]]


@pytest.mark.parametrize("policy", ["interrupt", "rollback"])
def test_double_text_interrupt_and_rollback_stop_the_running_run_at_its_next_step(
        eng, project, tmp_path, monkeypatch, policy):
    from boss_engine.config import get_settings
    from boss_engine.services import agent_run_worker, thread_runs

    ledger, release = tmp_path / "effects.log", threading.Event()
    in_tool = threading.Event()
    seen: dict = {}

    def build(connection, settings, run):
        def slow_read(args: dict) -> dict:
            in_tool.set()
            release.wait(10)
            return {"ok": True}

        text_in = next(m["content"] for m in reversed(run["messages"]) if m["role"] == "user")
        seen[str(run["id"])] = [m.get("content") for m in run["messages"]]
        script = ([Call("read_a", {"x": "slow"}, id="r1"), Say("first finished")]
                  if text_in == "first" else [Say("second finished")])
        tools = toolset(ledger)
        tools._tools["read_a"] = Tool(ToolSchema("read_a", "slow read", OBJ), slow_read)
        return kernel.RunSpec(engine=ScriptedEngine(script), tools=tools, policies=policies(),
                              project_id="", system="agent", messages=[
                                  m for m in run["messages"] if m["role"] != "system"])

    monkeypatch.setitem(agent_run_worker.RUNNERS, "scripted_p2", agent_run_worker.kernel_runner(
        build))
    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo", double_text=policy)
        first = thread_runs.admit(c, project_id=project, thread_id=tid, messages=[
            {"role": "user", "content": "first"}], finalize_ctx={"runner": "scripted_p2"})

    def execute(admitted):
        from boss_engine.services.jobs import JobLeaseService

        with eng.begin() as c:
            leased = JobLeaseService(get_settings()).lease_owned_job(
                c, project_id=project, job_id=admitted["job_id"], worker_id="w",
                lease_ttl_seconds=60)
        return agent_run_worker.execute(eng, get_settings(), kind="agent.run",
                                        job_id=admitted["job_id"], run_id=admitted["run_id"],
                                        worker_id="w", lease_token=leased["lease_token"])

    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.update(execute(first)))
    worker.start()
    assert in_tool.wait(10), "the first run is inside its tool"
    with eng.begin() as c:
        second = thread_runs.admit(c, project_id=project, thread_id=tid, messages=[
            {"role": "user", "content": "second"}], finalize_ctx={"runner": "scripted_p2"})
    assert second["status"] == "pending", "the active run is still running its step"
    release.set()
    worker.join(20)
    assert outcome["status"] == "cancelled", outcome
    runs = _runs(eng, tid)
    assert runs[first["run_id"]]["status"] == ("rolled_back" if policy == "rollback"
                                                else "cancelled")
    assert runs[second["run_id"]]["status"] == "queued" and runs[second["run_id"]]["job_id"]
    done = execute({**second, "job_id": runs[second["run_id"]]["job_id"]})
    assert done["status"] == "completed"
    history = seen[second["run_id"]]
    if policy == "rollback":
        assert "first" not in history, "a rollback erases the stopped run from the thread"
    else:
        assert "first" in history, "an interrupt keeps the stopped run's progress"


# ════ (e) fork ═════════════════════════════════════════════════════════════════════════════════

def test_a_failed_run_forked_at_step_k_with_an_edited_result_completes(eng, project, tmp_path):
    ledger = tmp_path / "effects.log"
    run_id = f"krun_{project}_fail"
    failed = kernel.run(_spec(project, run_id, [Call("read_a", {"x": "a"}, id="k1"),
                                                Fail(RuntimeError("model fell over"))],
                              toolset(ledger), PgCheckpointStore(eng)))
    assert failed.status == "failed"
    store = PgCheckpointStore(eng)
    before = store.history(run_id)
    step_k = next(h for h in reversed(before) if h["step"] == 1 and h["phase"] == "")
    with eng.begin() as c:
        snapshot = c.execute(text("SELECT checkpoint_id, md5(state::text) FROM agent_checkpoints "
                                  "WHERE run_id = :r ORDER BY seq"), {"r": run_id}).all()

    branch = fork(store, step_k["checkpoint_id"], new_run_id=f"krun_{project}_branch",
                  tool_results={"k1": {"ok": True, "balance": 999}})
    done = kernel.resume(_spec(project, branch.run_id, [Say("the balance is 999")],
                               toolset(ledger), PgCheckpointStore(eng)))
    assert done.status == "completed" and "999" in done.text
    first = store.history(branch.run_id)[0]
    assert first["parent_checkpoint_id"] == step_k["checkpoint_id"] and first["source"] == "update"
    with eng.begin() as c:
        after = c.execute(text("SELECT checkpoint_id, md5(state::text) FROM agent_checkpoints "
                               "WHERE run_id = :r ORDER BY seq"), {"r": run_id}).all()
    assert after == snapshot, "the original run's checkpoints never change"
    assert count(ledger, "read_a") == 1, "the fork replays nothing: the edited result stands"


# ════ admission, lints and measurements ════════════════════════════════════════════════════════

def test_a_run_and_its_job_are_admitted_in_one_transaction(eng, project):
    from boss_engine.services import thread_runs

    run_id = None
    with pytest.raises(RuntimeError):
        with eng.begin() as c:
            tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo")
            run_id = thread_runs.admit(c, project_id=project, thread_id=tid, messages=[
                {"role": "user", "content": "x"}])["run_id"]
            raise RuntimeError("the request died after admission")
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM agent_runs WHERE id = :r"),
                         {"r": run_id}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM jobs WHERE payload->>'run_id' = :r"),
                         {"r": run_id}).scalar() == 0
    with eng.begin() as c:
        tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo")
        ok = thread_runs.admit(c, project_id=project, thread_id=tid, messages=[
            {"role": "user", "content": "x"}])
    with eng.begin() as c:
        job = c.execute(text("SELECT kind, payload->>'run_id' FROM jobs WHERE id = :j"),
                        {"j": ok["job_id"]}).one()
    assert tuple(job) == ("agent.run", ok["run_id"])


def test_every_write_tool_declares_an_idempotency_class():
    from boss_engine.kernel.idempotency import CLASSES
    from boss_engine.services.agent_loop import native_tool_registry
    from boss_engine.tools.builtin import build_default_registry

    missing = sorted(s.name for registry in (build_default_registry(), native_tool_registry())
                     for s in registry.specs() if s.kind == "write"
                     and s.idempotency not in CLASSES)
    assert not missing, ("declare idempotent | keyed | irreversible for these write tools "
                         f"(kernel/idempotency.py TOOL_IDEMPOTENCY or idempotency=): {missing}")


def test_every_production_kernel_run_is_given_a_checkpoint_store():
    """A suspension must never live only in the memory of the process that ran it: every
    RunSpec built outside the kernel names its `store` (kernel.durable.store_for)."""
    import ast

    offenders = []
    for path in sorted((ROOT / "engine" / "boss_engine").rglob("*.py")):
        if "/kernel/" in str(path):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr",
                                                      getattr(node.func, "id", "")) == "RunSpec":
                if not any(k.arg == "store" for k in node.keywords):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"kernel.RunSpec without store=: {offenders}"


def test_state_blob_size_and_write_latency_per_durability_mode(eng, capsys):
    from boss_engine.kernel.state import RunState

    state = RunState(run_id="krun_measure", project_id="", lineage_id="krun_measure", system=[],
                     messages=[{"role": "user", "content": "x" * 400}]
                     + [{"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 600}
                        for i in range(12)])
    numbers = measure(eng, state, steps=20)
    with capsys.disabled():
        print("\nmode   state_bytes  checkpoints  loop_ms/step  write_ms_p50")
        for mode, row in numbers.items():
            print(f"{mode:6} {row['state_bytes']:11} {row['checkpoints_written']:12} "
                  f"{row['loop_ms_per_step']:13} {row['write_ms_p50']:13}")
    assert numbers["sync"]["checkpoints_written"] == 21
    assert numbers["exit"]["checkpoints_written"] == 1, "exit writes only the final state"
    assert all(row["state_bytes"] > 7_000 for row in numbers.values())
    print(json.dumps(numbers))


# ════ a workflow agent step's run, admitted in the tick and executed on a worker ══════════════

def test_an_agent_step_run_is_admitted_and_executed_on_a_worker_not_in_the_api(
        eng, project, monkeypatch):
    """agent_loop's run (the workflow agent step) on the worker path end to end: admitted with
    its job on the step's thread, executed by agent_run_worker in this process on the kernel
    with the Postgres checkpointer, the agent_runs row the workflow awaits completed."""
    from boss_engine.config import get_settings
    from boss_engine.services import agent_loop, agent_run_worker
    from boss_engine.services.jobs import JobLeaseService

    model = ScriptedEngine([Say('{"outcome": "completed", "summary": "trimmed"}')])
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *a, **k: model)
    monkeypatch.setattr(agent_loop, "governed", lambda raw, *a, **k: raw)
    with eng.begin() as c:
        admitted = agent_loop.admit_agent_run(
            c, get_settings(), project, "You are the video editor.", "WHAT I WANT DONE\ntrim it",
            max_steps=4, thread_ref="workflow_step:wfr_1:edit",
            finalize_ctx={"agent_step": {"role_key": "video_editor", "skill_ids": [],
                                         "mcp_server_ids": [], "actor": "workflow"}})
    assert admitted["status"] == "queued" and admitted["job_id"]
    run_id = admitted["run_id"]
    with eng.begin() as c:
        row = c.execute(text("SELECT status, thread_id, finalize_ctx FROM agent_runs "
                             "WHERE id = :r"), {"r": run_id}).mappings().one()
        assert row["status"] == "queued" and row["thread_id"]
        assert row["finalize_ctx"]["executes_on"] == "worker"
        assert model.calls == 0, "admission drives nothing: the API never runs the agent"
        leased = JobLeaseService(get_settings()).lease_owned_job(
            c, project_id=project, job_id=admitted["job_id"], worker_id="worker-1",
            lease_ttl_seconds=60)
    out = agent_run_worker.execute(eng, get_settings(), kind="agent.run",
                                   job_id=admitted["job_id"], run_id=run_id,
                                   worker_id="worker-1", lease_token=leased["lease_token"])
    assert out["status"] == "completed", out
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                         {"r": run_id}).scalar() == "completed"
        assert c.execute(text("SELECT count(*) FROM agent_checkpoints WHERE run_id = :r"),
                         {"r": run_id}).scalar() >= 2, "checkpointed in Postgres"
        assert c.execute(text("SELECT run_state FROM agent_threads WHERE id = :t"),
                         {"t": row["thread_id"]}).scalar() == "idle"
    assert model.calls == 1
