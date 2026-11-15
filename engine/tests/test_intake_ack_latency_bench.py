"""T2 (P9a), as a small local benchmark: signed deliveries through the real FastAPI route into
the bench database, one process, sequential and 8-way concurrent; reports p50/p95/p99 of the
ack. Runs only when BOSS_INTAKE_BENCH=1 (it is a measurement, not a gate); the numbers are in
the P9a-1 PR description."""
from __future__ import annotations

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boss_engine.services import event_intake

from ._intake_support import make_connection, make_project, pg_engine, purge, standard_webhook_request, token_of

pytestmark = pytest.mark.skipif(os.environ.get("BOSS_INTAKE_BENCH") != "1",
                                reason="set BOSS_INTAKE_BENCH=1 to measure ack latency")


def _pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * len(values))))]


def test_ack_latency_through_the_route(capsys):
    from apps.api.app.routers.intake import build_intake_router
    from boss_engine.config import get_settings

    engine = pg_engine()
    pid = make_project(engine)
    event_intake.BUCKETS.reset()
    try:
        app = FastAPI()
        app.include_router(build_intake_router(
            engine=engine, settings=get_settings, require_write_actor=lambda *a: "ceo",
            require_reader=lambda *a: None))
        conn = make_connection(engine, pid, "webhook", name="bench",
                               limits={"rps": 10000, "burst": 10000, "hard_cap_per_min": 10 ** 9})
        url = conn["url_path"]
        body = {"type": "com.grafana.alert", "subject": "payments-api",
                "text": "error rate > 2% on payments-api for 5 minutes " * 4}

        def one(i: int) -> float:
            req = standard_webhook_request(conn["secret"], body, msg_id=f"b{i}")
            t0 = time.perf_counter()
            with TestClient(app) as client:
                r = client.post(url, content=req.body, headers=dict(req.headers))
            assert r.status_code == 202, r.text
            return time.perf_counter() - t0

        # warm-up
        for i in range(20):
            one(10_000 + i)
        n_seq = int(os.environ.get("BOSS_INTAKE_BENCH_N", "500"))
        seq = [one(i) for i in range(n_seq)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            t0 = time.perf_counter()
            conc = list(pool.map(one, range(20_000, 20_000 + n_seq)))
            wall = time.perf_counter() - t0
        report = {
            "sequential": {"n": n_seq, "p50_ms": round(_pct(seq, 50) * 1000, 1),
                           "p95_ms": round(_pct(seq, 95) * 1000, 1),
                           "p99_ms": round(_pct(seq, 99) * 1000, 1),
                           "mean_ms": round(statistics.mean(seq) * 1000, 1)},
            "concurrent_8": {"n": n_seq, "p50_ms": round(_pct(conc, 50) * 1000, 1),
                             "p95_ms": round(_pct(conc, 95) * 1000, 1),
                             "p99_ms": round(_pct(conc, 99) * 1000, 1),
                             "throughput_rps": round(n_seq / wall, 1)},
        }
        with capsys.disabled():
            print("\nINTAKE ACK LATENCY " + json.dumps(report))
        assert report["sequential"]["p99_ms"] <= 300
    finally:
        purge(engine, pid)
