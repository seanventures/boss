"""PER-AGENT ENDPOINTS ON THE AGENT PROTOCOL MODEL (AP-P3, BOSSAPP-1054; AGENT_PLATFORM §4.4
"Per-agent endpoints"; wireframe A7).

The resource model is the Agent Protocol's (Agents, Threads, Runs): a client makes a THREAD with
a deployed agent, starts RUNS on it, streams each run's typed events, gets, joins or cancels a
run, and forks a thread from any checkpoint. Every run is the same run the rest of BOSS has —
admitted on its thread (thread_runs.admit, one active run per thread, double-texting), pinned to
the deployment's version, executed by a worker, governed by the kernel's mandatory middleware.
An interrupt is a Decision: a client approves it through the Decision API.

Run statuses, as the protocol names them:

    pending      admitted, waiting for a worker (or behind another run on its thread)
    running      a worker is executing it
    interrupted  waiting on a person (a Decision is open)
    success      completed
    error        failed
    cancelled    cancelled, or erased by a rollback

THE STREAM. `stream` yields typed events `{type, ns, data}` as Server-Sent Events:

    metadata   first: the run and thread ids
    updates    one per checkpoint (a step boundary): step, phase, status
    messages   each new message (the model's turns, tool results) as it is checkpointed
    custom     an interrupt: the decisions a person must answer
    values     the run's state when it ends or stops for a person
    end        last
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import agent_deployments
from .core import emit_event

STATUS = {"pending": "pending", "queued": "pending", "drained": "pending",
          "budget_wait": "pending", "running": "running",
          "awaiting_approval": "interrupted", "completed": "success", "failed": "error",
          "denied": "error", "cancelled": "cancelled", "rolled_back": "cancelled"}
TERMINAL = frozenset({"success", "error", "cancelled"})


class ProtocolError(ValueError):
    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── Threads ────────────────────────────────────────────────────────────────────────────────────

def _thread_row(connection: Connection, project_id: str, thread_id: str) -> dict:
    row = connection.execute(text(
        "SELECT id, project_id, actor, agent_id, deployment_id, run_state, active_run_id, "
        "double_text, metadata, created_at, updated_at FROM agent_threads WHERE id = :t "
        "AND project_id = :p AND surface = 'agent'"), {"t": thread_id, "p": project_id}
    ).mappings().first()
    if row is None:
        raise ProtocolError("no such thread in this project.", status=404)
    return dict(row)


def thread_out(row: dict) -> dict:
    return {"thread_id": row["id"], "agent_key": row.get("agent_id"),
            "deployment_id": row.get("deployment_id"),
            "status": row.get("run_state"), "active_run_id": row.get("active_run_id"),
            "double_text": row.get("double_text"), "metadata": _json(row.get("metadata"), {}),
            "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))}


def create_thread(connection: Connection, *, project_id: str, agent_key: str, actor: str,
                  environment: str = "prod", metadata: Optional[dict] = None,
                  double_text: str = "enqueue") -> dict:
    """A new thread with a deployed agent (refused when the agent is not deployed there or not
    exposed over REST)."""
    from . import thread_runs

    dep = agent_deployments.live_for_run(connection, project_id, agent_key, environment)
    if not (dep.get("expose") or {}).get("rest", True):
        raise ProtocolError(f"{agent_key} is not exposed over the API in {environment}.",
                            status=403)
    if double_text not in thread_runs.POLICIES:
        raise ProtocolError(f"double_text must be one of {', '.join(thread_runs.POLICIES)}.")
    tid = thread_runs.ensure_thread(connection, project_id=project_id, actor=actor,
                                    agent_id=agent_key, double_text=double_text)
    connection.execute(text(
        "UPDATE agent_threads SET deployment_id = :d, metadata = metadata || CAST(:m AS JSONB) "
        "WHERE id = :t"), {"d": dep["id"], "m": json.dumps({**(metadata or {}),
                                                           "environment": dep["environment"]}),
                           "t": tid})
    emit_event(connection, project_id, "agent.thread_created", actor, "agent_thread", tid,
               {"agent_key": agent_key, "environment": dep["environment"]})
    return thread_out(_thread_row(connection, project_id, tid))


def get_thread(connection: Connection, project_id: str, thread_id: str) -> dict:
    return thread_out(_thread_row(connection, project_id, thread_id))


def thread_history(connection: Connection, project_id: str, thread_id: str) -> dict:
    """The thread's runs, oldest first, and its checkpoints (the points a fork can start from)."""
    _thread_row(connection, project_id, thread_id)
    runs = [run_out(dict(r)) for r in connection.execute(text(
        f"SELECT {_RUN_COLS} FROM agent_runs WHERE thread_id = :t AND project_id = :p "
        "ORDER BY created_at, id"), {"t": thread_id, "p": project_id}).mappings().all()]
    checkpoints = [{"checkpoint_id": r["checkpoint_id"], "run_id": r["run_id"],
                    "parent_checkpoint_id": r["parent_checkpoint_id"], "step": r["step"],
                    "source": r["source"], "status": r["status"],
                    "created_at": _iso(r["created_at"])}
                   for r in connection.execute(text(
                       "SELECT checkpoint_id, run_id, parent_checkpoint_id, step, source, status, "
                       "created_at FROM agent_checkpoints WHERE thread_id = :t ORDER BY seq"),
                       {"t": thread_id}).mappings().all()]
    return {"thread_id": thread_id, "runs": runs, "checkpoints": checkpoints}


# ── Runs ───────────────────────────────────────────────────────────────────────────────────────

_RUN_COLS = ("id, project_id, thread_id, status, agent_key, agent_version, agent_version_hash, "
             "environment, deployment_id, trigger, parent_run_id, output_text, error, pending, "
             "input, created_at, updated_at")


def run_out(row: dict, *, decisions: Optional[list] = None) -> dict:
    pending = _json(row.get("pending"), None)
    return {"run_id": row["id"], "thread_id": row.get("thread_id"),
            "status": STATUS.get(str(row.get("status")), str(row.get("status"))),
            "boss_status": row.get("status"), "agent_key": row.get("agent_key"),
            "version": row.get("agent_version"), "version_hash": row.get("agent_version_hash"),
            "environment": row.get("environment"), "trigger": row.get("trigger"),
            "parent_run_id": row.get("parent_run_id"), "output": row.get("output_text"),
            "error": row.get("error"), "interrupts": decisions if decisions is not None else (
                (pending or {}).get("interrupts") if isinstance(pending, dict) else None),
            "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))}


def _decisions_for(connection: Connection, run_id: str) -> list:
    return [{"decision_id": r["id"], "interrupt_id": r["interrupt_id"], "status": r["status"],
             "question": r["question"]}
            for r in connection.execute(text(
                "SELECT id, interrupt_id, status, question FROM decisions "
                "WHERE recommendation->>'run_id' = :r ORDER BY created_at"),
                {"r": run_id}).mappings().all()]


def get_run(connection: Connection, project_id: str, run_id: str,
            thread_id: Optional[str] = None) -> dict:
    row = connection.execute(text(
        f"SELECT {_RUN_COLS} FROM agent_runs WHERE id = :r AND project_id = :p "
        "AND thread_id IS NOT NULL"), {"r": run_id, "p": project_id}).mappings().first()
    if row is None or (thread_id and row["thread_id"] != thread_id):
        raise ProtocolError("no such run in this project.", status=404)
    return run_out(dict(row), decisions=_decisions_for(connection, run_id))


def start_run(connection: Connection, *, project_id: str, thread_id: str, actor: str,
              input: Any, double_text: Optional[str] = None) -> dict:
    """A run on the thread, admitted with its job in one transaction and pinned to the version
    the thread's deployment runs now."""
    from . import thread_runs

    thread = _thread_row(connection, project_id, thread_id)
    if not thread.get("deployment_id"):
        raise ProtocolError("this thread has no deployed agent.", status=409)
    dep = agent_deployments.by_id(connection, thread["deployment_id"])
    if dep is None or dep["status"] != "live":
        raise ProtocolError("the agent this thread talks to is no longer deployed.", status=409)
    content = input if isinstance(input, str) else (
        (input or {}).get("message") or (input or {}).get("goal")
        or json.dumps(input, default=str)) if input else ""
    if not str(content).strip():
        raise ProtocolError("say what the run should do (`input`).")
    try:
        admitted = agent_deployments.admit_run(
            connection, dep, thread_id=thread_id,
            messages=[{"role": "user", "content": str(content)}], trigger="api",
            policy=double_text, actor=actor)
    except thread_runs.ThreadBusy as exc:
        raise ProtocolError(f"the thread is busy with {exc.active_run_id} and its double-text "
                            "policy is reject.", status=409) from exc
    return get_run(connection, project_id, admitted["run_id"])


def cancel_run(connection: Connection, *, project_id: str, run_id: str, actor: str) -> dict:
    """Cancel: a run waiting (for a worker, behind another, on a person) ends now; a running one
    stops at its next step boundary (the kernel's cancel hook)."""
    from . import thread_runs

    run = get_run(connection, project_id, run_id)
    status = run["boss_status"]
    if run["status"] in TERMINAL:
        return {**run, "cancel": "already_ended"}
    if status == "running":
        connection.execute(text("""
            UPDATE agent_threads SET metadata = metadata || jsonb_build_object('stop_run',
                   CAST(:r AS TEXT)), updated_at = now() WHERE id = :t"""),
            {"r": run_id, "t": run["thread_id"]})
        outcome = "stopping"
    else:
        thread_runs.finish(connection, run_id, "cancelled")
        connection.execute(text(
            "UPDATE jobs SET status = 'cancelled', updated_at = now() WHERE kind IN "
            "('agent.run', 'agent.resume') AND payload->>'run_id' = :r "
            "AND status IN ('queued', 'failed_retryable')"), {"r": run_id})
        connection.execute(text(
            "UPDATE decisions SET status = 'cancelled', updated_at = now() WHERE "
            "recommendation->>'run_id' = :r AND status = 'pending'"), {"r": run_id})
        outcome = "cancelled"
    emit_event(connection, project_id, "agent.run_cancel_requested", actor, "agent_run",
               run_id, {"outcome": outcome})
    return {**get_run(connection, project_id, run_id), "cancel": outcome}


def fork(connection: Connection, *, project_id: str, thread_id: str, run_id: str, actor: str,
         checkpoint_id: Optional[str] = None, tool_results: Optional[dict] = None) -> dict:
    """`fork(thread, checkpoint)` (P2's kernel/durable.fork): the run's state at a checkpoint (its
    latest by default) becomes a NEW run on the same thread, its tool results optionally
    replaced, admitted like any run on the same pinned version; it resumes from there."""
    from ..kernel.durable import PgCheckpointStore
    from ..kernel.durable import fork as _fork
    from .core import make_id

    source = get_run(connection, project_id, run_id, thread_id)
    cp = checkpoint_id or connection.execute(text(
        "SELECT checkpoint_id FROM agent_checkpoints WHERE run_id = :r ORDER BY seq DESC "
        "LIMIT 1"), {"r": run_id}).scalar()
    if not cp:
        raise ProtocolError("that run has no checkpoint to fork from.", status=409)
    owner = connection.execute(text(
        "SELECT run_id FROM agent_checkpoints WHERE checkpoint_id = :c"), {"c": cp}).scalar()
    if owner != run_id:
        raise ProtocolError("that checkpoint is not this run's.", status=404)
    row = connection.execute(text("SELECT finalize_ctx, deployment_id FROM agent_runs "
                                  "WHERE id = :r"), {"r": run_id}).mappings().first()
    dep = agent_deployments.by_id(connection, row["deployment_id"]) if row["deployment_id"] \
        else None
    if dep is None:
        raise ProtocolError("only a deployed agent's run can be forked here.", status=409)
    new_id = make_id("arun")
    store = PgCheckpointStore(connection, thread_id=thread_id)
    _fork(store, cp, new_run_id=new_id, tool_results=tool_results or None)
    pinned = {**dep, "version": source["version"], "content_hash": source["version_hash"]}
    agent_deployments.admit_run(
        connection, pinned, thread_id=thread_id, run_id=new_id, parent_run_id=run_id,
        messages=[{"role": "user", "content": f"(forked from {run_id} at {cp})"}],
        trigger="fork", actor=actor, extra={"forked_from": {"run_id": run_id,
                                                            "checkpoint_id": cp}})
    return {**get_run(connection, project_id, new_id), "forked_from": {"run_id": run_id,
                                                                       "checkpoint_id": cp}}


# ── Waiting and streaming (out of any transaction: each poll reads committed state) ────────────

def join(engine: Any, project_id: str, run_id: str, *, timeout: float = 300.0,
         poll: float = 0.5) -> dict:
    """Wait until the run ends or stops for a person (or `timeout`); its state then."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        with engine.connect() as c:
            run = get_run(c, project_id, run_id)
        stopped = run["status"] in TERMINAL or waits_on_a_person(run)
        if stopped or time.monotonic() >= deadline:
            return {**run, "timed_out": not stopped}
        time.sleep(poll)


def waits_on_a_person(run: dict) -> bool:
    """Interrupted AND a decision still pending: once every one is answered the run is about to
    resume (its job is queued), so a client waiting on it keeps waiting."""
    return run["status"] == "interrupted" and any(
        d.get("status") == "pending" for d in run.get("interrupts") or [])


def sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


def stream(engine: Any, project_id: str, run_id: str, *, timeout: float = 300.0,
           poll: float = 0.25, stop_on_interrupt: bool = True) -> Iterator[dict]:
    """The run's typed events (see the module docstring) until it ends — or stops for a person,
    when `stop_on_interrupt` — or `timeout`."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    seen_seq, seen_messages, announced = 0, 0, set()
    with engine.connect() as c:
        run = get_run(c, project_id, run_id)
    yield {"type": "metadata", "ns": [], "data": {"run_id": run_id,
                                                  "thread_id": run["thread_id"],
                                                  "agent_key": run["agent_key"],
                                                  "version": run["version"]}}
    while True:
        with engine.connect() as c:
            rows = c.execute(text(
                "SELECT seq, checkpoint_id, step, phase, status, source, state "
                "FROM agent_checkpoints WHERE run_id = :r AND seq > :s ORDER BY seq"),
                {"r": run_id, "s": seen_seq}).mappings().all()
            run = get_run(c, project_id, run_id)
        for cp in rows:
            seen_seq = int(cp["seq"])
            state = _json(cp["state"], {}) or {}
            yield {"type": "updates", "ns": [f"step:{cp['step']}"],
                   "data": {"checkpoint_id": cp["checkpoint_id"], "step": cp["step"],
                            "phase": cp["phase"], "status": cp["status"],
                            "source": cp["source"]}}
            messages = state.get("messages") or []
            for message in messages[seen_messages:]:
                yield {"type": "messages", "ns": [f"step:{cp['step']}"],
                       "data": {"role": message.get("role"), "content": message.get("content"),
                                "tool_calls": message.get("tool_calls"),
                                "tool_call_id": message.get("tool_call_id")}}
            seen_messages = max(seen_messages, len(messages))
        if run["status"] == "interrupted":
            fresh = [d for d in run["interrupts"] or [] if d.get("status") == "pending"
                     and d["decision_id"] not in announced]
            if fresh:
                announced.update(d["decision_id"] for d in fresh)
                yield {"type": "custom", "ns": [], "data": {
                    "kind": "interrupt", "decisions": fresh,
                    "how": "Answer each through the Decision API (POST /v1/decisions/{id}/"
                           "approve or /reject); the run resumes when the last is answered."}}
        if run["status"] in TERMINAL or (stop_on_interrupt and waits_on_a_person(run)):
            yield {"type": "values", "ns": [], "data": run}
            yield {"type": "end", "ns": [], "data": {"status": run["status"]}}
            return
        if time.monotonic() >= deadline:
            yield {"type": "values", "ns": [], "data": run}
            yield {"type": "end", "ns": [], "data": {"status": run["status"], "timed_out": True}}
            return
        time.sleep(poll)


# ── The A7 card and the MCP view ───────────────────────────────────────────────────────────────

def mcp_tool_name(agent_key: str) -> str:
    return f"agent_{agent_key}"


def endpoints_card(connection: Connection, project_id: str, agent_key: str, *,
                   environment: str = "prod") -> dict:
    """Wireframe A7: the three ways to use the agent from outside (REST + stream, MCP tool,
    A2A — off, D11), who may call it, and a working `curl`."""
    dep = agent_deployments.get(connection, project_id, agent_key, environment)
    if dep is None:
        raise ProtocolError(f"{agent_key} is not deployed to {environment}.", status=404)
    expose = dep.get("expose") or {}
    callers = [{"principal_id": r["id"], "name": r["display_name"], "kind": r["kind"],
                "via": r["role_key"], "may": "runs only", "last_used_at": _iso(r["last_used"])}
               for r in connection.execute(text("""
                   SELECT p.id, p.display_name, p.kind, min(ar.key) AS role_key,
                          (SELECT max(k.last_used_at) FROM identity_api_credentials k
                            WHERE k.principal_id = p.id) AS last_used
                     FROM identity_role_bindings b
                     JOIN identity_principals p ON p.id = b.subject_id
                     JOIN identity_access_roles ar ON ar.id = b.role_id
                     JOIN identity_access_role_permissions rp ON rp.role_id = b.role_id
                    WHERE rp.permission_key = 'agent:invoke' AND p.kind = 'service_account'
                      AND b.subject_type = 'principal' AND p.status = 'active'
                      AND (b.scope_type = 'org' OR b.scope_id = :p)
                    GROUP BY p.id, p.display_name, p.kind ORDER BY p.display_name LIMIT 50"""),
                   {"p": project_id}).mappings().all()] if _has_identity(connection) else []
    base = f"/v1/agents/{agent_key}"
    return {
        "agent_key": agent_key, "environment": environment, "version": dep.get("version"),
        "rest": {"enabled": bool(expose.get("rest", True)),
                 "create_thread": f"POST {base}/threads",
                 "start_run": "POST /v1/threads/{thread_id}/runs",
                 "stream": "POST /v1/threads/{thread_id}/runs/stream",
                 "wait": "POST /v1/threads/{thread_id}/runs/wait",
                 "permission": "agent:invoke"},
        "mcp": {"enabled": bool(expose.get("mcp", True)), "tool": mcp_tool_name(agent_key),
                "note": "Claude Code, Codex or any MCP client can call it."},
        "a2a": {"enabled": False, "card_path": f"/a2a/{agent_key}/.well-known/agent-card.json",
                "note": "Off: read-only agents only until sandbox isolation lands (D11)."},
        "callers": callers,
        "try_it": (f"curl -N $BOSS{base}/runs/stream \\\n"
                   "  -H \"Authorization: Bearer $BOSS_SERVICE_KEY\" \\\n"
                   f"  -d '{{\"project_id\": \"{project_id}\", \"input\": \"...\"}}'"),
    }


def _has_identity(connection: Connection) -> bool:
    return bool(connection.execute(text(
        "SELECT to_regclass('identity_access_role_permissions')")).scalar())


def mcp_agents(connection: Connection, project_id: str) -> list:
    """The deployed agents an MCP client may call as `agent_<key>` (prod, exposed over MCP,
    spec agents only — a migrated legacy bot is not a tool)."""
    rows = connection.execute(text(
        "SELECT agent_key, display_name, version FROM agent_deployments WHERE project_id = :p "
        "AND environment = 'prod' AND status = 'live' AND runner = 'deployed_agent' "
        "AND COALESCE((expose->>'mcp')::boolean, true) ORDER BY agent_key"),
        {"p": project_id}).mappings().all()
    return [{"tool": mcp_tool_name(r["agent_key"]), "agent_key": r["agent_key"],
             "name": r["display_name"], "version": r["version"]} for r in rows]


__all__ = ["ProtocolError", "STATUS", "cancel_run", "create_thread", "endpoints_card", "fork",
           "get_run", "get_thread", "join", "mcp_agents", "mcp_tool_name", "run_out", "sse",
           "start_run", "stream", "thread_history"]
