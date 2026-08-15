"""#103 — concurrency load/soak harness: metrics, the stub runner, the concurrent driver, and the
saturation sweep. Model-free (stub runner)."""
from apps.api.app.services import concurrency_harness as ch


def test_percentile():
    v = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert ch.percentile(v, 0.5) == 0.3 and ch.percentile(v, 0.0) == 0.1 and ch.percentile(v, 1.0) == 0.5
    assert ch.percentile([], 0.5) is None


def test_stub_task_deterministic_failure():
    assert ch.stub_task(1, duration_s=0.0, fail_every=0)["ok"] is True
    assert ch.stub_task(0, duration_s=0.0, fail_every=3)["ok"] is False   # index 0 % 3 == 0 -> fails
    assert ch.stub_task(1, duration_s=0.0, fail_every=3)["ok"] is True


def test_summarize_metrics():
    results = [{"ok": True, "duration_s": 0.1, "concurrency_at_start": 5},
               {"ok": True, "duration_s": 0.2, "concurrency_at_start": 8},
               {"ok": False, "duration_s": 0.3, "error": "boom", "concurrency_at_start": 6}]
    m = ch.summarize(results, wall_clock_s=1.0)
    assert m["tasks"] == 3 and m["succeeded"] == 2 and m["failed"] == 1
    assert m["success_rate"] == round(2 / 3, 3) and m["throughput_per_s"] == 3.0
    assert m["latency_s"]["max"] == 0.3 and m["peak_concurrency"] == 8
    assert m["errors"] == {"boom": 1}


def test_run_load_respects_concurrency_and_counts():
    rep = ch.run_load(lambda i: ch.stub_task(i, duration_s=0.02, fail_every=5), n=30, concurrency=6)
    assert rep["tasks"] == 30
    assert rep["peak_concurrency"] <= 6            # never more than the cap in flight
    assert rep["failed"] == len([i for i in range(30) if i % 5 == 0])  # deterministic failures
    assert rep["throughput_per_s"] > 0 and rep["requested"] == {"n": 30, "concurrency": 6}


def test_run_load_survives_a_crashing_task():
    def boom(i):
        if i == 2:
            raise ValueError("kaboom")
        return {"ok": True}
    rep = ch.run_load(boom, n=5, concurrency=3)
    assert rep["tasks"] == 5 and rep["failed"] == 1   # the crash is a failure, harness completes


def test_run_load_zero_tasks():
    rep = ch.run_load(ch.stub_task, n=0, concurrency=4)
    assert rep["tasks"] == 0 and rep["succeeded"] == 0


def test_sweep_finds_saturation():
    sweep = ch.sweep(lambda i: ch.stub_task(i, duration_s=0.01), concurrencies=[1, 4, 8], n_per_level=16)
    assert len(sweep["levels"]) == 3
    assert sweep["saturation_at_concurrency"] in (1, 4, 8)
    assert sweep["peak_throughput_per_s"] and sweep["peak_throughput_per_s"] > 0


def test_endpoint_stub_default(client):
    body = client.post("/v1/concurrency-load", json={"n": 20, "concurrency": 5, "stub_duration_ms": 1}).json()
    assert body["tasks"] == 20 and body["throughput_per_s"] > 0
    assert body["runner"] == "python_stub"
    assert body["spent_usd_micros"] == 0
    assert body["go_scale_track"]["s1_go_sandbox_service"] is False
    assert body["go_scale_track"]["go_runtime_shipped"] is False
    sweep = client.post("/v1/concurrency-load",
                        json={"sweep": [1, 4], "n": 10, "stub_duration_ms": 1}).json()
    assert "saturation_at_concurrency" in sweep
    assert sweep["runner"] == "python_stub"
    assert sweep["go_scale_track"]["s1_go_sandbox_service"] is False
