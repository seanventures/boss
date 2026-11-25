"""THE LOCAL WORKER'S LONG-POLL (P9a-5, BOSSAPP-1097; boss-eventing-lld.md §8.2 "Local (4a)").

    worker → POST /v1/worker/poll {capabilities, wait_ms: 15000}
    API:  claim_job(target local) → found? return leased
          else wait on this process's hub for a `job.enqueued` of one of those kinds (≤ wait_ms)
          on wake → claim again (SKIP LOCKED: several waiters race, one wins, the others re-wait)

The wait is an asyncio wait on the request's event loop — NO THREAD IS HELD and NO WAITER
QUERIES ANYTHING ON ITS OWN. Postgres announces every queued job on the `boss_jobs` channel
(db/schema.sql `boss_jobs_notify`: kind, target, project) at commit; one `PgListener` per API
process feeds a hub; a waiter wakes when a note names a kind it can run and a local target.

The safety net for a lost NOTIFY (a listener reconnect) is ONE sweep per API process every
`SWEEP_SECONDS`: a single `SELECT DISTINCT kind FROM jobs WHERE …` over every waited-on kind,
whatever the number of waiters — never a per-waiter tick (run_plane._HostWaiters is the same
shape for the microVM hosts). Worst-case added latency after a lost NOTIFY: 5 s.

Version skew: an older worker sends no `wait_ms` and gets the old answer at once; an older API
ignores `wait_ms` and answers `idle` without `long_poll: true`, so a new worker falls back to
its `poll_interval_seconds` sleep (apps/worker/boss_worker/main.py).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy import text

logger = logging.getLogger("boss.worker.longpoll")

WAIT_MAX_MS = 25_000
SWEEP_SECONDS = 5.0
CHANNEL = "boss_jobs"
LOCAL_TARGETS = (None, "local")


def is_local_target(target: Any) -> bool:
    return target in LOCAL_TARGETS


class _LocalWaiters:
    """Every /v1/worker/poll waiting on this process, keyed by the kinds it can run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set = set()
        self._started = False
        self.sweeps = 0
        self.wakes = 0
        self.listener_ready = threading.Event()

    def start(self, engine: Any) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            from .event_stream import Hub, PgListener, libpq_dsn

            if not str(getattr(engine, "dialect", None) and engine.dialect.name or "").startswith(
                    "postgres"):
                raise RuntimeError("NOTIFY needs Postgres; sweeping only")
            hub = Hub()
            hub.listen(_Any(), self._on_note)
            listener = PgListener(libpq_dsn(engine), hub, channel=CHANNEL, poll=1.0)
            listener.start()
            self._listener = listener
        except Exception:  # noqa: BLE001 — without the hub the sweep still wakes everyone
            logger.warning("worker long-poll: NOTIFY listener not started; sweeping only",
                           exc_info=True)
        threading.Thread(target=self._sweep_loop, args=(engine,), name="worker-longpoll-sweep",
                         daemon=True).start()

    def register(self, engine: Any, kinds: set) -> Any:
        from .run_plane import Waiter

        self.start(engine)
        waiter = Waiter(set(kinds))
        with self._lock:
            self._waiters.add(waiter)
        return waiter

    def unregister(self, waiter: Any) -> None:
        with self._lock:
            self._waiters.discard(waiter)

    def count(self) -> int:
        with self._lock:
            return len(self._waiters)

    def wanted(self) -> list:
        with self._lock:
            return sorted({k for w in self._waiters for k in w.targets})

    # waking
    def _on_note(self, note: dict) -> None:
        if note.get("__resync__"):
            self.wake_all()
            return
        if not is_local_target(note.get("target")):
            return
        kind = note.get("kind")
        if kind:
            self.wake({str(kind)})

    def wake(self, kinds: set) -> None:
        with self._lock:
            hit = [w for w in self._waiters if w.targets & kinds]
        for waiter in hit:
            self.wakes += 1
            waiter.set()

    def wake_all(self) -> None:
        with self._lock:
            hit = list(self._waiters)
        for waiter in hit:
            waiter.set()

    # the one sweep per process
    def sweep(self, engine: Any) -> list:
        from ..db import background

        kinds = self.wanted()
        if not kinds:
            return []
        with background():
            with engine.connect() as c:
                found = [str(r[0]) for r in c.execute(text("""
                    SELECT DISTINCT kind FROM jobs
                     WHERE (target IS NULL OR target = 'local') AND kind = ANY(:k)
                       AND run_after <= now()
                       AND (status IN ('queued', 'failed_retryable')
                            OR (status IN ('leased', 'running') AND lease_expires_at <= now()))"""),
                    {"k": kinds}).all()]
        self.sweeps += 1
        if found:
            self.wake(set(found))
        return found

    def _sweep_loop(self, engine: Any) -> None:
        while True:
            time.sleep(SWEEP_SECONDS)
            try:
                self.sweep(engine)
            except Exception:  # noqa: BLE001 — the next sweep tries again
                pass


class _Any:
    def matches(self, note: dict) -> bool:
        return True


WAITERS = _LocalWaiters()


async def poll(engine: Any, *, claim: Callable[[], Optional[dict]], capabilities: list,
               wait_ms: Any = 0) -> dict:
    """Claim now; nothing → an asyncio wait (≤ wait_ms) for a job of one of `capabilities`,
    then claim again, until the deadline. The waiter is registered BEFORE the first claim, so a
    job committed between the two is never missed. The claim runs in the threadpool."""
    from starlette.concurrency import run_in_threadpool

    wait = max(0, min(int(wait_ms or 0), WAIT_MAX_MS)) / 1000.0
    started = time.monotonic()
    deadline = started + wait
    waiter = WAITERS.register(engine, set(capabilities)) if wait and capabilities else None
    try:
        while True:
            if waiter is not None:
                waiter.clear()
            job = await run_in_threadpool(claim)
            waited = int((time.monotonic() - started) * 1000)
            if job:
                return {"status": "leased", "job": job, "long_poll": waiter is not None,
                        "waited_ms": waited}
            left = deadline - time.monotonic()
            if waiter is None or left <= 0:
                return {"status": "idle", "job": None, "long_poll": waiter is not None,
                        "waited_ms": waited}
            await waiter.wait(left)
    finally:
        if waiter is not None:
            WAITERS.unregister(waiter)


__all__ = ["CHANNEL", "SWEEP_SECONDS", "WAITERS", "WAIT_MAX_MS", "is_local_target", "poll"]
