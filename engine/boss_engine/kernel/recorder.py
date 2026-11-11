"""Every event a run produces, numbered and handed to the ledger and the caller.

The old loops' visibility was each its own: agent_loop emitted `agent.*` events, EngineAgent
called `on_step` with labels, the coder wrote agent_turns, the decision loop wrote nothing. A
kernel run records ONE stream: kept on the result, written through the sink (kernel/ledger.py's
DbSink in production, nothing in a unit test), and passed to `on_event` so a caller maps it to
its own labels or events. A sink or callback that fails is logged; it never fails the run.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from .ledger import SinkMeta
from .state import RunState
from .types import Event, Usage, now

log = logging.getLogger("boss.kernel")


class Recorder:
    def __init__(self, state: RunState, *, sink: Any = None,
                 on_event: Optional[Callable[[Event], None]] = None,
                 policy: Optional[Mapping[str, str]] = None, meta: Optional[SinkMeta] = None):
        self.state = state
        self.sink = sink
        self.on_event = on_event
        self.policy = dict(policy or {})
        self.meta = meta or SinkMeta()
        self.events: list = []

    def _seq(self) -> int:
        seq = None
        if self.sink is not None and hasattr(self.sink, "next_seq"):
            seq = self.sink.next_seq()
        if seq is None:
            seq = self.state.seq + 1
        self.state.seq = max(self.state.seq, int(seq))
        return int(seq)

    def emit(self, kind: str, data: Optional[Mapping[str, Any]] = None, *,
             usage: Optional[Usage] = None, latency_ms: int = 0,
             workspace_ref: Optional[str] = None) -> Event:
        event = Event(kind=kind, run_id=self.state.run_id, seq=self._seq(), step=self.state.step,
                      at=now(), data=dict(data or {}), act_id=self.state.act_id, usage=usage,
                      latency_ms=int(latency_ms or 0), policy=self.policy,
                      workspace_ref=workspace_ref)
        self.events.append(event)
        if self.sink is not None:
            try:
                self.sink.write(event, self.state, self.meta)
            except Exception as exc:  # noqa: BLE001 — the ledger never stops the work
                log.warning("kernel: event %s not recorded (non-fatal): %s", kind, exc)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception as exc:  # noqa: BLE001 — nor does a caller's progress callback
                log.warning("kernel: on_event failed for %s (non-fatal): %s", kind, exc)
        return event


__all__ = ["Recorder"]
