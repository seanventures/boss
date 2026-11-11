"""The harness score per change (BOSSAPP-959): GET /v1/harness/score answers the latest benchmark
run per engine and level against its baseline, with the verdict per metric; the runs are listed
and each can be read whole. Read only.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.harness_bench import store

RULES = {"k_stdev": 2.0, "metrics": {"cost_usd": {"abs_floor": 0.05}},
         "guards": {"success_rate": {"max_drop": 0.0}, "cost_usd": {"max_rise_pct": 15}}}


def _run(engine_db, *, cost: float, success: bool, baseline_name=None, compared_to=None):
    rid = store.new_run_id()
    meta = {"id": rid, "suite": "boss-harness", "suite_version": "1", "suite_sha256": "s" * 64,
            "engine": "claude_code", "model": None, "level": "delegate", "boss_sha": "abc123",
            "boss_dirty": False, "config": {"settings": {}}, "config_fingerprint": "fp",
            "rules": RULES, "task_ids": ["t1"], "repeat": 1, "budget_usd": 5.0}
    result = {"task_id": "t1", "kind": "fix", "iteration": 1, "success": success,
              "components": {"acceptance_passed": success},
              "metrics": {"wall_s": 100.0, "tokens_in": 1000, "tokens_out": 100, "cost_usd": cost,
                          "model_calls": 5, "turns": 20, "repair_rounds": 0, "interventions": 0}}
    with engine_db.begin() as c:
        store.create_run(c, meta)
        store.add_result(c, rid, result)
        store.finish_run(c, rid, status="completed", summary=store.summarize([result]),
                         report_path=None)
        if baseline_name:
            store.record_baseline(c, rid, baseline_name)
        if compared_to:
            c.execute(text("UPDATE harness_bench_runs SET compared_to = :b WHERE id = :i"),
                      {"b": compared_to, "i": rid})
    return rid


def test_the_score_compares_the_latest_run_with_its_baseline(client, engine):
    name = f"api-test-{uuid.uuid4().hex[:6]}"
    base = _run(engine, cost=1.0, success=True, baseline_name=name)
    cand = _run(engine, cost=2.0, success=False, compared_to=name)
    try:
        resp = client.get("/v1/harness/score")
        assert resp.status_code == 200, resp.text
        row = next(r for r in resp.json()["rows"] if r["engine"] == "claude_code")
        assert row["run"]["id"] == cand and row["baseline"] == name
        assert row["verdict"] == "worse"
        assert row["metrics"]["cost_usd"]["verdict"] == "worse"
        assert row["metrics"]["success_rate"]["candidate"] == 0.0
        assert {g["metric"] for g in row["guards"] if g["status"] == "fail"} == \
            {"success_rate", "cost_usd"}

        runs = client.get("/v1/harness/bench-runs").json()["runs"]
        assert [r["id"] for r in runs][:2] == [cand, base]
        whole = client.get(f"/v1/harness/bench-runs/{cand}").json()
        assert whole["results"][0]["task_id"] == "t1" and whole["run"]["compared_to"] == name
        assert client.get("/v1/harness/bench-runs/hbr_nope").status_code == 404
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM harness_bench_runs WHERE id IN (:a, :b)"),
                      {"a": base, "b": cand})
