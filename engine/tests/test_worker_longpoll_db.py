"""The local worker's long-poll (P9a-5, BOSSAPP-1097; LLD §8.2 "Local (4a)", §14 T9).

A waiting poll wakes within milliseconds of a job's commit through the `boss_jobs` NOTIFY and
this process's hub; nothing polls Postgres while idle (one 5 s sweep per process); a job for a
microVM deployment never wakes or reaches a local waiter; a lost NOTIFY is covered by the sweep.
T9: event received at the front door → run's job leased by the long-poll, measured end to end
through the real processor thread (NOTIFY boss_intake → route → admit → NOTIFY boss_jobs →
claim); p95 ≤ 550 ms. The kernel start (the worker driving the run) needs a model and is not
part of the bench; it is the same step the plain poll path already measures."""
from __future__ import annotations

import asyncio
import statistics
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import event_intake, jobs, worker_longpoll

from ._intake_support import (
    deploy_local_agent,
    make_connection,
    make_project,
    make_route,
    pg_engine,
    purge,
    standard_webhook_request,
    token_of,
)


@pytest.fixture()
def world():
    engine = pg_engine()
    pid = make_project(engine)
    event_intake.BUCKETS.reset()
    try:
        yield engine, pid
    finally:
        purge(engine, pid)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _service():
    return jobs.JobLeaseService(_settings())


def _claimer(engine, kinds: list[str], worker: str = "w-longpoll"):
    service = _service()

    def claim():
        with engine.begin() as c:
            return service.claim_job(c, worker, kinds, 60)
    return claim


def _enqueue_later(engine, pid: str, kind: str, delay: float, *, target=None) -> threading.Thread:
    def go():
        time.sleep(delay)
        with engine.begin() as c:
            job = jobs.enqueue_job(c, pid, kind, {"n": 1}, priority=50)
            if target:
                c.execute(text("UPDATE jobs SET target = :t WHERE id = :j"),
                          {"t": target, "j": job["id"]})
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _wait_listener(timeout: float = 5.0) -> None:
    listener = getattr(worker_longpoll.WAITERS, "_listener", None)
    if listener is not None:
        listener.ready.wait(timeout)


def test_a_waiting_poll_wakes_on_the_jobs_notify_within_a_second(world):
    engine, pid = world
    kind = f"test.longpoll.{uuid.uuid4().hex[:6]}"
    worker_longpoll.WAITERS.start(engine)
    _wait_listener()
    started = time.monotonic()
    _enqueue_later(engine, pid, kind, 0.3)
    got = asyncio.run(worker_longpoll.poll(engine, claim=_claimer(engine, [kind]),
                                           capabilities=[kind], wait_ms=5000))
    elapsed = time.monotonic() - started
    assert got["status"] == "leased" and got["job"]["kind"] == kind and got["long_poll"] is True
    assert 0.25 < elapsed < 1.5, f"woke in {elapsed:.3f}s (NOTIFY, not the 5 s sweep)"
    assert got["waited_ms"] >= 250
    assert worker_longpoll.WAITERS.count() == 0, "the waiter is gone after the answer"


def test_an_idle_poll_returns_at_the_deadline_without_querying(world, monkeypatch):
    engine, pid = world
    kind = f"test.longpoll.{uuid.uuid4().hex[:6]}"
    calls = {"n": 0}
    base = _claimer(engine, [kind])

    def counting():
        calls["n"] += 1
        return base()

    started = time.monotonic()
    got = asyncio.run(worker_longpoll.poll(engine, claim=counting, capabilities=[kind], wait_ms=700))
    assert got["status"] == "idle" and got["long_poll"] is True and got["job"] is None
    assert 0.65 <= time.monotonic() - started < 1.5
    assert calls["n"] <= 2, "one claim before the wait and at most one after: no polling"


def test_a_microvm_deployments_job_never_wakes_or_reaches_a_local_waiter(world):
    engine, pid = world
    kind = f"test.longpoll.{uuid.uuid4().hex[:6]}"
    worker_longpoll.WAITERS.start(engine)
    _wait_listener()
    _enqueue_later(engine, pid, kind, 0.2, target="deployment:dep_x")
    got = asyncio.run(worker_longpoll.poll(engine, claim=_claimer(engine, [kind]),
                                           capabilities=[kind], wait_ms=900))
    assert got["status"] == "idle", "target=deployment:<id> is the hosts' work, never a local worker's"
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM jobs WHERE project_id = :p AND kind = :k"),
                           {"p": pid, "k": kind}).scalar()
    assert status == "queued"
    # a job spelled target='local' is still a local job
    with engine.begin() as c:
        job = jobs.enqueue_job(c, pid, kind, {"n": 2}, priority=50)
        c.execute(text("UPDATE jobs SET target = 'local' WHERE id = :j"), {"j": job["id"]})
    got = asyncio.run(worker_longpoll.poll(engine, claim=_claimer(engine, [kind]),
                                           capabilities=[kind], wait_ms=0))
    assert got["status"] == "leased" and got["job"]["id"] == job["id"] and got["long_poll"] is False


def test_the_sweep_is_one_query_over_every_waited_kind_and_wakes_only_those(world):
    engine, pid = world
    kind_a, kind_b = f"test.sweep.a.{uuid.uuid4().hex[:6]}", f"test.sweep.b.{uuid.uuid4().hex[:6]}"

    async def scenario():
        wa = worker_longpoll.WAITERS.register(engine, {kind_a})
        wb = worker_longpoll.WAITERS.register(engine, {kind_b})
        try:
            assert worker_longpoll.WAITERS.sweep(engine) == []
            with engine.begin() as c:
                jobs.enqueue_job(c, pid, kind_a, {"n": 1}, priority=50)
            # a lost NOTIFY: the hub said nothing; the sweep finds the queued kind
            wa.clear(), wb.clear()
            found = worker_longpoll.WAITERS.sweep(engine)
            assert found == [kind_a]
            assert await wa.wait(1.0) is True
            assert await wb.wait(0.05) is False, "a kind nobody queued wakes nobody"
        finally:
            worker_longpoll.WAITERS.unregister(wa)
            worker_longpoll.WAITERS.unregister(wb)

    asyncio.run(scenario())


# ═══ T9: event → run leased, through the real processor and the long-poll ══════════════════════

def test_t9_event_to_leased_run_p95_under_550ms_with_the_long_poll(world, capsys):
    engine, pid = world
    dep = deploy_local_agent(engine, pid)
    conn = make_connection(engine, pid, "webhook", name="t9")
    make_route(engine, pid, dep["id"], conn["id"], name="t9",
               limits={"per_entity": 1000, "per_entity_window_seconds": 3600, "storm_per_min": 10000})
    settings = _settings()
    event_intake.stop()
    assert event_intake.start(engine, settings) is True        # the real NOTIFY-driven processor
    worker_longpoll.WAITERS.start(engine)
    _wait_listener()
    time.sleep(1.0)                                              # listeners up
    kinds = ["agent.run"]
    samples: list[float] = []
    n = 20

    def scoped_claim():
        # The bench DB is shared and other projects leave queued agent.run jobs behind, which
        # the real claim_job (no project filter, by design) would lease first. The lease here
        # is the same SKIP LOCKED claim on the local target, scoped to this project; the
        # wake path under measurement (NOTIFY → hub → claim) is the one the API runs.
        with engine.begin() as c:
            row = c.execute(text("""
                UPDATE jobs SET status = 'leased', lease_owner = 'w-t9', attempt = attempt + 1,
                       lease_expires_at = now() + interval '60 seconds', updated_at = now()
                 WHERE id = (SELECT id FROM jobs WHERE project_id = :p AND kind = 'agent.run'
                              AND status = 'queued' AND (target IS NULL OR target = 'local')
                              ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                RETURNING id, kind, payload"""), {"p": pid}).mappings().first()
            return dict(row) if row else None

    async def one(i: int) -> float:
        waiter_task = asyncio.ensure_future(worker_longpoll.poll(
            engine, claim=scoped_claim, capabilities=kinds, wait_ms=10000))
        await asyncio.sleep(0.05)                                # the poll is waiting
        req = standard_webhook_request(conn["secret"], {"type": "com.acme.message",
                                                        "subject": f"T9/{i}", "text": f"m{i}"},
                                       msg_id=f"t9-{uuid.uuid4().hex[:8]}")
        t0 = time.perf_counter()
        got = event_intake.receive(engine, "webhook", token_of(conn["url_path"]), req)
        assert got["http_status"] == 202
        leased = await waiter_task
        t1 = time.perf_counter()
        assert leased["status"] == "leased", leased
        assert leased["job"]["kind"] == "agent.run"
        with engine.begin() as c:
            run = c.execute(text("SELECT finalize_ctx->>'intake_event_id' FROM agent_runs WHERE id = :r"),
                            {"r": leased["job"]["payload"]["run_id"]}).scalar()
        assert run == got["event_id"], "the leased job is this event's run"
        return (t1 - t0) * 1000.0

    async def bench():
        for i in range(n):
            samples.append(await one(i))

    try:
        asyncio.run(bench())
    finally:
        event_intake.stop()
    samples_sorted = sorted(samples)
    p50 = statistics.median(samples_sorted)
    p95 = samples_sorted[int(round(0.95 * (n - 1)))]
    print(f"\nT9 local long-poll: n={n} event received → run leased: "
          f"p50={p50:.0f} ms p95={p95:.0f} ms max={samples_sorted[-1]:.0f} ms")
    assert p95 <= 550, f"p95 {p95:.0f} ms over the 550 ms budget: {[round(s) for s in samples]}"
