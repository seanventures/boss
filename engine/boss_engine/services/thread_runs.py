"""THREADS AND ADMISSION: one active run per thread, a run admitted with its job in one
transaction, and what a second message does to a busy thread (AP-P2, BOSSAPP-1053;
docs/design/AGENT_PLATFORM.md §4.3 "Admission is atomic", "Double-texting and concurrency").

A THREAD (agent_threads, surface 'agent') is where an agent's runs happen one after another: a
workflow agent step's own thread, a deployed agent's conversation. Its row holds the run state
(idle | busy | interrupted | error), the run holding it (`active_run_id`) and its double-text
policy. A run on a thread is an agent_runs row with `thread_id`.

ADMISSION IS ATOMIC. `admit` writes the run row and its `agent.run` job in the caller's
transaction, so there is never a run nobody will execute nor a job for a run that does not
exist ("admission is not durable" is the gap LangGraph's docs admit). The run then EXECUTES ON A
WORKER (services/agent_run_worker.py), never inside an HTTP request.

DOUBLE-TEXTING. A new input on a busy thread follows the thread's policy (or the caller's):

  enqueue    (default) the new run waits its turn: admitted `pending`, with no job, and
             `release` gives it its job the moment the active run ends.
  reject     refused (ThreadBusy); nothing is written.
  interrupt  the active run is asked to stop at its next step boundary (the kernel's cancel
             hook, through `stop_requested`); its progress stays on the thread, and the new run
             starts when it has stopped.
  rollback   as interrupt, but the active run is erased from the thread's history
             (`rolled_back`): the new run starts from the thread as it was before it.

A run's conversation is built when it STARTS (thread_history + its own input), so a queued run
sees what the run before it produced.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

ENQUEUE, REJECT, INTERRUPT, ROLLBACK = "enqueue", "reject", "interrupt", "rollback"
POLICIES = (ENQUEUE, REJECT, INTERRUPT, ROLLBACK)

#: A run in one of these holds its thread.
ACTIVE = ("queued", "running", "awaiting_approval", "drained", "budget_wait")
#: A run in one of these has let go of it.
ENDED = ("completed", "failed", "cancelled", "rolled_back", "denied")

JOB_KIND = "agent.run"


class ThreadBusy(RuntimeError):
    """A new input on a busy thread whose policy is `reject`."""

    def __init__(self, thread_id: str, active_run_id: Optional[str]):
        super().__init__(f"thread {thread_id} is busy with {active_run_id}; its double-text "
                         "policy is reject")
        self.thread_id, self.active_run_id = thread_id, active_run_id


def ensure_thread(connection: Connection, *, project_id: str, actor: str,
                  external_ref: Optional[str] = None, agent_id: Optional[str] = None,
                  double_text: str = ENQUEUE, thread_id: Optional[str] = None) -> str:
    """The thread for (project, external_ref, agent) — created on first use. A workflow agent
    step's thread is bound to the step (`workflow_step:<run>:<step>`)."""
    if double_text not in POLICIES:
        raise ValueError(f"double_text {double_text!r} is not one of {POLICIES}")
    role = str(agent_id or "")
    if external_ref:
        found = connection.execute(text(
            "SELECT id FROM agent_threads WHERE project_id = :p AND surface = 'agent' "
            "AND external_ref = :r AND agent_role = :a"),
            {"p": project_id, "r": external_ref, "a": role}).scalar()
        if found:
            return str(found)
    new_id = thread_id or make_id("thr")
    connection.execute(text("""
        INSERT INTO agent_threads (id, project_id, actor, surface, external_ref, agent_role,
                                   agent_id, double_text, run_state)
        VALUES (:id, :p, :actor, 'agent', :ref, :role, :agent, :dt, 'idle')
        ON CONFLICT DO NOTHING"""),
        {"id": new_id, "p": project_id, "actor": actor, "ref": external_ref, "role": role,
         "agent": agent_id, "dt": double_text})
    if external_ref:
        return str(connection.execute(text(
            "SELECT id FROM agent_threads WHERE project_id = :p AND surface = 'agent' "
            "AND external_ref = :r AND agent_role = :a"),
            {"p": project_id, "r": external_ref, "a": role}).scalar())
    return new_id


def _thread(connection: Connection, thread_id: str, *, lock: bool = False) -> dict:
    row = connection.execute(text(
        "SELECT id, project_id, run_state, active_run_id, double_text, metadata "
        "FROM agent_threads WHERE id = :t" + (" FOR UPDATE" if lock else "")),
        {"t": thread_id}).mappings().first()
    if row is None:
        raise LookupError(f"no thread {thread_id}")
    out = dict(row)
    out["metadata"] = out["metadata"] if isinstance(out["metadata"], dict) \
        else json.loads(out["metadata"] or "{}")
    return out


def _run_status(connection: Connection, run_id: Optional[str]) -> Optional[str]:
    if not run_id:
        return None
    return connection.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                              {"r": run_id}).scalar()


def admit(connection: Connection, *, project_id: str, thread_id: str, messages: list,
          finalize_ctx: Optional[dict] = None, task_id: Optional[str] = None,
          policy: Optional[str] = None, trigger: str = "api",
          agent_version_hash: Optional[str] = None, run_id: Optional[str] = None,
          priority: Optional[int] = None, parent_run_id: Optional[str] = None) -> dict:
    """Admit one run on `thread_id`, in the caller's transaction. Returns {"run_id", "status",
    "job_id", "policy"}: `queued` with its job when the thread was idle, `pending` behind the
    active run otherwise (raises ThreadBusy under `reject`)."""
    from .agent_loop import KERNEL_LOOP, _save_run, _zero_usage
    from .jobs import JOB_PRIORITY_REQUESTED, enqueue_job

    thread = _thread(connection, thread_id, lock=True)
    chosen = policy or thread["double_text"] or ENQUEUE
    if chosen not in POLICIES:
        raise ValueError(f"double_text {chosen!r} is not one of {POLICIES}")
    active = thread["active_run_id"]
    active_status = _run_status(connection, active)
    busy = bool(active) and active_status in ACTIVE
    if busy and chosen == REJECT:
        raise ThreadBusy(thread_id, active)
    run_id = run_id or make_id("arun")
    fctx = {**(finalize_ctx or {}), "loop": KERNEL_LOOP, "executes_on": "worker",
            "thread_id": thread_id}
    # The run row first, then (when it may start) its job — the same transaction either way.
    _save_run(connection, run_id, project_id, task_id, "pending" if busy else "queued",
              [dict(m) for m in messages], [], _zero_usage(), fctx, step=0)
    connection.execute(text("""
        UPDATE agent_runs SET thread_id = :t, trigger = :trigger, double_text = :dt,
               agent_version_hash = :v, parent_run_id = :parent, input = CAST(:input AS JSONB)
        WHERE id = :r"""), {"t": thread_id, "trigger": trigger, "dt": chosen,
                             "v": agent_version_hash, "parent": parent_run_id, "r": run_id,
                             "input": json.dumps(messages, default=str)})
    job_id = None
    if busy:
        if chosen in (INTERRUPT, ROLLBACK):
            _stop_active(connection, thread, active, active_status, rollback=chosen == ROLLBACK)
    else:
        job_id = _start(connection, thread_id, run_id, project_id, task_id,
                        priority=priority if priority is not None else JOB_PRIORITY_REQUESTED,
                        enqueue_job=enqueue_job)
    emit_event(connection, project_id, "agent.run_admitted", "boss", "agent_run", run_id,
               {"thread_id": thread_id, "policy": chosen, "busy": busy, "job_id": job_id,
                "trigger": trigger})
    if busy and chosen in (INTERRUPT, ROLLBACK):
        # An active run with no worker on it (waiting on a person, or queued) stopped at once:
        # the thread is free now.
        if _run_status(connection, active) in ENDED:
            job_id = release(connection, thread_id) or job_id
    return {"run_id": run_id, "status": "pending" if busy and not job_id else "queued",
            "job_id": job_id, "policy": chosen, "thread_id": thread_id}


def _start(connection: Connection, thread_id: str, run_id: str, project_id: str,
           task_id: Optional[str], *, priority: int, enqueue_job: Any) -> str:
    job = enqueue_job(connection, project_id, JOB_KIND, {"run_id": run_id, "thread_id": thread_id},
                      task_id=task_id, priority=priority, max_attempts=3,
                      dedupe_key=f"{JOB_KIND}:{run_id}")
    job_id = str(job.get("id") or "")
    connection.execute(text("""
        UPDATE agent_runs SET status = 'queued', job_id = :j,
               finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb)
                              || jsonb_build_object('created_by_job_id', CAST(:j AS TEXT))
        WHERE id = :r"""), {"j": job_id, "r": run_id})
    connection.execute(text("""
        UPDATE agent_threads SET run_state = 'busy', active_run_id = :r, updated_at = now()
        WHERE id = :t"""), {"r": run_id, "t": thread_id})
    return job_id


def _stop_active(connection: Connection, thread: dict, active: str, status: Optional[str], *,
                 rollback: bool) -> None:
    """Ask the active run to stop. A run a worker is executing stops at its next step boundary
    (agent_run_worker's should_stop reads `stop_run`); one that waits on a person or for its
    worker is stopped here and now."""
    meta = {**thread["metadata"], "stop_run": active, "rollback_run": active if rollback else None}
    connection.execute(text(
        "UPDATE agent_threads SET metadata = CAST(:m AS JSONB), updated_at = now() WHERE id = :t"),
        {"m": json.dumps(meta), "t": thread["id"]})
    if status in ("awaiting_approval", "queued", "drained", "budget_wait"):
        finish(connection, active, "rolled_back" if rollback else "cancelled")
        connection.execute(text(
            "UPDATE jobs SET status = 'cancelled', updated_at = now() WHERE kind = :k "
            "AND payload->>'run_id' = :r AND status IN ('queued', 'failed_retryable')"),
            {"k": JOB_KIND, "r": active})


def stop_requested(connection: Connection, run_id: str) -> bool:
    """The kernel's cancel hook for a run on a thread: True once a double-text interrupt or
    rollback asked this run to stop."""
    row = connection.execute(text(
        "SELECT t.metadata->>'stop_run' FROM agent_runs r JOIN agent_threads t "
        "ON t.id = r.thread_id WHERE r.id = :r"), {"r": run_id}).scalar()
    return bool(row) and row == run_id


def finish(connection: Connection, run_id: str, status: Optional[str] = None) -> Optional[str]:
    """A run on a thread has ended (or stopped). Records a final status when given — a run a
    rollback stopped ends `rolled_back` — and frees the thread for the next run in line.
    Returns the next run's job id, if one started."""
    row = connection.execute(text(
        "SELECT thread_id, status FROM agent_runs WHERE id = :r"), {"r": run_id}
    ).mappings().first()
    if row is None or not row["thread_id"]:
        return None
    thread = _thread(connection, row["thread_id"], lock=True)
    final = status or row["status"]
    if thread["metadata"].get("rollback_run") == run_id and final in ("cancelled", "completed",
                                                                       "failed"):
        final = "rolled_back"
    if final != row["status"]:
        connection.execute(text(
            "UPDATE agent_runs SET status = :s, updated_at = now() WHERE id = :r"),
            {"s": final, "r": run_id})
    if final not in ENDED:
        state = {"awaiting_approval": "interrupted", "failed": "error"}.get(final, "busy")
        connection.execute(text(
            "UPDATE agent_threads SET run_state = :s, updated_at = now() WHERE id = :t"),
            {"s": state, "t": thread["id"]})
        return None
    if thread["active_run_id"] != run_id:
        return None
    meta = {k: v for k, v in thread["metadata"].items()
            if not (k in ("stop_run", "rollback_run") and v == run_id)}
    connection.execute(text("""
        UPDATE agent_threads SET run_state = :s, active_run_id = NULL,
               metadata = CAST(:m AS JSONB), updated_at = now() WHERE id = :t"""),
        {"s": "error" if final == "failed" else "idle", "m": json.dumps(meta), "t": thread["id"]})
    return release(connection, thread["id"])


def release(connection: Connection, thread_id: str) -> Optional[str]:
    """The next run waiting on a free thread gets its job (the oldest `pending` one)."""
    from .jobs import JOB_PRIORITY_REQUESTED, enqueue_job

    thread = _thread(connection, thread_id, lock=True)
    if thread["active_run_id"] and _run_status(connection, thread["active_run_id"]) in ACTIVE:
        return None
    nxt = connection.execute(text(
        "SELECT id, project_id, task_id FROM agent_runs WHERE thread_id = :t "
        "AND status = 'pending' ORDER BY created_at, id LIMIT 1"), {"t": thread_id}
    ).mappings().first()
    if nxt is None:
        connection.execute(text(
            "UPDATE agent_threads SET active_run_id = NULL, updated_at = now() WHERE id = :t "
            "AND active_run_id IS NOT NULL"), {"t": thread_id})
        return None
    return _start(connection, thread_id, nxt["id"], nxt["project_id"], nxt["task_id"],
                  priority=JOB_PRIORITY_REQUESTED, enqueue_job=enqueue_job)


def thread_history(connection: Connection, thread_id: str, *, before_run: str) -> list:
    """The conversation a new run on this thread starts from: the final messages of the last
    run that ended on it (never one a rollback erased, never the new run itself), without its
    system message. Empty for a thread's first run."""
    row = connection.execute(text("""
        SELECT messages FROM agent_runs WHERE thread_id = :t AND id <> :r
           AND status IN ('completed', 'cancelled', 'failed')
           AND created_at <= (SELECT created_at FROM agent_runs WHERE id = :r)
         ORDER BY created_at DESC, id DESC LIMIT 1"""), {"t": thread_id, "r": before_run}).first()
    if row is None:
        return []
    messages = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
    return [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]


__all__ = ["ACTIVE", "ENDED", "ENQUEUE", "INTERRUPT", "JOB_KIND", "POLICIES", "REJECT",
           "ROLLBACK", "ThreadBusy", "admit", "ensure_thread", "finish", "release",
           "stop_requested", "thread_history"]
