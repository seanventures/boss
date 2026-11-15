"""The kernel's ports, bound to the run plane instead of a database (boss-agent-hosts-lld.md §2.7:
"RemoteCheckpointStore keeps the CheckpointStore interface, so the kernel cannot tell the
difference").

  RemoteCheckpointStore   PgCheckpointStore with its two write primitives and its reads sent to
                          the run plane: durability modes, the async writer, the exit snapshot
                          and the synchronous 'started' marker are the SAME code
  RemoteSink              the ledger (DbSink) and the trace spans (DbSpanSink), remotely
  RemoteGate              the budget gate: reserve / settle / release one model turn
  RemoteInterrupts        an interrupt becomes a Decision on the control plane
  RemoteToolSet           BOSS-state tools run on the control plane (D1a)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..kernel.durable import SYNC, PgCheckpointStore
from ..kernel.state import RunState
from . import wire
from .client import Fenced, RunHandle, RunPlaneHTTPError

log = logging.getLogger(__name__)


class RemoteCheckpointStore(PgCheckpointStore):
    """The run's checkpoints over the run plane; every write fenced there (module doc)."""

    durable = True

    def __init__(self, handle: RunHandle, *, thread_id: Optional[str] = None, ns: str = "",
                 durability: str = SYNC, tap: Optional[Any] = None):
        super().__init__(None, thread_id=thread_id, ns=ns, durability=durability)
        self.handle = handle
        #: Called with each committed checkpoint row: the in-VM listener streams a run it drives
        #: from these, on the caller's own connection (P8b-9).
        self.tap = tap

    def _put_checkpoint(self, row: dict, *, own: bool = False) -> None:
        self.handle.call("PUT", "checkpoints", json={"row": row})
        if self.tap is not None:
            try:
                self.tap(row)
            except Exception:  # noqa: BLE001 — a slow or gone listener never fails the run
                log.debug("checkpoint tap failed", exc_info=True)

    def _put_write(self, row: dict, *, own: bool = False) -> None:
        self.handle.call("PUT", "checkpoint-writes", json={"row": row})

    def _state(self, found: Optional[dict]) -> Optional[RunState]:
        if not found:
            return None
        data = found["state"] if isinstance(found["state"], dict) else json.loads(found["state"])
        state = RunState.from_checkpoint(data)
        state.checkpoint_id = found["checkpoint_id"]
        return state

    def load(self, run_id: str) -> Optional[RunState]:
        self._own(run_id)
        return self._state(self.handle.call("GET", "checkpoints",
                                            params={"ns": self.ns}).get("checkpoint"))

    def get(self, checkpoint_id: str) -> Optional[RunState]:
        return self._state(self.handle.call("GET", "checkpoints", params={
            "checkpoint_id": checkpoint_id}).get("checkpoint"))

    def history(self, run_id: str) -> list:
        self._own(run_id)
        return list(self.handle.call("GET", "checkpoints", params={"history": "true"})
                    .get("history") or [])

    def writes(self, checkpoint_id: str) -> dict:
        self.flush()
        return dict(self.handle.call("GET", "checkpoint-writes", params={
            "checkpoint_id": checkpoint_id}).get("writes") or {})

    def _own(self, run_id: str) -> None:
        if run_id != self.handle.run_id:
            raise ValueError("a run plane store reads only its own run")


class RemoteSink:
    """The run's ledger events and trace spans, written on the control plane under the fence.
    Like DbSink, a ledger write that fails is logged, never raised into the run."""

    def __init__(self, handle: RunHandle):
        self.handle = handle

    def next_seq(self) -> Optional[int]:
        try:
            return int(self.handle.call("POST", "ledger/seq", json={}).get("seq"))
        except Exception as exc:  # noqa: BLE001 — a run without a seq still runs
            log.warning("run plane ledger: no sequence (non-fatal): %s", exc)
            return None

    def write(self, event: Any, state: Any, meta: Any) -> None:
        from ..kernel.ledger import DbSink

        if event.kind in DbSink.SKIP:
            return
        try:
            self.handle.call("POST", "ledger", json={
                "event": wire.event_out(event), "meta": wire.meta_out(meta),
                "state": {"project_id": state.project_id, "act_id": state.act_id,
                          "attempt": state.attempt, "lineage_id": state.lineage_id}})
        except Exception as exc:  # noqa: BLE001
            log.warning("run plane ledger: %s not written (non-fatal): %s", event.kind, exc)

    def write_span(self, span: Any) -> None:
        try:
            self.handle.call("POST", "spans", json={"span": wire.span_out(span)})
        except Exception as exc:  # noqa: BLE001
            log.warning("run plane trace: span not written (non-fatal): %s", exc)


class RemoteGate:
    """kernel.governance.BudgetGate over the run plane."""

    def __init__(self, handle: RunHandle, *, skill_id: str = "model.agent_turn"):
        self.handle, self.skill_id = handle, skill_id

    def open(self) -> Any:
        from ..services.model_routing import ModelSpendDeferred, ModelSpendRefused

        try:
            return tuple(self.handle.call("POST", "budget/reserve",
                                          json={"skill_id": self.skill_id})["held"])
        except RunPlaneHTTPError as exc:
            message = (exc.body or {}).get("message") if isinstance(exc.body, dict) else None
            if exc.code == "spend_deferred":
                raise ModelSpendDeferred(message or "waiting for the budget",
                                         retry_after_seconds=(exc.body or {}).get(
                                             "retry_after_seconds")) from exc
            if exc.code == "spend_refused":
                raise ModelSpendRefused(message or "budget cap") from exc
            raise

    def settle(self, held: Any, usage: dict) -> None:
        self.handle.call("POST", "budget/settle", json={"held": list(held), "usage": usage,
                                                       "skill_id": self.skill_id})

    def fail(self, held: Any, error: BaseException) -> None:
        from ..services.model_routing import not_dispatched

        self.handle.call("POST", "budget/settle", json={
            "held": list(held), "skill_id": self.skill_id,
            "failed": {"message": str(error)[:500] or type(error).__name__,
                       "not_dispatched": not_dispatched(error)}})


class RemoteInterrupts:
    """RunSpec.interrupts: `open(state, interrupt) -> decision_id` on the control plane."""

    def __init__(self, handle: RunHandle):
        self.handle = handle

    def open(self, state: Any, item: Any) -> str:
        return str(self.handle.call("POST", "interrupts",
                                    json={"interrupt": wire.interrupt_out(item)})["decision_id"])


class RemoteToolSet:
    """The run's BOSS-state tools: their schemas and every call answered by the control plane,
    under the run's delegated authority (D1a). A fenced call raises: the run is not ours."""

    def __init__(self, handle: RunHandle):
        self.handle = handle
        self._schemas: Optional[list] = None

    def schemas(self, *, step: int, state: RunState) -> list:
        if self._schemas is None:
            self._schemas = [wire.schema_in(s) for s in
                             self.handle.call("GET", "tools").get("tools") or []]
        return list(self._schemas)

    def invoke(self, call: Any, *, state: RunState, approved: Optional[dict] = None) -> Any:
        from ..kernel.toolset import failed

        try:
            out = self.handle.call("POST", f"tools/{call.name}/invoke", json={
                "call": {"id": call.id, "arguments": call.arguments,
                         "raw_arguments": call.raw_arguments}, "approved": approved})
        except Fenced:
            raise
        except RunPlaneHTTPError as exc:
            return failed(call, exc)
        return wire.result_in(out["result"])


__all__ = ["RemoteCheckpointStore", "RemoteGate", "RemoteInterrupts", "RemoteSink",
           "RemoteToolSet"]
