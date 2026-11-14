"""SUB-AGENTS ON BOSS'S OWN TABLES (AP-P7, BOSSAPP-1058): the database half of
kernel/subagents.py.

  DbBranches         the single-writer registry (agent_branch_writers, D12): a live run holds the
                     branch it writes; a writer sub-agent on a held branch is refused at spawn.
  OboExchange        a child's EXCHANGED RUN TOKEN: inside a delegated run, the parent's shim
                     performs the RFC 8693 exchange (identity/runs.spawn_child) for the child's
                     Passport, with a NARROWER scope (a read-only child: the parent's `:read`
                     permissions only) under the nested run id `<parent run>/ag:<child run>`. The
                     child's tool calls are then decided against its own token, and revoking the
                     parent's Passport or grant revokes the child's token and halts its agent run
                     in the same transaction (identity/cascade.py, the `ag` halter).
  DbAsyncSubagents   ASYNC sub-agents as durable runs on workers (AP-P2): `start` admits the
                     child run on its own thread with its job in one transaction; check / update /
                     cancel / list answer A2A v1.0 Tasks (protocols/a2a_types: id = the child run,
                     contextId = its thread, INPUT_REQUIRED = an open interrupt whose status
                     message carries {decision_id, interrupt_id}).
  notify_parent      EVENT-DRIVEN NOTIFICATION (never polled): thread_runs.finish calls it as a
                     child run ends or stops for a person; the Task lands in
                     agent_subagent_notifications for the parent, which reads it as a message at
                     its next step — or, when the parent has already ended, its thread is woken
                     with it (a new run admitted on the parent's thread).
  middleware_for     what an agent run's published spec asks for (agent_loop wires it): the
                     subagents middleware with these ports, write_todos and the rubric, and a
                     child's own budget slice.
"""
from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

log = logging.getLogger(__name__)

TRIGGER = "subagent"
WAKE_TRIGGER = "subagent_notify"
#: A notification's report, at most (the parent reads the rest with check_subagent).
REPORT_CHARS = 6000


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── The single writer ─────────────────────────────────────────────────────────────────────────

class DbBranches:
    """agent_branch_writers on the run's connection. A holder whose run has ended (or never
    existed as a live run here) does not hold the branch."""

    LIVE = ("queued", "running", "awaiting_approval", "drained", "budget_wait", "pending")

    def __init__(self, connection: Connection, project_id: str,
                 live_in_process: Optional[set] = None):
        self.connection, self.project_id = connection, project_id
        #: Sync children in this process have no agent_runs row: they are live while listed.
        self.live_in_process = live_in_process if live_in_process is not None else set()

    def _live(self, run_id: str) -> bool:
        if run_id in self.live_in_process:
            return True
        status = self.connection.execute(text(
            "SELECT status FROM agent_runs WHERE id = :r"), {"r": run_id}).scalar()
        if status is None:
            # A kernel run with no agent_runs row (a sync child, a scripted run): its checkpoint
            # says whether it is still going.
            status = self.connection.execute(text(
                "SELECT status FROM agent_checkpoints WHERE run_id = :r ORDER BY seq DESC "
                "LIMIT 1"), {"r": run_id}).scalar()
        return status in self.LIVE

    def holder(self, branch: str) -> Optional[str]:
        run_id = self.connection.execute(text(
            "SELECT run_id FROM agent_branch_writers WHERE project_id = :p AND branch = :b"),
            {"p": self.project_id, "b": branch}).scalar()
        return str(run_id) if run_id and self._live(str(run_id)) else None

    def claim(self, branch: str, run_id: str) -> str:
        holder = self.holder(branch)
        if holder and holder != run_id:
            return f"branch {branch!r} is being written by run {holder}"
        self.connection.execute(text("""
            INSERT INTO agent_branch_writers (project_id, branch, run_id)
            VALUES (:p, :b, :r)
            ON CONFLICT (project_id, branch) DO UPDATE SET run_id = EXCLUDED.run_id,
                   acquired_at = now()"""), {"p": self.project_id, "b": branch, "r": run_id})
        if self.connection.execute(text("SELECT 1 FROM agent_runs WHERE id = :r"),
                                   {"r": run_id}).scalar() is None:
            self.live_in_process.add(run_id)  # a sync child: live until it lets go
        return ""

    def release(self, run_id: str) -> None:
        self.live_in_process.discard(run_id)
        self.connection.execute(text(
            "DELETE FROM agent_branch_writers WHERE project_id = :p AND run_id = :r"),
            {"p": self.project_id, "r": run_id})


# ── The token exchange ────────────────────────────────────────────────────────────────────────

def narrowed_scope(scope: Any, *, writes: bool) -> frozenset:
    """A child's requested scope: the parent's, or only its read permissions for a reader."""
    perms = frozenset(str(p) for p in scope or ())
    return perms if writes else frozenset(p for p in perms if p.endswith(":read"))


class OboExchange:
    """The identity port of kernel/subagents (see the module docstring). Outside a delegated run
    (no run token in context: the legacy path) the child runs under the ambient authority, as
    every tool call there already does."""

    def __init__(self, engine: Any, *, project_id: str, base_url: Optional[str] = None,
                 db: Any = None):
        self.engine, self.project_id, self.base_url, self.db = engine, project_id, base_url, db
        self.shims: dict = {}

    @contextlib.contextmanager
    def _db(self) -> Iterator[Any]:
        if self.db is not None:
            yield self.db
            return
        with self.engine.begin() as db:
            yield db

    @contextlib.contextmanager
    def child(self, request: Any) -> Iterator[Any]:
        from ..identity import lineage
        from ..identity import runs as identity_runs
        from ..identity import tokens
        from ..identity.delegation import AUD_API

        ctx = lineage.current_context()
        if ctx is None or ctx.run_token is None:
            yield None
            return
        parent_run = ctx.run_token.task.run_id
        shim = identity_runs.shim_for(parent_run)
        if shim is None:
            raise PermissionError("the parent run holds no credential to exchange")
        base = self.base_url or tokens.issuer()
        try:
            with self._db() as db:
                agent_id = identity_runs.agent_for_role(db, self.project_id, request.target)
                if not agent_id:
                    raise PermissionError(f"{request.target} has no Passport in this project")
                child = identity_runs.spawn_child(
                    shim, child_agent_id=agent_id, child_name=f"ag:{request.child_run_id}",
                    scope=narrowed_scope(ctx.run_token.scope, writes=request.writes), db=db,
                    base_url=base)
                url = base + "/internal/run"
                claims = tokens.verify_run_token(
                    child.tokens[AUD_API].access_token, audience=AUD_API,
                    dpop_proof=child.authorization(AUD_API, "POST", url)["DPoP"], htm="POST",
                    htu=url, db=db)
                child_ctx = tokens.context_for(claims, db=db)
        except tokens.TokenError as exc:
            raise PermissionError(exc.reason) from exc
        self.shims[request.child_run_id] = child
        with lineage.acting(child_ctx):
            yield child_ctx

    def release(self, request: Any) -> None:
        """The child ended: its tokens and egress die now (identity/runs.end_run)."""
        from ..identity import runs as identity_runs

        child = self.shims.pop(request.child_run_id, None)
        if child is None:
            return
        try:
            with self._db() as db:
                org = db.execute(text(
                    "SELECT org_id FROM identity_run_credentials WHERE run_id = :r LIMIT 1"),
                    {"r": child.run_id}).scalar() or ""
                identity_runs.end_run(child.run_id, db=db, org_id=str(org))
        except Exception as exc:  # noqa: BLE001 — the token still expires; never fail the parent
            log.warning("subagents: child credentials not ended (non-fatal): %s", exc)


# ── Async sub-agents: durable child runs, A2A Tasks ───────────────────────────────────────────

def task_of(row: Any) -> dict:
    """An agent_runs row as an A2A v1.0 Task (the shape agreed with P5)."""
    from ..protocols.a2a_types import (Artifact, Task, TaskState, TaskStatus, agent_message,
                                       state_for_run_status, text_part)

    row = dict(row)
    state = state_for_run_status(row.get("status"))
    message = None
    pending = _json(row.get("pending"), None) or {}
    if state == TaskState.INPUT_REQUIRED:
        message = agent_message(
            "waiting on a person", context_id=row.get("thread_id"), task_id=row["id"],
            data={"decision_id": pending.get("decision_id"),
                  "interrupt_id": pending.get("interrupt_id")})
    elif state == TaskState.FAILED and row.get("error"):
        message = agent_message(str(row["error"])[:2000], context_id=row.get("thread_id"),
                                task_id=row["id"])
    artifacts = []
    if state == TaskState.COMPLETED and row.get("output_text"):
        artifacts.append(Artifact(parts=[text_part(str(row["output_text"]))], name="report"))
    fctx = _json(row.get("finalize_ctx"), {}) or {}
    sub = fctx.get("subagent") if isinstance(fctx.get("subagent"), dict) else {}
    return Task(id=str(row["id"]), context_id=row.get("thread_id"),
                status=TaskStatus(state=state, message=message), artifacts=artifacts,
                metadata={"target": sub.get("target") or row.get("agent_key"),
                          "parent_run_id": row.get("parent_run_id"),
                          "boss_status": row.get("status")}).to_dict()


_ROW = ("id, project_id, thread_id, status, agent_key, parent_run_id, output_text, error, "
        "pending, finalize_ctx, trigger")


class DbAsyncSubagents:
    """The async port of kernel/subagents over agent_runs / agent_threads / jobs, on the parent
    run's connection (each start is admitted in the parent's transaction)."""

    def __init__(self, connection: Connection, *, project_id: str, actor: str,
                 parent_role: Optional[str] = None, role_prompt: Any = None,
                 max_steps: Optional[int] = None):
        self.connection, self.project_id, self.actor = connection, project_id, actor
        self.parent_role = parent_role
        #: target -> the role prompt its child starts with (the agent's compiled prompt).
        self.role_prompt = role_prompt or (lambda target: "")
        self.max_steps = max_steps

    def _row(self, run_id: str) -> Optional[dict]:
        row = self.connection.execute(text(
            f"SELECT {_ROW} FROM agent_runs WHERE id = :r AND project_id = :p"),
            {"r": run_id, "p": self.project_id}).mappings().first()
        return dict(row) if row else None

    def start(self, request: Any) -> dict:
        from ..kernel.subagents import child_system
        from . import thread_runs

        parent_thread = self.connection.execute(text(
            "SELECT thread_id FROM agent_runs WHERE id = :r"),
            {"r": request.parent_run_id}).scalar()
        tid = thread_runs.ensure_thread(
            self.connection, project_id=self.project_id, actor=self.actor,
            external_ref=f"subagent:{request.parent_run_id}:{request.call_id}",
            agent_id=request.target)
        system = child_system(request, role_prompt=str(self.role_prompt(request.target) or ""))
        fctx = {"role_key": request.target,
                "subagent": {"parent_run_id": request.parent_run_id,
                             "parent_thread_id": parent_thread, "target": request.target,
                             "call_id": request.call_id, "depth": request.depth,
                             "token_budget": request.token_budget,
                             "usd_micros_budget": request.usd_micros_budget,
                             "report_tokens": request.report_tokens, "writes": request.writes,
                             "branch": request.branch, "notify": True},
                **({"max_steps": int(self.max_steps)} if self.max_steps else {})}
        admitted = thread_runs.admit(
            self.connection, project_id=self.project_id, thread_id=tid,
            messages=[{"role": "system", "content": system}, *request.messages],
            finalize_ctx=fctx, trigger=TRIGGER, run_id=request.child_run_id,
            parent_run_id=request.parent_run_id)
        self.connection.execute(text(
            "UPDATE agent_runs SET agent_key = :a WHERE id = :r"),
            {"a": request.target, "r": admitted["run_id"]})
        emit_event(self.connection, self.project_id, "agent.subagent_started", self.actor,
                   "agent_run", admitted["run_id"],
                   {"parent_run_id": request.parent_run_id, "target": request.target,
                    "thread_id": tid, "async": True})
        return task_of(self._row(admitted["run_id"]))

    def check(self, task_id: str) -> dict:
        row = self._row(task_id)
        if row is None:
            raise LookupError(f"no sub-agent task {task_id}")
        return task_of(row)

    def update(self, task_id: str, message: str, *, strategy: str = "enqueue") -> dict:
        """More instructions: a new run on the child's thread, queued after the current one
        (`enqueue`) or stopping it at its next step (`interrupt`). Its Task is returned."""
        from . import thread_runs

        row = self._row(task_id)
        if row is None or not row.get("thread_id"):
            raise LookupError(f"no sub-agent task {task_id}")
        fctx = _json(row.get("finalize_ctx"), {}) or {}
        keep = {k: v for k, v in fctx.items() if k in ("role_key", "subagent", "max_steps")}
        admitted = thread_runs.admit(
            self.connection, project_id=self.project_id, thread_id=row["thread_id"],
            messages=[{"role": "user", "content": str(message)}], finalize_ctx=keep,
            policy="interrupt" if strategy == "interrupt" else "enqueue", trigger=TRIGGER,
            parent_run_id=row.get("parent_run_id"))
        self.connection.execute(text("UPDATE agent_runs SET agent_key = :a WHERE id = :r"),
                                {"a": row.get("agent_key"), "r": admitted["run_id"]})
        return task_of(self._row(admitted["run_id"]))

    def cancel(self, task_id: str) -> dict:
        from . import agent_protocol

        agent_protocol.cancel_run(self.connection, project_id=self.project_id, run_id=task_id,
                                  actor=self.actor)
        return task_of(self._row(task_id))

    def list(self, parent_run_id: str) -> list:
        rows = self.connection.execute(text(
            f"SELECT {_ROW} FROM agent_runs WHERE parent_run_id = :r AND project_id = :p "
            "AND trigger = :t ORDER BY created_at, id"),
            {"r": parent_run_id, "p": self.project_id, "t": TRIGGER}).mappings().all()
        return [task_of(r) for r in rows]

    def notifications(self, parent_run_id: str) -> list:
        """The children's news this parent has not read yet, marked read."""
        rows = self.connection.execute(text("""
            UPDATE agent_subagent_notifications SET delivered_at = now(), delivered_to = :r
             WHERE parent_run_id = :r AND delivered_at IS NULL
         RETURNING child_run_id, target, state, task, report, created_at"""),
            {"r": parent_run_id}).mappings().all()
        return [{"task": _json(r["task"], {}), "target": r["target"], "state": r["state"],
                 "report": r["report"] or ""} for r in sorted(rows, key=lambda x: x["created_at"])]


# ── Event-driven notification ─────────────────────────────────────────────────────────────────

def notify_parent(connection: Connection, run_id: str) -> Optional[str]:
    """A run that might be a background sub-agent has ended or stopped for a person (called by
    thread_runs.finish). Its Task is written for the parent; a parent that already ended is woken
    on its thread. Returns the notification id (None: not a sub-agent, or already told)."""
    row = connection.execute(text(
        f"SELECT {_ROW} FROM agent_runs WHERE id = :r"), {"r": run_id}).mappings().first()
    if row is None or row["trigger"] != TRIGGER or not row["parent_run_id"]:
        return None
    task = task_of(row)
    state = str((task.get("status") or {}).get("state") or "")
    if state not in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
                     "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_REJECTED"):
        return None
    fctx = _json(row["finalize_ctx"], {}) or {}
    sub = fctx.get("subagent") if isinstance(fctx.get("subagent"), dict) else {}
    nid = make_id("snote")
    inserted = connection.execute(text("""
        INSERT INTO agent_subagent_notifications (id, project_id, parent_run_id,
               parent_thread_id, child_run_id, target, state, task, report)
        VALUES (:id, :p, :parent, :pt, :child, :target, :state, CAST(:task AS JSONB), :report)
        ON CONFLICT (child_run_id, state) DO NOTHING RETURNING id"""),
        {"id": nid, "p": row["project_id"], "parent": row["parent_run_id"],
         "pt": sub.get("parent_thread_id"), "child": run_id, "target": sub.get("target"),
         "state": state, "task": json.dumps(task, default=str),
         "report": str(row["output_text"] or "")[:REPORT_CHARS]}).scalar()
    if inserted is None:
        return None
    emit_event(connection, row["project_id"], "agent.subagent_finished", "boss", "agent_run",
               run_id, {"parent_run_id": row["parent_run_id"], "state": state,
                        "target": sub.get("target"), "notification_id": nid})
    if sub.get("notify", True):
        _wake(connection, row, sub, nid, task)
    return nid


def _wake(connection: Connection, child: dict, sub: dict, nid: str, task: dict) -> None:
    """A parent that is no longer running reads nothing at "its next step": its thread is woken
    with the news instead — a new run on it, admitted (with its job) in this transaction."""
    from ..kernel.subagents import note_text
    from . import thread_runs

    parent = connection.execute(text(
        "SELECT id, project_id, thread_id, status, task_id, finalize_ctx FROM agent_runs "
        "WHERE id = :r"), {"r": child["parent_run_id"]}).mappings().first()
    if parent is None or not parent["thread_id"] or parent["status"] not in thread_runs.ENDED:
        return  # running or waiting: it reads the notification at its next step
    fctx = _json(parent["finalize_ctx"], {}) or {}
    keep = {k: v for k, v in fctx.items() if k not in (
        "kernel", "created_by_job_id", "thread_id", "loop", "executes_on")}
    note = note_text({"task": task, "target": sub.get("target"),
                      "report": str(child.get("output_text") or "")[:REPORT_CHARS]})
    admitted = thread_runs.admit(
        connection, project_id=parent["project_id"], thread_id=parent["thread_id"],
        messages=[{"role": "user", "content": note}], finalize_ctx=keep, trigger=WAKE_TRIGGER,
        task_id=parent["task_id"], parent_run_id=parent["id"])
    connection.execute(text(
        "UPDATE agent_subagent_notifications SET delivered_at = now(), delivered_to = :to "
        "WHERE id = :n"), {"to": admitted["run_id"], "n": nid})


# ── A remote agent as a sub-agent target (P5's adapter) ──────────────────────────────────────

class RemoteTarget:
    """An A2A agent or an Agent Protocol server (LangGraph) as a kernel/subagents target, through
    P5's adapter (services/remote_subagents): `run` for `task`, `start` / check / update /
    cancel for the async tools. P5 records the remote span (under the `task` span: the
    traceparent is forwarded) and books the remote's reported cost on the parent run; the
    remote's tokens count against the supervisor's budget like a local child's. A remote has no
    BOSS Decision to propagate: a synchronous task that stops for input is reported to the parent
    as such (answer it with an async start + update_subagent)."""

    writes = False
    parallel_safe = True

    def __init__(self, name: str, ref: Any, *, engine: Any, project_id: str,
                 description: str = "", timeout: float = 120.0, transport: Any = None,
                 sleep: Any = None):
        import time as _time

        self.name, self.ref, self.engine, self.project_id = name, ref, engine, project_id
        self.description, self.timeout, self.transport = description, timeout, transport
        self.sleep = sleep or _time.sleep

    def _outcome(self, request: Any, out: dict) -> Any:
        from ..kernel.subagents import SubagentOutcome
        from ..kernel.types import Usage
        from ..protocols import a2a_types as t
        from . import remote_subagents as rs

        task = t.Task.from_dict(out["task"])
        cost = rs.cost_of(task.metadata)
        usage = Usage(model=f"remote:{self.name}", input_tokens=cost["prompt_tokens"],
                      output_tokens=cost["completion_tokens"],
                      cost_usd_micros=int(out.get("cost_usd_micros") or 0))
        report = str(out.get("report") or rs.report_of(task))
        if task.state == t.TaskState.COMPLETED:
            return SubagentOutcome(status="completed", child_run_id=task.id, text=report,
                                   usage=usage)
        if task.state.interrupted or out.get("timed_out"):
            why = "is waiting for input" if task.state.interrupted else "did not finish in time"
            return SubagentOutcome(status="completed", child_run_id=task.id, usage=usage,
                                   text=f"[remote agent {self.name} {why}] {report}",
                                   stopped="input_required" if task.state.interrupted
                                   else "timeout")
        return SubagentOutcome(status="failed", child_run_id=task.id, usage=usage,
                               error=f"remote task {task.state.value}: {report}"[:600])

    def run(self, request: Any) -> Any:
        from . import remote_subagents as rs

        try:
            out = rs.run(self.engine, project_id=self.project_id,
                         parent_run_id=request.parent_run_id, ref=self.ref, brief=request.brief,
                         timeout=self.timeout, traceparent=request.traceparent,
                         transport=self.transport, sleep=self.sleep)
        except rs.RemoteAgentError as exc:
            from ..kernel.subagents import SubagentOutcome

            return SubagentOutcome(status="failed", child_run_id=request.child_run_id,
                                   error=str(exc)[:600])
        return self._outcome(request, out)

    def resume(self, request: Any, *, answers: dict) -> Any:
        from ..kernel.subagents import SubagentOutcome

        return SubagentOutcome(status="failed", child_run_id=request.child_run_id,
                               error="a remote sub-agent is answered with update_subagent")

    def start(self, request: Any) -> dict:
        from . import remote_subagents as rs

        out = rs.start(self.engine, project_id=self.project_id,
                       parent_run_id=request.parent_run_id, ref=self.ref, brief=request.brief,
                       traceparent=request.traceparent, transport=self.transport)
        task = dict(out["task"])
        task["metadata"] = {**dict(task.get("metadata") or {}), "target": self.name,
                            "remote_handle": out["handle"]}
        return task

    def check_task(self, task_id: str, entry: dict) -> dict:
        from . import remote_subagents as rs

        return rs.check(self.engine, project_id=self.project_id, ref=self.ref,
                        handle=entry["handle"], transport=self.transport)["task"]

    def update_task(self, task_id: str, entry: dict, message: str) -> dict:
        from . import remote_subagents as rs

        out = rs.update(self.engine, project_id=self.project_id, ref=self.ref,
                        handle=entry["handle"], answer=message, transport=self.transport)
        entry["handle"] = out["handle"]
        return out["task"]

    def cancel_task(self, task_id: str, entry: dict) -> dict:
        from . import remote_subagents as rs

        return rs.cancel(self.engine, project_id=self.project_id, ref=self.ref,
                         handle=entry["handle"], transport=self.transport)["task"]


# ── What a published spec asks for ────────────────────────────────────────────────────────────

def _active_body(connection: Connection, project_id: str, role: str) -> tuple:
    """(the spec body, the compiled prompt) of the agent's active version, or ({}, "")."""
    try:
        from ..agents.versions import active

        with connection.begin_nested():
            version = active(connection, project_id, role)
    except Exception:  # noqa: BLE001 — no published spec: nothing to coordinate
        return {}, ""
    doc = (version or {}).get("spec") or {}
    body = doc.get("spec") if isinstance(doc, dict) else None
    return dict(body or {}), str((version or {}).get("compiled_prompt") or "")


def middleware_for(connection: Connection, *, project_id: str, role: str, run_id: str,
                   actor: str, finalize_ctx: Optional[dict] = None,
                   branch: Optional[str] = None, parent_writes: bool = False) -> tuple:
    """The P7 middleware an agent run gets from its published spec: write_todos and the rubric
    (each opt-in), the sub-agent tools (only when `coordination.enabled`), and — for a run that
    IS a background sub-agent — its budget slice. () for every agent that opts into nothing."""
    from ..kernel import planning, subagents

    body, _prompt = _active_body(connection, project_id, role) if role else ({}, "")
    out: list = list(planning.from_spec(body))
    fctx = dict(finalize_ctx or {})
    sub = fctx.get("subagent") if isinstance(fctx.get("subagent"), dict) else {}
    co = subagents.Coordination.from_spec(body.get("coordination"))
    if co.enabled:
        defs = {d.ref: d for d in (subagents.SubagentDef.from_spec(e)
                                   for e in body.get("subagents") or [])}

        def prompt_of(target: str) -> str:
            return _active_body(connection, project_id, target)[1]

        targets = {ref: subagents.LocalTarget(
            ref, subagents.inherit(role_prompt=prompt_of(ref)), description=d.description,
            writes=d.writes, parallel_safe=False) for ref, d in defs.items()}
        engine = getattr(connection, "engine", None)
        out.append(subagents.Subagents(
            co, targets=targets, defs=defs, depth=int(sub.get("depth") or 0),
            branch=branch or sub.get("branch"), parent_writes=parent_writes,
            branches=DbBranches(connection, project_id),
            identity=OboExchange(engine, project_id=project_id, db=connection)
            if engine is not None else None,
            async_port=DbAsyncSubagents(connection, project_id=project_id, actor=actor,
                                        parent_role=role, role_prompt=prompt_of)))
    if sub.get("token_budget") or sub.get("usd_micros_budget"):
        out.append(subagents.SliceGuard(sub.get("token_budget"), sub.get("usd_micros_budget")))
    return tuple(out)


__all__ = ["DbAsyncSubagents", "DbBranches", "OboExchange", "REPORT_CHARS", "RemoteTarget", "TRIGGER",
           "WAKE_TRIGGER", "middleware_for", "narrowed_scope", "notify_parent", "task_of"]
