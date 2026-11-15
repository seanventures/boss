"""The shared Agent Protocol + A2A handlers, served FROM A REPLICA (P8b-9, BOSSAPP-1076;
boss-agent-hosts-lld.md §3.13.2, §3.17.3).

`RunPlaneBackend` is protocols/agent_serving's Backend over the run plane: every request becomes
`POST /v1/run-plane/admit` with the caller's forwarded credential (the PDP runs at BOSS on every
op: invoke, thread, read, cancel, answer), and a run BOSS leases to this replica is driven HERE,
in one of the VM's slots (the work inbox's), with its checkpoints tapped so the SSE stream goes
back on the caller's own connection. A run whose thread is busy on another replica is answered
`pending` (forwarding through the hosts is P8b-T3's; events tail through `read` until then).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Iterator, Optional

from ..protocols.agent_serving import (Caller, ServingError, checkpoint_events, final_events,
                                       interrupt_events, metadata_event, stopped)
from .client import RunPlaneClient, RunPlaneHTTPError

log = logging.getLogger(__name__)

_DONE = object()


class _LocalRun:
    """A run this VM drives: its tapped checkpoints, until the drive ends."""

    def __init__(self) -> None:
        self.rows: "queue.Queue[Any]" = queue.Queue(maxsize=10_000)
        self.done = threading.Event()
        self.result: Optional[dict] = None

    def tap(self, row: dict) -> None:
        try:
            self.rows.put_nowait(row)
        except queue.Full:  # a stream nobody reads never holds the run up
            pass

    def finish(self, result: Optional[dict]) -> None:
        self.result = result
        self.done.set()
        try:
            self.rows.put_nowait(_DONE)
        except queue.Full:
            pass


class RunPlaneBackend:
    """protocols/agent_serving.Backend for a replica (module doc)."""

    POLL_SECONDS = 0.5

    def __init__(self, client: RunPlaneClient, *, card: Optional[Callable[[], Optional[dict]]] = None,
                 start_local: Optional[Callable[[str, Callable[[], Any]], Optional[str]]] = None,
                 drive: Optional[Callable[..., dict]] = None):
        from .runner import drive as default_drive

        self.client = client
        self._card = card
        self._start_local = start_local
        self._drive = drive or default_drive
        self._runs: dict = {}
        self._lock = threading.Lock()

    # every call is the PDP's (admit) ─────────────────────────────────────────────────────

    def _admit(self, caller: Caller, **kw: Any) -> dict:
        try:
            return self.client.admit(caller=caller.authorization,
                                     client_cert=caller.client_cert_pem,
                                     traceparent=caller.traceparent, **kw)
        except RunPlaneHTTPError as exc:
            detail = exc.body if isinstance(exc.body, dict) else {}
            raise ServingError(exc.status, str(exc.code or "refused"),
                               str(detail.get("message") or exc.code or "refused")) from exc

    def card(self, caller: Caller) -> Optional[dict]:
        return self._card() if self._card is not None else None

    def create_thread(self, caller: Caller, *, metadata: Optional[dict]) -> dict:
        return self._admit(caller, op="thread", metadata=metadata)["thread"]

    def start_run(self, caller: Caller, *, thread_id: Optional[str], input: Any,
                  double_text: Optional[str], surface: str) -> dict:
        out = self._admit(caller, op="invoke", thread_id=thread_id, input=input,
                          double_text=double_text, surface=surface)
        run_id = str(out["run_id"])
        if out.get("status") == "leased":
            self._drive_here(run_id, out)
            status = "running"
        else:
            status = "pending"
        return {"run_id": run_id, "thread_id": out.get("thread_id"), "status": status,
                "held_by": out.get("held_by")}

    def _drive_here(self, run_id: str, package: dict) -> None:
        handle = self.client.run(package)
        local = _LocalRun()
        with self._lock:
            self._runs[run_id] = local

        def target() -> None:
            result: Optional[dict] = None
            try:
                result = self._drive(handle, package, tap=local.tap)
            except Exception as exc:  # noqa: BLE001 — the lease expires; another replica resumes
                log.warning("run %s could not be driven here: %s", run_id, exc)
            finally:
                local.finish(result)

        refused = self._start_local(run_id, target) if self._start_local else None
        if self._start_local is None:
            threading.Thread(target=target, daemon=True, name=f"run-{run_id[-8:]}").start()
        if refused:
            with self._lock:
                self._runs.pop(run_id, None)
            try:
                handle.hand_back()
            except Exception:  # noqa: BLE001 — the lease expires anyway
                log.debug("hand back of %s failed", run_id, exc_info=True)
            raise ServingError(503, refused, "this replica has no free slot; retry")

    def view(self, caller: Caller, run_id: str, *, surface: str) -> dict:
        return self._admit(caller, op="read", run_id=run_id, surface=surface)["view"]

    def cancel(self, caller: Caller, run_id: str, *, surface: str) -> dict:
        return self._admit(caller, op="cancel", run_id=run_id, surface=surface)["run"]

    def answer(self, caller: Caller, run_id: str, answers: list, *, surface: str) -> None:
        self._admit(caller, op="answer", run_id=run_id, answers=answers, surface=surface)

    def events(self, caller: Caller, run_id: str, *, timeout: float,
               surface: str) -> Iterator[dict]:
        """The run's typed events. Authorised once up front (a `read` at admit), then streamed
        from this VM's own drive when it runs here, else tailed through `read`."""
        run = self.view(caller, run_id, surface=surface)["run"]
        return self._events(caller, run_id, run, timeout=timeout, surface=surface)

    def _events(self, caller: Caller, run_id: str, run: dict, *, timeout: float,
                surface: str) -> Iterator[dict]:
        deadline = time.monotonic() + max(0.0, timeout)
        announced: set = set()
        yield metadata_event(run)
        with self._lock:
            local = self._runs.get(run_id)
        seen = 0
        if local is not None:
            while time.monotonic() < deadline:
                try:
                    row = local.rows.get(timeout=min(1.0, max(0.05, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if row is _DONE:
                    break
                state = row.get("state")
                if isinstance(state, str):
                    try:
                        state = json.loads(state)
                    except ValueError:
                        state = {}
                cp = {"checkpoint_id": row.get("id"), "step": row.get("step"),
                      "phase": row.get("phase"), "status": row.get("status"),
                      "source": row.get("source")}
                events, seen = checkpoint_events(cp, state or {}, seen)
                yield from events
            with self._lock:
                if local.done.is_set():
                    self._runs.pop(run_id, None)
        while True:
            run = self.view(caller, run_id, surface=surface)["run"]
            yield from interrupt_events(run, announced)
            if stopped(run):
                yield from final_events(run)
                return
            if time.monotonic() >= deadline:
                yield from final_events(run, timed_out=True)
                return
            time.sleep(self.POLL_SECONDS)


__all__ = ["RunPlaneBackend"]
