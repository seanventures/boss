"""boss-runtime: drive one leased run on the P1 kernel against the run plane (P8a,
boss-agent-hosts-lld.md §3.13.1).

`drive` is agent_run_worker.kernel_runner + agent_run_worker._settle's caller, with the database
replaced by the run plane: the same RunSpec fields (persist, store, thread, drain, interrupts,
cancel), the same run-or-resume choice, the same outcome shape — so a run behaves the same on a
replica as on a local worker (P8a-T3).

HOW A RUN IS BUILT IN THE VM. `RUNNERS` maps finalize_ctx["runner"] to
`build(package, handle) -> kernel.RunSpec` (engine, tools, policies, system, messages). The
`deployed_agent` runner compiles the run's PINNED agent version from the package (no database)
and offers the agent's BOSS-state tools through `RemoteToolSet` (they run on the control plane,
D1a). Its model engine comes from `ENGINE_FACTORY`: the central model gateway reached over vsock
lands in P8b (S10), so until then a VM refuses a deployed run it has no engine for, in words.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any, Callable, Optional

from .client import Fenced, RunHandle, RunPlaneHTTPError
from .remote import (RemoteCheckpointStore, RemoteGate, RemoteInterrupts, RemoteSink,
                     RemoteToolSet)

log = logging.getLogger(__name__)

#: finalize_ctx["runner"] -> build(package, handle) -> RunSpec. Tests register scripted ones.
RUNNERS: dict = {}

#: (package, handle) -> a kernel engine for a deployed agent's model calls. P8b points it at the
#: central model gateway over vsock; until then None refuses the run with a visible reason.
ENGINE_FACTORY: Optional[Callable[[dict, RunHandle], Any]] = None

#: The lease heartbeat while a step runs (the lease is 30 s; LLD §3.14.8).
HEARTBEAT_SECONDS = 10.0


def _build_deployed(package: dict, handle: RunHandle) -> Any:
    from .. import kernel
    from ..agents import spec as S
    from ..kernel import presets
    from ..kernel.agent_spec import policies_for, run_fields

    agent = package.get("agent") or {}
    if not agent.get("spec"):
        raise RuntimeError("the run plane sent no agent version for a deployed run")
    if ENGINE_FACTORY is None:
        raise RuntimeError("this VM has no model engine yet: deployed agents on microVMs reach "
                           "models through the central model gateway, which lands in P8b")
    compiled = S.compile_spec(S.load_spec(agent["spec"]))
    fields = run_fields(compiled)
    steps = int(compiled.limits.max_steps or 20)
    run = package["run"]
    fctx = dict(run.get("finalize_ctx") or {})
    return kernel.RunSpec(
        engine=ENGINE_FACTORY(package, handle), tools=RemoteToolSet(handle),
        policies=policies_for(presets.agent_loop(steps), compiled),
        project_id=str(run["project_id"]), system=agent.get("compiled_prompt") or compiled.prompt,
        messages=[m for m in (run.get("messages") or []) if m.get("role") != "system"],
        actor=fields["actor"], gate=RemoteGate(handle), sink=RemoteSink(handle),
        store=None, persist=True,
        extra={**fields["extra"], "agent_version": agent.get("version"),
               "deployment_id": fctx.get("deployment_id"),
               "environment": fctx.get("environment"),
               **({"traceparent": fctx["traceparent"]} if fctx.get("traceparent") else {})})


RUNNERS["deployed_agent"] = _build_deployed


class _Beat:
    """Renews the lease while the run drives (a long tool call must not lose it)."""

    def __init__(self, handle: RunHandle, every: float):
        self.handle, self.every = handle, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="run-lease-beat", daemon=True)

    def __enter__(self) -> "_Beat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                self.handle.heartbeat()
            except Fenced:
                return
            except Exception as exc:  # noqa: BLE001 — the next beat or write tells
                log.warning("lease heartbeat for %s failed: %s", self.handle.run_id, exc)


def drive(handle: RunHandle, package: dict, *, drain: Optional[Callable[[], bool]] = None,
          heartbeat_seconds: float = HEARTBEAT_SECONDS) -> dict:
    """Drive one leased run to its outcome and settle it on the run plane. Returns the
    settlement, or {"fenced": True} when another executor holds the run now (nothing more is
    written: the fence refused it)."""
    from .. import kernel

    run = package["run"]
    fctx = dict(run.get("finalize_ctx") or {})
    name = str(fctx.get("runner") or "deployed_agent")
    try:
        build = RUNNERS.get(name)
        if build is None:
            raise RuntimeError(f"no runtime runner {name!r} for agent run {run['id']}")
        store = RemoteCheckpointStore(handle, thread_id=fctx.get("thread_id"))
        spec = dataclasses.replace(
            build(package, handle), run_id=str(run["id"]), project_id=str(run["project_id"]),
            persist=True, store=store, thread_id=fctx.get("thread_id"), drain=drain,
            interrupts=RemoteInterrupts(handle),
            cancel=kernel.CancelToken(should_stop=handle.should_stop)
            if fctx.get("thread_id") else None)
        with _Beat(handle, heartbeat_seconds):
            if store.load(str(run["id"])) is None:
                result = kernel.run(spec)
            else:
                answers = package.get("answers") or handle.answers()
                result = kernel.resume(spec, answers=answers or None)
            store.close()
    except Fenced:
        log.info("run %s: fenced — another executor holds it now", run["id"])
        return {"fenced": True, "run_id": run["id"]}
    except Exception as exc:  # noqa: BLE001 — recorded on the run, as on the worker
        try:
            return handle.fail(f"{type(exc).__name__}: {exc}"[:2000])
        except Fenced:
            return {"fenced": True, "run_id": run["id"]}
    outcome = {"status": result.status, "run_id": result.run_id, "output_text": result.text,
               "error": result.error, "stopped": result.stopped}
    try:
        return handle.settle(outcome)
    except Fenced:
        return {"fenced": True, "run_id": run["id"]}
    except RunPlaneHTTPError:
        raise


__all__ = ["ENGINE_FACTORY", "HEARTBEAT_SECONDS", "RUNNERS", "drive"]
