"""THE POSTGRES CHECKPOINTER: a run's state per super-step, its tool batch's pending writes, and
forks (AP-P2, BOSSAPP-1053; docs/design/AGENT_PLATFORM.md §4.3; decision D1: native, on BOSS's
own lease and fence machinery, not LangGraph's library).

    agent_checkpoints        one row per super-step: the model turn (phase 'tools': its calls
                             are in flight) and the step boundary after its tools (phase ''). A
                             row is never updated: history is a tree of parent links.
    agent_checkpoint_writes  one row per tool call of the in-flight batch, written as the call
                             lands — 'started' before a write tool runs, then its result — so a
                             crash mid-batch re-runs only the calls that did not finish, and an
                             irreversible one that started and left nothing parks instead of
                             firing twice (kernel/idempotency.py).

DURABILITY MODES (what a crash may lose, measured in the PR):

  sync    every checkpoint and write is committed before the run goes on. On a connection
          (`bind` is the run's own Connection, `commit=True`) the run's database effects commit
          WITH the checkpoint, so the two never disagree. The default for a run with write tools.
  async   checkpoints are written by a background writer on their own connection while the run
          goes on; flushed before the run ends or suspends. A crash may lose the last step or
          two. The default for a read-only run. The 'started' marker of a write still commits
          synchronously: it guards an effect.
  exit    nothing is written until the run ends, suspends or drains. A crash loses the run.

`fence` (optional) is called on the writing connection before every write: the worker's lease
check (jobs.require_active_lease), so a worker whose lease was reclaimed cannot write a
checkpoint over the new holder's (the fencing the job plane already does for builds).

`mirror` (optional) is a second store saved alongside every checkpoint: agent_loop's agent_runs
row, so the console and every reader of agent_runs see a kernel run exactly as before; `load`
falls back to it for a run started before this store existed.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any, Callable, Optional

from .state import RunState

SYNC = "sync"
ASYNC = "async"
EXIT = "exit"
MODES = (SYNC, ASYNC, EXIT)


def _text(sql: str) -> Any:
    from sqlalchemy import text

    return text(sql)


def new_checkpoint_id() -> str:
    return f"ckpt_{uuid.uuid4().hex[:24]}"


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class PgCheckpointStore:
    """A kernel CheckpointStore on agent_checkpoints / agent_checkpoint_writes (see module doc).
    `bind` is an Engine (each write its own transaction) or a Connection (writes on it; with
    `commit=True` each is committed there, commit-as-you-go)."""

    durable = True

    def __init__(self, bind: Any, *, thread_id: Optional[str] = None, ns: str = "",
                 durability: str = SYNC, commit: bool = False, mirror: Any = None,
                 fence: Optional[Callable[[Any], None]] = None):
        if durability not in MODES:
            raise ValueError(f"durability {durability!r} is not one of {MODES}")
        self.bind = bind
        self.thread_id = thread_id
        self.ns = ns
        self.durability = durability
        self.commit = commit
        self.mirror = mirror
        self.fence = fence
        #: Per write: {"kind", "bytes", "ms", "mode"} — what the PR measures.
        self.stats: list = []
        self._pending: Optional[dict] = None   # durability 'exit': the latest unsaved snapshot
        self._queue: Optional[queue.Queue] = None
        self._writer: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None

    # ── connections ───────────────────────────────────────────────────────────────────────
    def _is_connection(self) -> bool:
        return hasattr(self.bind, "execute") and not hasattr(self.bind, "connect")

    def _engine(self) -> Any:
        return self.bind.engine if self._is_connection() else self.bind

    def _run_sql(self, fn: Callable[[Any], Any], *, own: bool = False) -> Any:
        """`fn(connection)` on the run's connection (committed when asked), or in a transaction
        of its own (`own`, or an Engine bind)."""
        if self._is_connection() and not own:
            if self.fence is not None:
                self.fence(self.bind)
            out = fn(self.bind)
            if self.commit:
                self.bind.commit()
            return out
        with self._engine().begin() as connection:
            if self.fence is not None:
                self.fence(connection)
            return fn(connection)

    # ── checkpoints ───────────────────────────────────────────────────────────────────────
    def save(self, state: RunState, *, source: str = "loop") -> None:
        if self.mirror is not None:
            self.mirror.save(state)
        parent = state.checkpoint_id
        state.checkpoint_id = new_checkpoint_id()
        if state.thread_id is None and self.thread_id:
            state.thread_id = self.thread_id
        row = self._row(state, parent=parent, source=source)
        ending = state.status not in ("running",)
        if self.durability == EXIT and not ending and source in ("loop",):
            self._pending = row
            return
        if self.durability == EXIT and self._pending is not None and ending:
            self._pending = None
        if self.durability == ASYNC and not ending:
            self._enqueue(("checkpoint", row))
            return
        if self.durability == ASYNC:
            self._enqueue(("checkpoint", row))
            self.flush()
            return
        self._write_checkpoint(row)

    def _row(self, state: RunState, *, parent: Optional[str], source: str) -> dict:
        blob = json.dumps(state.to_checkpoint(), default=str)
        return {"id": state.checkpoint_id, "run": state.run_id, "thread": state.thread_id,
                "parent": parent, "ns": self.ns, "step": int(state.step),
                "source": "exit" if (self.durability == EXIT and source == "loop") else source,
                "phase": "tools" if state.batch else "", "status": state.status,
                "state": blob, "bytes": len(blob.encode("utf-8")), "mode": self.durability,
                "meta": json.dumps({"attempt": state.attempt, "lineage_id": state.lineage_id})}

    def _write_checkpoint(self, row: dict, *, own: bool = False) -> None:
        started = time.perf_counter()
        self._run_sql(lambda c: c.execute(_text("""
            INSERT INTO agent_checkpoints (checkpoint_id, run_id, thread_id, parent_checkpoint_id,
                ns, step, source, phase, status, state, state_bytes, durability, metadata)
            VALUES (:id, :run, :thread, :parent, :ns, :step, :source, :phase, :status,
                    CAST(:state AS JSONB), :bytes, :mode, CAST(:meta AS JSONB))
            ON CONFLICT (checkpoint_id) DO NOTHING"""), row), own=own)
        self.stats.append({"kind": "checkpoint", "bytes": row["bytes"], "mode": row["mode"],
                           "ms": (time.perf_counter() - started) * 1000.0})

    def load(self, run_id: str) -> Optional[RunState]:
        row = self._run_sql(lambda c: c.execute(_text(
            "SELECT checkpoint_id, state FROM agent_checkpoints WHERE run_id = :r AND ns = :ns "
            "ORDER BY seq DESC LIMIT 1"), {"r": run_id, "ns": self.ns}).first(), own=True)
        if row is None:
            return self.mirror.load(run_id) if self.mirror is not None else None
        data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        state = RunState.from_checkpoint(data)
        state.checkpoint_id = row[0]
        return state

    def get(self, checkpoint_id: str) -> Optional[RunState]:
        row = self._run_sql(lambda c: c.execute(_text(
            "SELECT state FROM agent_checkpoints WHERE checkpoint_id = :c"),
            {"c": checkpoint_id}).first(), own=True)
        if row is None:
            return None
        state = RunState.from_checkpoint(row[0] if isinstance(row[0], dict) else json.loads(row[0]))
        state.checkpoint_id = checkpoint_id
        return state

    def history(self, run_id: str) -> list:
        rows = self._run_sql(lambda c: c.execute(_text(
            "SELECT checkpoint_id, parent_checkpoint_id, step, source, phase, status, state_bytes,"
            " durability FROM agent_checkpoints WHERE run_id = :r ORDER BY seq"),
            {"r": run_id}).mappings().all(), own=True)
        return [dict(r) for r in rows]

    # ── pending writes ────────────────────────────────────────────────────────────────────
    def start_write(self, state: RunState, *, call: Any, idx: int, idempotency: str,
                    key: str) -> None:
        """A write tool is about to run: committed before it does, whatever the mode."""
        if self.durability == EXIT or not state.checkpoint_id:
            return
        self.flush()
        self._write_row({"ckpt": state.checkpoint_id, "call": call.id, "run": state.run_id,
                         "step": int(state.step), "idx": idx, "tool": call.name,
                         "idem": idempotency, "key": key, "status": "started",
                         "result": None, "rendered": None}, sync=True)

    def put_write(self, state: RunState, *, call: Any, idx: int, status: str, result: Any,
                  rendered: str, idempotency: Optional[str] = None,
                  key: Optional[str] = None) -> None:
        """A tool call's result, as it lands."""
        if self.durability == EXIT or not state.checkpoint_id:
            return
        self._write_row({"ckpt": state.checkpoint_id, "call": call.id, "run": state.run_id,
                         "step": int(state.step), "idx": idx, "tool": call.name,
                         "idem": idempotency, "key": key, "status": status,
                         "result": json.dumps(_jsonable(result)), "rendered": rendered},
                        sync=self.durability == SYNC)

    def _write_row(self, row: dict, *, sync: bool) -> None:
        if not sync:
            self._enqueue(("write", row))
            return
        started = time.perf_counter()
        self._run_sql(lambda c: self._upsert_write(c, row))
        self.stats.append({"kind": "write", "bytes": len(row.get("result") or "")
                           + len(row.get("rendered") or ""), "mode": self.durability,
                           "ms": (time.perf_counter() - started) * 1000.0})

    @staticmethod
    def _upsert_write(connection: Any, row: dict) -> None:
        connection.execute(_text("""
            INSERT INTO agent_checkpoint_writes (checkpoint_id, call_id, run_id, step, idx, tool,
                idempotency, idempotency_key, status, result, rendered)
            VALUES (:ckpt, :call, :run, :step, :idx, :tool, :idem, :key, :status,
                    CAST(:result AS JSONB), :rendered)
            ON CONFLICT (checkpoint_id, call_id) DO UPDATE SET
              status = EXCLUDED.status, result = EXCLUDED.result, rendered = EXCLUDED.rendered,
              idempotency = COALESCE(EXCLUDED.idempotency, agent_checkpoint_writes.idempotency),
              idempotency_key = COALESCE(EXCLUDED.idempotency_key,
                                         agent_checkpoint_writes.idempotency_key),
              updated_at = now()"""), row)

    def writes(self, checkpoint_id: str) -> dict:
        """The pending writes of a checkpoint's batch, by call id."""
        self.flush()
        rows = self._run_sql(lambda c: c.execute(_text(
            "SELECT call_id, tool, status, result, rendered, idempotency, idempotency_key "
            "FROM agent_checkpoint_writes WHERE checkpoint_id = :c ORDER BY idx"),
            {"c": checkpoint_id}).mappings().all(), own=True)
        return {str(r["call_id"]): dict(r) for r in rows}

    # ── the async writer ──────────────────────────────────────────────────────────────────
    def _enqueue(self, item: tuple) -> None:
        if self._queue is None:
            self._queue = queue.Queue()
            self._writer = threading.Thread(target=self._drain_queue, name="ckpt-writer",
                                            daemon=True)
            self._writer.start()
        self._queue.put(item)

    def _drain_queue(self) -> None:
        assert self._queue is not None
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                kind, row = item
                if kind == "checkpoint":
                    self._write_checkpoint(row, own=True)
                else:
                    started = time.perf_counter()
                    with self._engine().begin() as connection:
                        self._upsert_write(connection, row)
                    self.stats.append({"kind": "write", "mode": self.durability,
                                       "bytes": len(row.get("result") or ""),
                                       "ms": (time.perf_counter() - started) * 1000.0})
            except BaseException as error:  # noqa: BLE001 — surfaced on flush
                self._error = error
            finally:
                self._queue.task_done()

    def flush(self) -> None:
        """Every queued write is in the database (async), or the exit snapshot is (exit)."""
        if self._queue is not None:
            self._queue.join()
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        if self._pending is not None:
            row, self._pending = self._pending, None
            self._write_checkpoint(row)

    def close(self) -> None:
        self.flush()
        if self._queue is not None:
            self._queue.put(None)
            self._queue = None


def _postgres(bind: Any) -> bool:
    try:
        dialect = getattr(bind, "dialect", None) or getattr(getattr(bind, "engine", None),
                                                            "dialect", None)
        return getattr(dialect, "name", "") == "postgresql"
    except Exception:  # noqa: BLE001
        return False


def store_for(bind: Any, *, writes: bool = False, thread_id: Optional[str] = None,
              mirror: Any = None, commit: bool = False,
              fence: Optional[Callable[[Any], None]] = None) -> Any:
    """THE STORE EVERY kernel.run IS GIVEN: the Postgres checkpointer when the caller has a
    Postgres engine or connection — durability `sync` for a run that may write, `async` for a
    read-only one (§4.3) — else the caller's `mirror`, else None (the process store, for tests
    and callers with no database). A suspension then lives in Postgres, never only in the
    memory of the process that happened to run it."""
    if bind is not None and _postgres(bind):
        return PgCheckpointStore(bind, thread_id=thread_id, mirror=mirror, commit=commit,
                                 durability=SYNC if writes else ASYNC, fence=fence)
    return mirror


# ── Fork: a new branch from any checkpoint; the old one never changes ─────────────────────────

def fork(store: PgCheckpointStore, checkpoint_id: str, *, new_run_id: Optional[str] = None,
         update: Optional[Callable[[RunState], None]] = None,
         tool_results: Optional[dict] = None) -> RunState:
    """`fork(thread, checkpoint)`: the state at `checkpoint_id`, as a NEW run (`new_run_id`) on
    the same thread, written as a checkpoint whose parent is the one forked from (source
    'update'). `tool_results` ({call_id: content}) replaces what tools returned at that point —
    an operator replaying a failed run with the result it should have had — and `update` may
    change anything else. Nothing about the original checkpoint or run is touched. Returns the
    branch's state; `kernel.resume` continues it."""
    base = store.get(checkpoint_id)
    if base is None:
        raise LookupError(f"no checkpoint {checkpoint_id}")
    state = RunState.from_checkpoint(json.loads(json.dumps(base.to_checkpoint(), default=str)))
    state.run_id = new_run_id or f"krun_{uuid.uuid4().hex[:20]}"
    state.extra = {**state.extra, "forked_from": {"run_id": base.run_id,
                                                  "checkpoint_id": checkpoint_id}}
    for call_id, content in (tool_results or {}).items():
        body = content if isinstance(content, str) else json.dumps(content, default=str)
        for message in state.messages:
            if message.get("role") == "tool" and str(message.get("tool_call_id")) == call_id:
                message["content"] = body
        for entry in state.trace:
            if str(entry.get("call_id")) == call_id:
                entry["result"] = content
                entry["status"] = "ok"
                entry["edited"] = True
    if update is not None:
        update(state)
    # The branch goes on from here: whatever the source ended as, the fork is runnable.
    state.status = "running" if state.status in ("failed", "completed", "cancelled") \
        else state.status
    state.stopped = "" if state.status == "running" else state.stopped
    state.final_text = None if state.status == "running" else state.final_text
    state.checkpoint_id = checkpoint_id  # the parent of the branch's first checkpoint
    store.save(state, source="update")
    return state


def measure(bind: Any, state: RunState, *, steps: int = 20) -> dict:
    """State-blob size and write latency per durability mode, on a real database (the PR's
    numbers): `steps` checkpoints and one pending write each, per mode, on throwaway runs."""
    out: dict = {}
    for mode in MODES:
        store = PgCheckpointStore(bind, durability=mode)
        probe = RunState.from_checkpoint(state.to_checkpoint())
        probe.run_id = f"krun_measure_{mode}_{uuid.uuid4().hex[:8]}"
        probe.checkpoint_id = None
        started = time.perf_counter()
        for step in range(steps):
            probe.step = step + 1
            store.save(probe)
            from .types import ToolCall

            store.put_write(probe, call=ToolCall(id=f"c{step}", name="probe", arguments={}),
                            idx=0, status="ok", result={"ok": True, "n": step}, rendered="ok")
        loop_ms = (time.perf_counter() - started) * 1000.0
        probe.status = "completed"
        store.save(probe)
        store.close()
        checkpoints = [s for s in store.stats if s["kind"] == "checkpoint"]
        out[mode] = {
            "state_bytes": checkpoints[-1]["bytes"] if checkpoints else 0,
            "checkpoints_written": len(checkpoints),
            "loop_ms_per_step": round(loop_ms / steps, 3),
            "write_ms_p50": round(sorted(s["ms"] for s in store.stats)[len(store.stats) // 2], 3)
            if store.stats else 0.0,
        }
        with (bind.engine if hasattr(bind, "engine") and not hasattr(bind, "connect")
              else bind).begin() as c:
            c.execute(_text("DELETE FROM agent_checkpoints WHERE run_id = :r"), {"r": probe.run_id})
    return out


__all__ = ["ASYNC", "EXIT", "MODES", "PgCheckpointStore", "SYNC", "fork", "measure",
           "new_checkpoint_id", "store_for"]
