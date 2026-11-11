"""Where a run's state is kept between steps, so it can stop and go on (design §2.7).

A durable run (`persist=True`) is saved after every step, as agent_loop saves agent_runs today;
any run is saved when it suspends, waits for the budget or is cancelled, because a suspension
that is not written down cannot be resumed.

`AgentRunsStore` writes the agent_runs row agent_loop writes, in the same shape (messages with
the system message first, transcript, usage, step, pending, output_text), so the console and
resume read a kernel run exactly as they read an old one; the kernel's own counters ride in
finalize_ctx["kernel"]. `MemoryStore` keeps them in the process (tests, chat turns).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional, Protocol

from .state import RunState


class CheckpointStore(Protocol):
    def save(self, state: RunState) -> None: ...

    def load(self, run_id: str) -> Optional[RunState]: ...


class MemoryStore:
    def __init__(self) -> None:
        self._runs: dict = {}
        self._lock = threading.Lock()

    def save(self, state: RunState) -> None:
        with self._lock:
            self._runs[state.run_id] = json.loads(json.dumps(state.to_checkpoint(), default=str))

    def load(self, run_id: str) -> Optional[RunState]:
        with self._lock:
            data = self._runs.get(run_id)
        return RunState.from_checkpoint(data) if data is not None else None


#: Suspensions of runs that named no store survive here, in this process, until resumed.
DEFAULT_STORE = MemoryStore()


class AgentRunsStore:
    """agent_runs, written on the caller's connection (the same transaction as the step, as
    `_save_run` does). `task_id` is the run's task; `finalize_ctx` the caller's own context."""

    def __init__(self, connection: Any, *, task_id: Optional[str] = None,
                 finalize_ctx: Optional[dict] = None):
        self.connection = connection
        self.task_id = task_id
        self.finalize_ctx = dict(finalize_ctx or {})

    def save(self, state: RunState) -> None:
        from sqlalchemy import text

        kernel = state.to_checkpoint()
        kernel.pop("messages", None)
        usage = state.usage.as_adapter_usage()
        self.connection.execute(text("""
            INSERT INTO agent_runs (id, project_id, task_id, status, messages, transcript, usage,
                                    finalize_ctx, step, pending, output_text)
            VALUES (:id, :p, :t, :status, CAST(:messages AS JSONB), CAST(:transcript AS JSONB),
                    CAST(:usage AS JSONB), CAST(:fctx AS JSONB), :step, CAST(:pending AS JSONB),
                    :output)
            ON CONFLICT (id) DO UPDATE SET
              status = EXCLUDED.status, messages = EXCLUDED.messages,
              transcript = EXCLUDED.transcript, usage = EXCLUDED.usage,
              finalize_ctx = EXCLUDED.finalize_ctx, step = EXCLUDED.step,
              pending = EXCLUDED.pending, output_text = EXCLUDED.output_text, updated_at = now()
        """), {
            "id": state.run_id, "p": state.project_id, "t": self.task_id, "status": state.status,
            "messages": json.dumps(state.openai_messages(), default=str),
            "transcript": json.dumps(state.trace, default=str),
            "usage": json.dumps(usage), "step": int(state.step),
            "fctx": json.dumps({**self.finalize_ctx, "kernel": kernel}, default=str),
            "pending": json.dumps(state.pending, default=str) if state.pending else None,
            "output": state.final_text,
        })

    def load(self, run_id: str) -> Optional[RunState]:
        from sqlalchemy import text

        row = self.connection.execute(text(
            "SELECT messages, finalize_ctx FROM agent_runs WHERE id = :id"), {"id": run_id}
        ).first()
        if row is None:
            return None
        fctx = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
        kernel = dict(fctx.get("kernel") or {})
        if not kernel:
            return None  # an old loop's run: K4 resumes those through agent_loop until it moves
        messages = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages = messages[1:]
        kernel["messages"] = messages
        self.finalize_ctx = {k: v for k, v in fctx.items() if k != "kernel"}
        return RunState.from_checkpoint(kernel)


__all__ = ["AgentRunsStore", "CheckpointStore", "DEFAULT_STORE", "MemoryStore"]
