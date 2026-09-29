"""VAG bake-off harness (spec §8): request building + batch aggregation + rendering."""
import json
import uuid

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.bakeoff import (
    aggregate_batch,
    build_run_request,
    render_markdown,
)

TASK = {
    "key": "slugify", "title": "URL slug generator", "instructions": "make slugs",
    "acceptance_criteria": ["a"], "risk_tier": 2,
}
CONFIG = {"name": "deepseek_all", "profile": "strict",
          "model_config": {"default": "native:deepseek-chat"}}


def test_build_run_request_shape():
    payload = build_run_request(
        TASK, CONFIG, "bake_x", 2,
        {"project_id": "boss-dogfood", "test_commands": ["python3 -m unittest discover -v"],
         "owner": "example-user", "repo": "boss-sandbox"})
    assert payload["project_id"] == "boss-dogfood"
    assert payload["model_config"] == {"default": "native:deepseek-chat"}
    assert payload["profile"] == "strict"
    assert payload["open_pr"] is False  # bake-offs suppress PRs by default
    assert payload["test_commands"] == ["python3 -m unittest discover -v"]  # suite default
    bk = payload["finalize_ctx"]["bakeoff"]
    assert (bk["batch_id"], bk["config"], bk["task_key"], bk["iteration"], bk["expect"]) == \
        ("bake_x", "deepseek_all", "slugify", 2, "pass")
    assert "#2" in payload["title"] and payload["owner"] == "example-user"


def test_build_run_request_carries_repair_retries():
    # per-config override wins; else the suite default; else omitted entirely.
    cfg = {**CONFIG, "repair_retries": 3}
    assert build_run_request(TASK, cfg, "bake_x", 1, {"project_id": "p"})["repair_retries"] == 3
    assert build_run_request(TASK, CONFIG, "bake_x", 1,
                             {"project_id": "p", "repair_retries": 2})["repair_retries"] == 2
    assert "repair_retries" not in build_run_request(TASK, CONFIG, "bake_x", 1, {"project_id": "p"})


def test_build_run_request_carries_review_panel_size():
    # #9 refute-panel sweep: per-config panel size wins; else suite default; else omitted.
    cfg = {**CONFIG, "review_panel_size": 3}
    assert build_run_request(TASK, cfg, "bake_x", 1, {"project_id": "p"})["review_panel_size"] == 3
    assert build_run_request(TASK, CONFIG, "bake_x", 1,
                             {"project_id": "p", "review_panel_size": 5})["review_panel_size"] == 5
    assert "review_panel_size" not in build_run_request(TASK, CONFIG, "bake_x", 1,
                                                        {"project_id": "p"})


def _seed_run(connection, project_id, batch_id, config, *, gate=None, expect="pass",
              llm_costs=(), run_status="completed", usage_cost=None, repair_attempts=None):
    run_id = f"crun_{uuid.uuid4().hex[:16]}"
    connection.execute(
        text("""INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, usage,
                                            finalize_ctx, created_at, updated_at)
                VALUES (:id, :p, :st, 'done', 'bake', '{}'::jsonb, CAST(:u AS JSONB),
                        CAST(:fc AS JSONB), now() - interval '90 seconds', now())"""),
        {"id": run_id, "p": project_id, "st": run_status,
         "u": json.dumps({"cost_usd_micros": usage_cost} if usage_cost else {}),
         "fc": json.dumps({"bakeoff": {"batch_id": batch_id, "config": config,
                                       "task_key": "t", "iteration": 1, "expect": expect}})},
    )
    if gate is not None:
        connection.execute(
            text("""INSERT INTO quality_gates (id, project_id, code_task_run_id, status,
                                               gates_passed, gates_failed, evidence)
                    VALUES (:id, :p, :run, :st, '[]'::jsonb, CAST(:gf AS JSONB),
                            CAST(:ev AS JSONB))"""),
            {"id": f"qg_{uuid.uuid4().hex[:16]}", "p": project_id, "run": run_id,
             "st": gate.get("status", "pass"),
             "gf": json.dumps(gate.get("failed", [])),
             "ev": json.dumps({"repair_attempts": repair_attempts}
                              if repair_attempts is not None else {})},
        )
    for cost, retries, estimated in llm_costs:
        connection.execute(
            text("""INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine,
                                           model, cost_usd_micros, retries, price_estimated)
                    VALUES (:id, :p, :run, 'implement', 'native', 'deepseek-chat',
                            :c, :r, :e)"""),
            {"id": f"llmc_{uuid.uuid4().hex[:16]}", "p": project_id, "run": run_id,
             "c": cost, "r": retries, "e": estimated},
        )
    return run_id


def test_aggregate_batch_metrics(engine):
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        # config A: 2 runs, both gates pass, $3 total across llm_calls
        _seed_run(c, project_id, batch_id, "a", gate={"status": "pass"},
                  llm_costs=[(1_000_000, 1, False), (500_000, 0, False)])
        _seed_run(c, project_id, batch_id, "a", gate={"status": "pass"},
                  llm_costs=[(1_500_000, 0, True)])
        # config B: 1 pass + 1 gate-fail with named gates
        _seed_run(c, project_id, batch_id, "b", gate={"status": "pass"},
                  llm_costs=[(2_000_000, 2, False)])
        _seed_run(c, project_id, batch_id, "b",
                  gate={"status": "fail", "failed": ["security_clean"]},
                  run_status="blocked", llm_costs=[(750_000, 0, False)])
    with engine.begin() as c:
        report = aggregate_batch(c, batch_id)

    a, b = report["configs"]["a"], report["configs"]["b"]
    assert report["total_runs"] == 4
    assert (a["runs"], a["gate_pass"], a["pass_rate"]) == (2, 2, 1.0)
    assert a["total_cost_usd_micros"] == 3_000_000
    assert a["cost_per_verified_outcome_usd_micros"] == 1_500_000
    assert a["retries"] == 1 and a["llm_calls"] == 3 and a["price_estimated_calls"] == 1
    assert a["mean_wall_clock_s"] is not None and a["mean_wall_clock_s"] > 0
    assert (b["runs"], b["gate_pass"], b["pass_rate"]) == (2, 1, 0.5)
    assert b["failure_breakdown"] == {"security_clean": 1}


def test_aggregate_repair_histogram(engine):
    """The repair loop's attempt count (quality_gates.evidence.repair_attempts) aggregates
    into a per-config histogram: how many runs used 0 / 1 / 2 / ... evidence-fed retries."""
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        _seed_run(c, project_id, batch_id, "hy", gate={"status": "pass"}, repair_attempts=0,
                  llm_costs=[(100, 0, False)])
        _seed_run(c, project_id, batch_id, "hy", gate={"status": "pass"}, repair_attempts=2,
                  llm_costs=[(100, 0, False)])
        _seed_run(c, project_id, batch_id, "hy",
                  gate={"status": "fail", "failed": ["written_tests_pass"]},
                  run_status="blocked", repair_attempts=3, llm_costs=[(100, 0, False)])
    with engine.begin() as c:
        report = aggregate_batch(c, batch_id)
    hist = report["configs"]["hy"]["repair_hist"]
    assert hist == {0: 1, 2: 1, 3: 1}
    md = render_markdown(report)
    assert "repairs used" in md and "2×1" in md  # non-zero repairs surfaced in the table


def test_aggregate_latency_and_time_per_verified(engine):
    """Time axis: per-config wall-clock -> s/verified, and per-model latency (avg/p90)."""
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        # two passing runs; wall_clock ~90s each (seeded in _seed_run)
        r1 = _seed_run(c, project_id, batch_id, "hy", gate={"status": "pass"},
                       llm_costs=[(100, 0, False)])
        r2 = _seed_run(c, project_id, batch_id, "hy", gate={"status": "pass"},
                       llm_costs=[(100, 0, False)])
        # explicit per-model latency: a fast cheap model + a slow frontier one
        for run in (r1, r2):
            for model, lat, cost in (("deepseek/deepseek-chat", 4000, 200),
                                     ("claude-opus-4-8", 40000, 90000)):
                c.execute(text("""INSERT INTO llm_calls (id, project_id, code_task_run_id, stage,
                                     engine, model, cost_usd_micros, latency_ms, retries, ok)
                                  VALUES (:id,:p,:run,'implement','x',:m,:c,:l,0,true)"""),
                          {"id": f"llmc_{uuid.uuid4().hex[:16]}", "p": project_id, "run": run,
                           "m": model, "c": cost, "l": lat})
    with engine.begin() as c:
        report = aggregate_batch(c, batch_id)
    hy = report["configs"]["hy"]
    assert hy["gate_pass"] == 2
    assert hy["wall_clock_per_verified_s"] is not None and hy["wall_clock_per_verified_s"] > 0
    assert hy["p90_wall_clock_s"] is not None
    models = report["models"]
    assert models["deepseek/deepseek-chat"]["avg_latency_ms"] == 4000
    assert models["claude-opus-4-8"]["avg_latency_ms"] == 40000  # frontier is the slow lane
    md = render_markdown(report)
    assert "s/verified" in md and "Per-model latency" in md and "deepseek/deepseek-chat" in md


def test_aggregate_zero_pass_cost_is_none(engine):
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        _seed_run(c, project_id, batch_id, "a", gate={"status": "fail", "failed": ["written_tests_pass"]},
                  run_status="blocked", llm_costs=[(100, 0, False)])
    with engine.begin() as c:
        report = aggregate_batch(c, batch_id)
    a = report["configs"]["a"]
    assert a["pass_rate"] == 0.0
    assert a["cost_per_verified_outcome_usd_micros"] is None


def test_known_bad_counts_false_pass_not_pass_rate(engine):
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        _seed_run(c, project_id, batch_id, "a", gate={"status": "pass"}, expect="fail",
                  llm_costs=[(100, 0, False)])  # verifier fooled -> false pass
        _seed_run(c, project_id, batch_id, "a", gate={"status": "fail", "failed": ["consistent"]},
                  expect="fail", run_status="blocked")  # verifier caught it -> good
    with engine.begin() as c:
        report = aggregate_batch(c, batch_id)
    a = report["configs"]["a"]
    assert (a["runs"], a["known_bad_runs"], a["false_pass"]) == (0, 2, 1)
    assert a["pass_rate"] is None  # known-bad probes never count toward pass rate


def test_cost_single_source_is_llm_calls(engine):
    """code_task_runs.usage carries the SAME dollars aggregated — the report must not
    add it to llm_calls sums (double count) nor prefer it."""
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        _seed_run(c, project_id, batch_id, "a", gate={"status": "pass"},
                  llm_costs=[(1_000_000, 0, False)], usage_cost=9_999_999)
    with engine.begin() as c:
        report = aggregate_batch(c, batch_id)
    assert report["configs"]["a"]["total_cost_usd_micros"] == 1_000_000


def test_render_markdown_smoke(engine):
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        _seed_run(c, project_id, batch_id, "claude_baseline", gate={"status": "pass"},
                  llm_costs=[(1_000_000, 0, False)])
    with engine.begin() as c:
        md = render_markdown(aggregate_batch(c, batch_id))
    assert batch_id in md and "claude_baseline" in md and "$1.00" in md


def test_report_endpoint_aggregates_server_side(client, engine):
    """GET /v1/bakeoff/{batch}/report — the driver's no-DB-access path (goldeneye)."""
    project_id = get_settings().default_project_id
    batch_id = f"bake_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        _seed_run(c, project_id, batch_id, "claude_baseline", gate={"status": "pass"},
                  llm_costs=[(1_000_000, 0, False)])
    resp = client.get(f"/v1/bakeoff/{batch_id}/report")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configs"]["claude_baseline"]["gate_pass"] == 1
    assert "claude_baseline" in body["markdown"]
    # unknown batch: empty but well-formed, not an error
    empty = client.get("/v1/bakeoff/bake_nonexistent/report").json()
    assert empty["total_runs"] == 0 and empty["configs"] == {}
