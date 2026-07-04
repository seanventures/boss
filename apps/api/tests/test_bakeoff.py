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
         "owner": "paramg", "repo": "boss-sandbox"})
    assert payload["project_id"] == "boss-dogfood"
    assert payload["model_config"] == {"default": "native:deepseek-chat"}
    assert payload["profile"] == "strict"
    assert payload["open_pr"] is False  # bake-offs suppress PRs by default
    assert payload["test_commands"] == ["python3 -m unittest discover -v"]  # suite default
    bk = payload["finalize_ctx"]["bakeoff"]
    assert (bk["batch_id"], bk["config"], bk["task_key"], bk["iteration"], bk["expect"]) == \
        ("bake_x", "deepseek_all", "slugify", 2, "pass")
    assert "#2" in payload["title"] and payload["owner"] == "paramg"


def _seed_run(connection, project_id, batch_id, config, *, gate=None, expect="pass",
              llm_costs=(), run_status="completed", usage_cost=None):
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
                                               gates_passed, gates_failed)
                    VALUES (:id, :p, :run, :st, '[]'::jsonb, CAST(:gf AS JSONB))"""),
            {"id": f"qg_{uuid.uuid4().hex[:16]}", "p": project_id, "run": run_id,
             "st": gate.get("status", "pass"),
             "gf": json.dumps(gate.get("failed", []))},
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
