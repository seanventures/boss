"""AGENT RUNS EXECUTE ON WORKERS, NOT IN THE API PROCESS (AP-P2, BOSSAPP-1053; design §4.3 "Out
of the API process", decision D5).

A workflow agent step used to run its whole agent loop inside the API's tick request: a deploy
killed it mid-turn, and a step idle past the database's idle-in-transaction limit rolled back
with everything it had done. Now the step ADMITS a run (thread_runs.admit: the run row and its
`agent.run` job in one transaction) and waits on it (`await: agent_run`), and a worker that
leases the job executes it HERE, in the worker's own process, against the database directly:

  * the run's connection commits at every checkpoint (kernel/durable.py, durability `sync`), so
    what a step did is never rolled back by what a later step did;
  * every write is FENCED by the job's lease (jobs.require_active_lease): a worker whose lease
    was reclaimed cannot write over the worker that now holds it;
  * a deploy DRAINS it: the worker's SIGTERM sets the drain flag, the kernel stops at the next
    step boundary with a checkpoint, and the job goes back to the queue without spending an
    attempt — the next worker resumes from that checkpoint;
  * a crash (kill -9) loses at most the call in flight: the lease expires, the job is leased
    again, and the run resumes from its last checkpoint, replaying the tool results it had
    written and parking an irreversible call that may have happened (kernel/idempotency.py);
  * a double-text interrupt stops it at its next step boundary (thread_runs.stop_requested).

`agent.resume` (a person answered what the run waits on) executes here too for a run admitted
on a worker; a run from before AP-P2 keeps resuming where it always did (the API).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

#: The job kinds this module executes.
KINDS = ("agent.run", "agent.resume")


def on_worker(connection: Any, run_id: str) -> bool:
    """Whether this run was admitted to execute on a worker (finalize_ctx.executes_on)."""
    value = connection.execute(text(
        "SELECT finalize_ctx->>'executes_on' FROM agent_runs WHERE id = :r"), {"r": run_id}
    ).scalar()
    return value == "worker"


class Lease:
    """The lease this execution holds: checked (FOR UPDATE) before every checkpoint write."""

    def __init__(self, jobs: Any, *, worker_id: str, job_id: str, lease_token: str):
        self.jobs, self.worker_id, self.job_id, self.lease_token = (jobs, worker_id, job_id,
                                                                    lease_token)

    def fence(self, connection: Any) -> None:
        self.jobs.require_active_lease(connection, self.worker_id, self.job_id, self.lease_token)


def execute(engine: Any, settings: Any, *, kind: str, job_id: str, run_id: str, worker_id: str,
            lease_token: str, drain: Optional[Callable[[], bool]] = None,
            handed_back: Optional[Callable[[], bool]] = None,
            jobs: Any = None) -> dict:
    """Execute one `agent.run` / `agent.resume` job in this process. Returns the job's result;
    `{"job_deferred": True}` when the job was handed back to the queue (a drain, a budget wait,
    an unknown outcome parked) and must not be completed."""
    from . import agent_loop, thread_runs
    from .jobs import JobLeaseService, LeaseNotFoundError

    jobs = jobs or JobLeaseService(settings)
    lease = Lease(jobs, worker_id=worker_id, job_id=job_id, lease_token=lease_token)
    with engine.connect() as connection:
        run = agent_loop.load_agent_run(connection, run_id)
        if run is None:
            return {"status": "failed", "message": f"no agent run {run_id}"}
        status = str(run["status"] or "")
        if status in thread_runs.ENDED or (kind == "agent.run" and status in (
                "awaiting_approval", "pending")):
            # Already settled (a re-delivered job), or waiting on a person / behind another
            # run: nothing to execute under this job.
            connection.commit()
            return {"status": status, "run_id": run_id, "noop": True}
        stop = (lambda: _stop_requested(engine, run_id)) if (
            run["finalize_ctx"] or {}).get("thread_id") else None
        token = agent_loop._DRIVE.set({"commit": True, "fence": lease.fence, "drain": drain,
                                       "should_stop": stop})
        try:
            outcome = _drive(connection, settings, run, kind=kind)
        except LeaseNotFoundError:
            # FENCED: another worker holds this run's lease now. Nothing of ours is committed
            # past the last checkpoint we were entitled to; the holder goes on from there.
            connection.rollback()
            raise
        except Exception:
            # A fatal model failure: the run is recorded failed (the old contract) and the job
            # fails with it; the thread moves on.
            connection.rollback()
            with engine.begin() as c:
                if (c.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                              {"r": run_id}).scalar() or "") in ("running", "queued"):
                    c.execute(text("UPDATE agent_runs SET status = 'failed', updated_at = now() "
                                   "WHERE id = :r"), {"r": run_id})
                thread_runs.finish(c, run_id, "failed")
            raise
        finally:
            agent_loop._DRIVE.reset(token)
        result = _settle(connection, jobs, lease, run_id, outcome, handed_back=handed_back)
        connection.commit()
        return result


def _stop_requested(engine: Any, run_id: str) -> bool:
    from . import thread_runs

    with engine.connect() as c:
        return thread_runs.stop_requested(c, run_id)


def _drive(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    """The run, driven: started (its thread's history first), then handed to its runner —
    agent_loop's (a workflow agent step) unless the run names another."""
    from . import agent_loop, thread_runs

    run_id = str(run["id"])
    fctx = dict(run["finalize_ctx"] or {})
    if run["status"] == "queued":
        messages = list(run["messages"] or [])
        thread_id = fctx.get("thread_id")
        if thread_id:
            history = thread_runs.thread_history(connection, thread_id, before_run=run_id)
            if history:
                head = [m for m in messages[:1] if m.get("role") == "system"]
                messages = head + history + messages[len(head):]
        agent_loop._save_run(connection, run_id, run["project_id"], run["task_id"], "running",
                             messages, [], agent_loop._zero_usage(), fctx, step=0)
        thread_runs.finish(connection, run_id)  # thread: busy (not ended)
        connection.commit()
        run = agent_loop.load_agent_run(connection, run_id) or run
    runner = RUNNERS.get(str(fctx.get("runner") or "agent_loop"))
    if runner is None:
        raise RuntimeError(f"no runner {fctx.get('runner')!r} for agent run {run_id}")
    return runner(connection, settings, run, kind=kind)


def _agent_loop_runner(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
    """A workflow agent step's run: agent_loop on the kernel, continued or resumed, under the
    authority its job carries (the person the workflow runs for, through this agent)."""
    from boss_engine.identity import catalogue as _identity_catalogue
    from boss_engine.identity import runs as _identity_runs

    from . import agent_loop
    from .connection_credentials import acting_agent

    run_id = str(run["id"])
    fctx = dict(run["finalize_ctx"] or {})
    role = (fctx.get("agent_step") or {}).get("role_key") or fctx.get("role_key") or "atlas"
    with _identity_runs.delegated(connection.engine, kind="ag", entity_id=run_id,
                                  project_id=run["project_id"], role_key=role,
                                  scope=_identity_catalogue.delegable(),
                                  job_id=fctx.get("created_by_job_id")), acting_agent(role):
        if kind == "agent.resume" and run["status"] == "awaiting_approval":
            return agent_loop.resume_agent_run(connection, settings, run_id)
        return agent_loop._drive_run(connection, settings, run_id,
                                     int(fctx["max_steps"]) if fctx.get("max_steps") else None)


class AgentRunRow:
    """A kernel run's agent_runs row kept in step with its checkpoints (the mirror a
    PgCheckpointStore saves beside each checkpoint): what the console, the workflow's await and
    the thread read."""

    def __init__(self, connection: Any):
        self.connection = connection

    def save(self, state: Any) -> None:
        import json as _json

        safe = lambda value: _json.dumps(value, default=str)  # noqa: E731
        self.connection.execute(text("""
            UPDATE agent_runs SET status = :s, messages = CAST(:m AS JSONB), step = :step,
                   pending = CAST(:p AS JSONB), output_text = :o, usage = CAST(:u AS JSONB),
                   updated_at = now() WHERE id = :r"""),
            {"s": state.status, "m": safe(state.openai_messages()), "step": int(state.step),
             "p": safe(state.pending) if state.pending else None, "o": state.final_text,
             "u": safe(state.usage.as_adapter_usage()), "r": state.run_id})

    def load(self, run_id: str) -> Any:
        return None


def kernel_runner(build: Callable[[Any, Any, dict], Any]) -> Callable[..., dict]:
    """A runner for any kernel agent: `build(connection, settings, run) -> RunSpec` (engine,
    tools, policies, system and the run's messages); this drives it on a worker — durable
    (`sync` checkpoints with pending writes, committed and fenced), drained, stoppable by a
    double-text interrupt — and answers in agent_loop's outcome shape. Phase 3's deployed agents
    register here; the tests' scripted agents do."""

    def run_it(connection: Any, settings: Any, run: dict, *, kind: str) -> dict:
        import dataclasses

        from .. import kernel
        from ..kernel.durable import PgCheckpointStore
        from .interrupts import DecisionInterrupts, answers_for_run

        opts = drive_options()
        fctx = dict(run.get("finalize_ctx") or {})
        store = PgCheckpointStore(connection, thread_id=fctx.get("thread_id"), commit=True,
                                  fence=opts.get("fence"), mirror=AgentRunRow(connection))
        spec = dataclasses.replace(
            build(connection, settings, run), run_id=str(run["id"]), project_id=str(
                run["project_id"]), persist=True, store=store, thread_id=fctx.get("thread_id"),
            drain=opts.get("drain"),
            interrupts=DecisionInterrupts(connection, project_id=str(run["project_id"]),
                                          task_id=run.get("task_id")),
            cancel=kernel.CancelToken(should_stop=opts["should_stop"])
            if opts.get("should_stop") else None)
        if store.load(str(run["id"])) is None:
            result = kernel.run(spec)
        else:
            pending = connection.execute(text(
                "SELECT state->'pending' FROM agent_checkpoints WHERE run_id = :r "
                "ORDER BY seq DESC LIMIT 1"), {"r": run["id"]}).scalar()
            answers = answers_for_run(connection, pending) if pending else {}
            result = kernel.resume(spec, answers=answers or None)
        return {"status": result.status, "run_id": result.run_id, "output_text": result.text,
                "error": result.error, "stopped": result.stopped}

    return run_it


def drive_options() -> dict:
    """How the current drive is hosted (commit, fence, drain, should_stop)."""
    from .agent_loop import _DRIVE

    return dict(_DRIVE.get() or {})


#: Runners by finalize_ctx["runner"]: how a run of that kind is driven on a worker.
RUNNERS: dict = {"agent_loop": _agent_loop_runner}


def _settle(connection: Any, jobs: Any, lease: Lease, run_id: str, outcome: dict, *,
            handed_back: Optional[Callable[[], bool]]) -> dict:
    """What the job and the thread do with how the drive ended."""
    from . import thread_runs

    status = str(outcome.get("status") or "")
    if status in ("denied", "not_found"):
        # A resume the decision did not release: the run still waits; nothing changes.
        return {"status": status, "run_id": run_id, "reason": outcome.get("reason")}
    if status == "drained":
        thread_runs.finish(connection, run_id, "drained")
        if handed_back is None or not handed_back():
            # A DEPLOY IS NOT THE JOB'S FAILURE: back to the queue, the attempt given back.
            jobs.defer_leased(connection, lease.worker_id, lease.job_id, lease.lease_token,
                              delay_seconds=2, reason="interrupted_by_deploy",
                              message="BOSS restarted for an update; the agent run resumes "
                                      "from its last checkpoint")
        return {"job_deferred": True, "status": "drained", "run_id": run_id}
    if status == "budget_wait":
        thread_runs.finish(connection, run_id, "budget_wait")
        jobs.defer_leased(connection, lease.worker_id, lease.job_id, lease.lease_token,
                          delay_seconds=int(outcome.get("retry_after_seconds") or 30),
                          reason="budget_wait", message=str(outcome.get("message") or ""))
        return {"job_deferred": True, "status": status, "run_id": run_id}
    if status == "suspended":
        pending = connection.execute(text("SELECT pending FROM agent_runs WHERE id = :r"),
                                     {"r": run_id}).scalar() or {}
        thread_runs.finish(connection, run_id, "awaiting_approval")
        if isinstance(pending, dict) and pending.get("parked") == "unknown_outcome":
            # AN IRREVERSIBLE CALL THAT MAY HAVE HAPPENED: the job parks as unknown-outcome
            # (never blind-retried); the run waits on the person's answer to its interrupt.
            from .durable_operations import park_job_unknown_outcome

            row = connection.execute(text("SELECT * FROM jobs WHERE id = :j"),
                                     {"j": lease.job_id}).mappings().first()
            if row is not None:
                park_job_unknown_outcome(connection, dict(row), {"code": "unknown_outcome"},
                                         actor=lease.worker_id,
                                         cause=f"{pending.get('tool')} may have run before "
                                               "the worker stopped")
            return {"job_deferred": True, "status": "unknown_outcome", "run_id": run_id}
        return {"status": "suspended", "run_id": run_id,
                "decision_id": outcome.get("decision_id")}
    thread_runs.finish(connection, run_id, status if status in thread_runs.ENDED else None)
    return {"status": status or "completed", "run_id": run_id,
            "output_text": str(outcome.get("output_text") or "")[:2000]}


__all__ = ["AgentRunRow", "KINDS", "Lease", "RUNNERS", "drive_options", "execute",
           "kernel_runner", "on_worker"]
