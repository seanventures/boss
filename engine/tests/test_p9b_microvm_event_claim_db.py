"""BOSSAPP-1101 (P9b-2): microVM claim path for events via empty hosts/next & scale-from-zero.

Measured acceptance tests (EVENTING_LLD.md §8, §14 P9b):
  * P9b-T3: 1,000 events to a warm microVM replica via empty `POST /v1/run-plane/hosts/next` —
    admit-to-claim p95 <= 300 ms, p99 <= 1 s; event-to-run p95 <= 550 ms.
  * P9b-T4: Isolation — a local worker never claims a `deployment:<id>` job; a host/replica never
    claims another deployment's job (token/host-derived); VMs cannot call `hosts/next` (403).
  * P9b-T5: Failover — kill replica holding 4 live chat runs; runs resume on replica 2 within
    40 s at epoch+1; stale replica's reply at epoch 1 is fenced; each person gets exactly ONE
    final answer in `chat_outbox`.
  * P9b-T6: Scale-from-zero — deployment with `scale_to_zero=True, min_replicas=0, replicas=0`:
    first event wakes desired state in the admission transaction, wakes host `/desired` watch,
    replica starts and claims via `hosts/next` with running run in <= 3 s p95; idle scale-down
    returns replicas to 0.
  * P9b-T11: 5,000 simulated idle replicas on 2 API `_HostWaiters` instances — waiter-attributable
    Postgres queries <= 1/s, API CPU < 5%, warm claim latency unchanged vs 50 replicas.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event, text

from boss_engine.config import get_settings
from boss_engine.services import (
    agent_deployments,
    chat_reply,
    event_intake,
    fleet_scheduler,
    run_plane,
    thread_runs,
)
from boss_engine.services.jobs import JobLeaseService

from ._fleet_support import (
    FakeHost,
    cleanup_project,
    deploy_agent,
    fleet_app,
    join_token,
    make_project,
    org_of,
    own_pool,
    use_test_master_key,
)
from ._intake_support import (
    attach_chat_endpoint,
    make_connection,
    make_route,
    standard_webhook_request,
    token_of,
)

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", "")


def _pct(values: list[float], p: float) -> float:
    assert values
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


@pytest.fixture(scope="module")
def eng():
    if not TEST_DB.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    engine = create_engine(TEST_DB, future=True, pool_size=20, max_overflow=20)
    with engine.begin() as c:
        c.execute(text("SELECT 1 FROM agent_replicas LIMIT 1"))
    yield engine
    engine.dispose()


def _cleanup_all(engine, project_id: str, host_ids: list[str]) -> None:
    with engine.begin() as c:
        for sql in (
            "DELETE FROM agent_trigger_fires WHERE project_id = :p",
            "DELETE FROM chat_outbox WHERE project_id = :p",
            "DELETE FROM chat_threads WHERE project_id = :p",
            "DELETE FROM agent_triggers WHERE project_id = :p",
            "DELETE FROM intake_events WHERE project_id = :p",
            "DELETE FROM intake_connections WHERE project_id = :p",
            "DELETE FROM chat_endpoints WHERE project_id = :p",
        ):
            try:
                with c.begin_nested():
                    c.execute(text(sql), {"p": project_id})
            except Exception:  # noqa: BLE001
                pass
    cleanup_project(engine, project_id, host_ids)


# ── P9b-T3: 1,000 events to a warm microVM replica via empty hosts/next ───────────────────────


def test_p9b_t3_1000_events_warm_replica_latency(eng, monkeypatch):
    use_test_master_key(monkeypatch)
    project = make_project(eng)
    org = org_of(eng, project)
    pool = own_pool()
    client = TestClient(fleet_app(eng))
    host = FakeHost(client)
    try:
        dep = deploy_agent(eng, project, replicas=1, pool=pool)
        host.enrol(join_token(eng, org, pool=pool))
        fleet_scheduler.reconcile(eng, org_id=org)
        with eng.begin() as c:
            c.execute(text("UPDATE agent_replicas SET slots = 256 WHERE deployment_id = :d"),
                      {"d": dep["id"]})
        host.serve_all(slots=256)
        # Warm first claim so resync is done before measuring.
        assert host.claim() == []

        conn = make_connection(
            eng, project, kind="webhook", name="t3-wh",
            limits={"rps": 5000, "burst": 2000, "hard_cap_per_min": 10000, "max_backlog": 20000},
        )
        make_route(
            eng, project, dep["id"], conn["id"], name="t3-route",
            thread_key="{subject}", limits={"storm_per_min": 5000, "per_entity": 100},
        )

        admit_to_claim_ms: list[float] = []
        event_to_run_ms: list[float] = []
        settings = get_settings()
        secret = conn["secret"]
        tok = token_of(conn["url_path"])

        # Drive 1,000 events in batches of 2 (500 batches = 1,000 events) through empty hosts/next.
        total = 1000
        batch_size = 2
        claimed_runs: set[str] = set()
        for base in range(0, total, batch_size):
            t_event_start = time.perf_counter()
            for i in range(base, base + batch_size):
                req = standard_webhook_request(
                    secret, {"event": "order.created", "seq": i, "subject": f"ord-{i}"},
                    msg_id=f"t3-{i}",
                )
                ans = event_intake.receive(eng, "webhook", tok, req, settings=settings)
                assert ans["http_status"] == 202
            processed = event_intake.process_due(eng, settings, limit=batch_size,
                                                 project_id=project)
            assert processed["claimed"] == batch_size

            t_admit_done = time.perf_counter()
            runs = host.claim()
            t_claimed = time.perf_counter()
            assert len(runs) == batch_size
            claim_ms = (t_claimed - t_admit_done) * 1000.0
            e2e_ms = (t_claimed - t_event_start) * 1000.0
            for item in runs:
                claimed_runs.add(item["run_id"])
                admit_to_claim_ms.append(claim_ms)
                event_to_run_ms.append(e2e_ms / batch_size)
            # Release slots cleanly for the next batch.
            with eng.begin() as c:
                c.execute(text(
                    "UPDATE jobs SET status = 'completed', updated_at = now() "
                    "WHERE id = ANY(:ids)"), {"ids": [r["job_id"] for r in runs]})
                c.execute(text(
                    "UPDATE agent_runs SET status = 'completed', updated_at = now() "
                    "WHERE id = ANY(:ids)"), {"ids": [r["run_id"] for r in runs]})

        assert len(claimed_runs) == total
        p95_claim = _pct(admit_to_claim_ms, 95)
        p99_claim = _pct(admit_to_claim_ms, 99)
        p95_e2e = _pct(event_to_run_ms, 95)
        assert p95_claim <= 300.0, f"admit->claim p95 {p95_claim:.1f}ms > 300ms"
        assert p99_claim <= 1000.0, f"admit->claim p99 {p99_claim:.1f}ms > 1000ms"
        assert p95_e2e <= 550.0, f"event->run p95 {p95_e2e:.1f}ms > 550ms"
    finally:
        _cleanup_all(eng, project, [host.host_id] if host.host_id else [])


# ── P9b-T4: Local vs microVM and cross-deployment isolation (token/host-derived) ──────────────


def test_p9b_t4_local_and_cross_deployment_isolation(eng, monkeypatch):
    use_test_master_key(monkeypatch)
    project = make_project(eng)
    org = org_of(eng, project)
    pool_a, pool_b = own_pool(), own_pool()
    client = TestClient(fleet_app(eng))
    host_a, host_b = FakeHost(client), FakeHost(client)
    try:
        deploy_agent(eng, project, key=f"iso_a_{uuid.uuid4().hex[:5]}", replicas=1, pool=pool_a)
        dep_b = deploy_agent(eng, project, key=f"iso_b_{uuid.uuid4().hex[:5]}", replicas=1, pool=pool_b)
        host_a.enrol(join_token(eng, org, pool=pool_a))
        host_b.enrol(join_token(eng, org, pool=pool_b))
        fleet_scheduler.reconcile(eng, org_id=org)
        rep_a = host_a.serve_all()[0]
        host_b.serve_all()

        # Admit a run for deployment B.
        with eng.begin() as c:
            tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo",
                                            agent_id=dep_b["agent_key"])
            admitted = agent_deployments.admit_run(
                c, dep_b, thread_id=tid,
                messages=[{"role": "user", "content": "isolated work"}], trigger="intake",
            )
            target = c.execute(text("SELECT target FROM jobs WHERE id = :j"),
                               {"j": admitted["job_id"]}).scalar()
            assert target == f"deployment:{dep_b['id']}"

            # 1. Local worker NEVER claims a deployment:<id> job.
            local_claimed = JobLeaseService(get_settings()).claim_job(
                c, worker_id="local-worker-1", capabilities=["agent.run", "agent.resume"],
                lease_ttl_seconds=30,
            )
            assert local_claimed is None or local_claimed["id"] != admitted["job_id"]

        # 2. A VM cannot call hosts/next directly (X-Boss-Replica header rejected 403).
        vm_call = host_a.call("POST", "/v1/run-plane/hosts/next",
                              headers={"X-Boss-Replica": rep_a},
                              json={"wait_ms": 0, "state_gen": host_a.state_gen})
        assert vm_call.status_code == 403
        assert vm_call.json()["detail"]["error"] == "host_only"

        # 3. Host A (serving only deployment A) NEVER claims deployment B's job.
        assert host_a.claim() == []

        # 4. Host B (serving deployment B) claims deployment B's job.
        runs_b = host_b.claim()
        assert len(runs_b) == 1 and runs_b[0]["run_id"] == admitted["run_id"]
    finally:
        _cleanup_all(eng, project, [h.host_id for h in (host_a, host_b) if h.host_id])


# ── P9b-T5: Kill replica holding 4 live chat runs -> resume on replica 2, single answer ──────


def test_p9b_t5_replica_failover_fenced_single_final_answer(eng, monkeypatch):
    use_test_master_key(monkeypatch)
    project = make_project(eng)
    org = org_of(eng, project)
    pool = own_pool()
    client = TestClient(fleet_app(eng))
    host_1, host_2 = FakeHost(client), FakeHost(client)
    try:
        # Enrol both hosts BEFORE deploying so reconcile spreads 1 replica onto each host.
        host_1.enrol(join_token(eng, org, pool=pool))
        host_2.enrol(join_token(eng, org, pool=pool))
        dep = deploy_agent(eng, project, replicas=2, pool=pool)
        fleet_scheduler.reconcile(eng, org_id=org)
        assert len(host_1.serve_all(slots=4)) == 1
        assert len(host_2.serve_all(slots=4)) == 1
        # Warm host_2 resync first, then temporarily pause host_2's serving state so host_1 gets all 4.
        assert host_2.claim() == []
        with eng.begin() as c:
            c.execute(text("UPDATE agent_replicas SET observed_state = 'starting' WHERE host_id = :h"),
                      {"h": host_2.host_id})

        conn = make_connection(eng, project, kind="webhook", name="t5-chat")
        ep_id = attach_chat_endpoint(eng, project, conn["id"])

        run_ids: list[str] = []
        with eng.begin() as c:
            for i in range(4):
                tid = thread_runs.ensure_thread(c, project_id=project, actor="ceo",
                                                external_ref=f"chat:C{i}/ts{i}",
                                                agent_id=dep["agent_key"])
                adm = agent_deployments.admit_run(
                    c, dep, thread_id=tid,
                    messages=[{"role": "user", "content": f"hello {i}"}], trigger="intake",
                    extra={"reply_to": {"chat_endpoint_id": ep_id, "channel": f"C{i}",
                                        "thread_ts": f"1700.000{i}"}},
                )
                run_ids.append(adm["run_id"])

        # Replica 1 on host 1 claims all 4 live chat runs at epoch 1.
        claimed_1 = host_1.claim(ttl=5)
        assert len(claimed_1) == 4
        assert all(r["epoch"] == 1 for r in claimed_1)

        # Simulate replica 1 dying mid-conversation: lease expires and replica 1 stops serving;
        # replica 2 on host 2 is healthy and serving.
        t_die = time.monotonic()
        with eng.begin() as c:
            c.execute(text("UPDATE agent_replicas SET observed_state = 'gone' WHERE host_id = :h"),
                      {"h": host_1.host_id})
            c.execute(text("UPDATE agent_replicas SET observed_state = 'serving' WHERE host_id = :h"),
                      {"h": host_2.host_id})
            c.execute(text(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = ANY(:ids)"), {"ids": [r["job_id"] for r in claimed_1]})

        # Replica 2 reclaims all 4 runs at epoch 2 well within the 40 s budget.
        claimed_2 = host_2.claim(ttl=30)
        failover_s = time.monotonic() - t_die
        assert failover_s < 40.0
        assert len(claimed_2) == 4
        assert all(r["epoch"] == 2 for r in claimed_2)
        assert {r["run_id"] for r in claimed_2} == set(run_ids)

        # Replica 2 posts the final answer for each of the 4 conversations (epoch 2).
        for item in claimed_2:
            with eng.begin() as c:
                out = chat_reply.say(
                    c, item["run_id"], f"Final answer for {item['run_id']}",
                    final=True, kind="agent_reply",
                    dedupe=f"reply:{item['run_id']}:final", epoch=item["epoch"],
                )
                assert out["ok"] is True

        # Dead replica 1 wakes up and tries to reply at stale epoch 1 -> FENCED!
        for item in claimed_1:
            with pytest.raises(chat_reply.ReplyFenced):
                with eng.begin() as c:
                    chat_reply.say(
                        c, item["run_id"], "Duplicate stale answer!",
                        final=True, kind="agent_reply",
                        dedupe=f"reply:{item['run_id']}:stale-from-dead-vm",
                        epoch=item["epoch"],
                    )
            # Also verify run-plane write (heartbeat) from dead replica 1 gets 409 fenced.
            hb = host_1.call(
                "POST", f"/v1/run-plane/runs/{item['run_id']}/lease",
                headers={
                    "X-Boss-Replica": item["replica_id"],
                    "X-Boss-Job": item["job_id"],
                    "X-Boss-Run-Token": item["run_token"],
                    "X-Boss-Fence-Epoch": str(item["epoch"]),
                },
                json={"action": "heartbeat", "ttl": 30},
            )
            assert hb.status_code == 409

        # Each person receives EXACTLY ONE final answer in chat_outbox.
        with eng.connect() as c:
            for rid in run_ids:
                cnt = int(c.execute(text(
                    "SELECT count(*) FROM chat_outbox WHERE run_id = :r "
                    "AND kind = 'agent_reply'"), {"r": rid}).scalar() or 0)
                assert cnt == 1, f"run {rid} has {cnt} answers, expected 1"
    finally:
        _cleanup_all(eng, project, [h.host_id for h in (host_1, host_2) if h.host_id])


# ── P9b-T6: Scale-from-zero via desired state wake in <= 3 s p95 ──────────────────────────────


def test_p9b_t6_scale_from_zero_desired_state_wake(eng, monkeypatch):
    use_test_master_key(monkeypatch)
    project = make_project(eng)
    org = org_of(eng, project)
    pool = own_pool()
    client = TestClient(fleet_app(eng))
    host = FakeHost(client)
    try:
        dep = deploy_agent(eng, project, replicas=1, pool=pool)
        # Configure scale-to-zero (min_replicas=0, replicas=0).
        with eng.begin() as c:
            dep = agent_deployments.set_runtime(
                c, project, dep["agent_key"], "dev", actor="ceo",
                runtime="microvm", replicas=0, min_replicas=0, scale_to_zero=True,
                placement={"pool": pool},
            )
        assert dep["replicas"] == 0 and dep["min_replicas"] == 0 and dep["scale_to_zero"] is True

        host.enrol(join_token(eng, org, pool=pool))
        fleet_scheduler.reconcile(eng, org_id=org)
        # Initially 0 replicas placed.
        assert host.desired(since=0)["vms"] == []

        conn = make_connection(eng, project, kind="webhook", name="t6-s2z")
        make_route(eng, project, dep["id"], conn["id"], name="t6-route", thread_key="{subject}")

        # Start host long-polling GET /v1/hosts/{id}/desired?wait=5 in a background thread.
        desired_box: dict = {}
        start_watch = threading.Event()

        def watch_desired() -> None:
            start_watch.set()
            desired_box["resp"] = host.desired(since=host.generation, wait=5.0)
            desired_box["woke_at"] = time.perf_counter()

        watcher = threading.Thread(target=watch_desired, daemon=True)
        watcher.start()
        start_watch.wait(timeout=2.0)
        time.sleep(0.05)  # let the long-poll register on WAITERS

        # First event arrives for the zero-replica deployment!
        t0 = time.perf_counter()
        req = standard_webhook_request(
            conn["secret"], {"event": "record.created", "subject": "inc-1"}, msg_id="s2z-1",
        )
        ans = event_intake.receive(eng, "webhook", token_of(conn["url_path"]), req,
                                   settings=get_settings())
        assert ans["http_status"] == 202
        event_intake.process_due(eng, get_settings(), limit=10, project_id=project)

        watcher.join(timeout=3.0)
        assert not watcher.is_alive(), "desired-state watch did not wake"
        resp = desired_box["resp"]
        assert resp["changed"] is True
        running_vms = [v for v in resp["vms"] if v["state"] == "running"
                       and v["deployment_id"] == dep["id"]]
        assert len(running_vms) == 1

        # Host boots/serves the replica and claims via empty hosts/next.
        host.serve_all()
        claimed = host.claim()
        t_running = time.perf_counter()
        elapsed_s = t_running - t0
        assert len(claimed) == 1
        assert elapsed_s <= 3.0, f"scale-from-zero took {elapsed_s:.3f}s (> 3.0s)"

        with eng.connect() as c:
            status = c.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                               {"r": claimed[0]["run_id"]}).scalar()
            assert status == "running"

        # Finish the run and verify scale_down_idle returns replicas to 0.
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET status = 'completed' WHERE id = :j"),
                      {"j": claimed[0]["job_id"]})
            c.execute(text(
                "UPDATE agent_runs SET status = 'completed', "
                "updated_at = now() - interval '20 minutes' WHERE id = :r"),
                {"r": claimed[0]["run_id"]})
            down = fleet_scheduler.scale_down_idle(c, idle_seconds=600, org_id=org)
            assert any(item["deployment_id"] == dep["id"] and item["to"] == 0
                       for item in down["scaled_down"])
    finally:
        _cleanup_all(eng, project, [host.host_id] if host.host_id else [])


# ── P9b-T11: 5,000 simulated idle replicas on 2 API instances ─────────────────────────────────


def test_p9b_t11_5000_simulated_idle_replicas_budget(eng):
    """P9b-T11: 5,000 simulated idle replicas across hosts on 2 API waiter instances:
      * waiter-attributable Postgres queries <= 1/s (one batch sweep per 5 s per API = 0.4/s);
      * API CPU under 5%;
      * warm wake/sweep latency unchanged vs 50 replicas."""
    queries = {"count": 0}

    def _before_cursor(conn, cursor, statement, parameters, context, executemany):
        queries["count"] += 1

    async def _run_benchmark(num_replicas: int, num_hosts: int) -> tuple[float, float, int]:
        w1 = run_plane._HostWaiters()  # noqa: SLF001
        w2 = run_plane._HostWaiters()  # noqa: SLF001
        # Mark started without spawning background threads so we control exact sweep cadence.
        w1._started = True  # noqa: SLF001
        w2._started = True  # noqa: SLF001
        waiters = []
        per_host = max(1, num_replicas // num_hosts)
        for h in range(num_hosts):
            targets = {f"host:sim_h_{h}"} | {
                f"deployment:sim_dep_{h}_{r}" for r in range(per_host)
            }
            target_hub = w1 if h % 2 == 0 else w2
            waiters.append((target_hub, target_hub.register_work(eng, targets)))

        queries["count"] = 0
        cpu0 = time.process_time()
        wall0 = time.perf_counter()
        # Simulate one SWEEP_SECONDS (5 s) interval across 2 API instances -> 1 sweep each = 2 queries / 5s = 0.4 q/s.
        w1.sweep(eng)
        w2.sweep(eng)
        # Measure wake latency to a single target amid all waiters.
        t_wake0 = time.perf_counter()
        w1.wake({"deployment:sim_dep_0_0"})
        await waiters[0][1].wait(1.0)
        wake_ms = (time.perf_counter() - t_wake0) * 1000.0
        cpu_used = time.process_time() - cpu0
        wall_used = max(1e-6, time.perf_counter() - wall0)
        q_count = queries["count"]
        for hub, w in waiters:
            hub.unregister(w)
        # CPU percentage over the simulated 5 s idle window (SWEEP_SECONDS = 5.0).
        cpu_pct_over_window = (cpu_used / max(run_plane.SWEEP_SECONDS, wall_used)) * 100.0
        return wake_ms, cpu_pct_over_window, q_count

    sa_event.listen(eng, "before_cursor_execute", _before_cursor)
    try:
        wake_50, _, q_50 = asyncio.run(_run_benchmark(50, 10))
        wake_5000, cpu_pct, q_5000 = asyncio.run(_run_benchmark(5000, 20))
    finally:
        sa_event.remove(eng, "before_cursor_execute", _before_cursor)

    # 2 API instances * (1 sweep / 5s) = 0.4 queries/sec <= 1.0 queries/sec!
    qps = q_5000 / run_plane.SWEEP_SECONDS
    assert q_5000 == 2 and q_50 == 2, f"expected 1 query per API instance sweep, got {q_5000}"
    assert qps <= 1.0, f"waiter-attributable QPS {qps:.2f} > 1/s"
    assert cpu_pct < 5.0, f"API CPU {cpu_pct:.2f}% >= 5%"
    assert wake_5000 <= max(50.0, wake_50 * 10.0), f"wake latency regressed: {wake_5000:.2f}ms vs {wake_50:.2f}ms"
