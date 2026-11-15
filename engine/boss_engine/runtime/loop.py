"""THE PULL LOOP (boss-agent-hosts-lld.md §3.13.2 "Internal callers reach replicas through the
pull loop"): a replica asks the run plane for its deployment's next admitted run, drives it, asks
again. Up to `slots` runs at once (the loop is I/O-bound, §2.6). SIGTERM sets `drain`: every run
stops at its next step boundary with a checkpoint and is handed back without spending an attempt
— the next replica resumes it."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

from .client import RunPlaneClient
from .runner import drive

log = logging.getLogger(__name__)


def serve(client: RunPlaneClient, *, slots: int = 4, poll_seconds: float = 1.0,
          drain: Optional[threading.Event] = None, once: bool = False,
          max_runs: Optional[int] = None, ttl: Optional[int] = None,
          on_result: Optional[Callable[[dict], None]] = None) -> int:
    """Serve until `drain` is set (or, with `once`, until nothing is left to take). Returns the
    number of runs driven."""
    drain = drain or threading.Event()
    driven = 0
    running: set = set()
    lock = threading.Lock()

    def finished(future: Future) -> None:
        with lock:
            running.discard(future)
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("a run's drive raised: %s", exc)
            return
        if on_result is not None:
            on_result(result)

    with ThreadPoolExecutor(max_workers=max(1, slots), thread_name_prefix="boss-run") as pool:
        while not drain.is_set():
            if max_runs is not None and driven >= max_runs:
                break
            with lock:
                full = len(running) >= slots
            if full:
                drain.wait(0.05)
                continue
            try:
                package = client.next(ttl=ttl)
            except Exception as exc:  # noqa: BLE001 — the control plane is away: retry
                log.warning("run plane next failed: %s", exc)
                package = None
            if package is None:
                with lock:
                    idle = not running
                if once and idle:
                    break
                drain.wait(poll_seconds)
                continue
            handle = client.run(package)
            future = pool.submit(drive, handle, package, drain=drain.is_set)
            with lock:
                running.add(future)
            future.add_done_callback(finished)
            driven += 1
    return driven


__all__ = ["serve"]
