"""THE BARE LEVEL (BOSSAPP-977): the plain-coding-agent baseline, in dollars.

What must hold, on the scripted conformance engine only (no model, no cost, no real CLI):

  * the brief handed to the engine's own loop carries the whole task and no pipeline framing;
  * metrics come from the engine's OWN telemetry, and a number the engine did not report is
    recorded UNKNOWN (null, named in telemetry_unknown), never a fabricated zero;
  * a bare run lands in the same store and shape as pipeline runs — and runs NO pipeline:
    no code_task_runs, no quality_gates, no PR;
  * the same acceptance checks decide success, so the fault the pipeline repairs
    (new-slug-service's failing_test) honestly FAILS bare;
  * a cross-level comparison is refused unless asked for (--cross-level), and the committed
    scripted-bare baseline was made on this suite and says all of the above.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "engine"
BENCH = ROOT / "scripts" / "harness-bench"
PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
DB_NAME = "boss_test_bench_bare"

import sys  # noqa: E402

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from boss_engine.harness_bench import LEVELS  # noqa: E402
from boss_engine.harness_bench.bare import (  # noqa: E402
    BARE_MAX_TURNS, bare_brief, bare_components, bare_metrics,
)
from boss_engine.harness_bench.compare import comparable  # noqa: E402
from boss_engine.harness_bench.store import default_baseline_name  # noqa: E402
from boss_engine.harness_bench.suite import BASELINES_DIR, load_suite  # noqa: E402


# ── the parts that need no world ─────────────────────────────────────────────────────────────

def test_bare_is_a_level():
    assert "bare" in LEVELS and "delegate" in LEVELS


def test_the_brief_carries_the_whole_task_and_no_pipeline_framing():
    task = {"title": "Fix the thing", "instructions": "Change src/a.js so it works.\n",
            "acceptance_criteria": ["it works", "npm test passes"],
            "test_commands": ["npm test", "npm run lint"]}
    brief = bare_brief(task)
    assert "TASK: Fix the thing" in brief
    assert "Change src/a.js so it works." in brief
    for line in ("- it works", "- npm test passes", "- npm test", "- npm run lint"):
        assert line in brief
    # The engine's own loop gets a plain brief: no BOSS repo-context pack, no stage names.
    for framing in ("REPO CONTEXT", "stage", "pipeline", "review panel"):
        assert framing not in brief


def test_metrics_come_from_the_engines_own_telemetry():
    session = {"usage": {"prompt_tokens": 100, "completion_tokens": 40,
                         "cache_read_tokens": 300, "cache_write_tokens": 100},
               "cost_usd_micros": 123_400, "turns": 7, "tool_calls": 12}
    metrics, unknown = bare_metrics(session, wall_s=12.345)
    assert unknown == []
    assert metrics["wall_s"] == 12.35
    assert metrics["tokens_in"] == 500 and metrics["tokens_out"] == 40
    assert metrics["cache_share"] == 0.6
    assert metrics["cost_usd"] == 0.1234
    assert metrics["model_calls"] == 1 and metrics["turns"] == 7 and metrics["tool_calls"] == 12
    # No pipeline ran, so these are 0 by construction, not unknown.
    assert metrics["repair_rounds"] == 0 and metrics["interventions"] == 0


def test_a_number_the_engine_did_not_report_is_unknown_never_zero():
    metrics, unknown = bare_metrics({"usage": {"model": "x"}, "cost_unreported": True,
                                     "cost_usd_micros": 0, "turns": 3}, wall_s=5)
    assert metrics["tokens_in"] is None and metrics["tokens_out"] is None
    assert metrics["cache_share"] is None and metrics["cost_usd"] is None
    assert sorted(unknown) == ["cache_share", "cost_usd", "tokens_in", "tokens_out"]
    # A session that never happened (the engine raised) reports nothing either.
    metrics, unknown = bare_metrics(None, wall_s=1)
    assert metrics["cost_usd"] is None and "cost_usd" in unknown


def test_success_is_completed_changed_and_accepted():
    ok = bare_components(session_ok=True, changed=True, acceptance_passed=True)
    assert ok == {"completed": True, "changed": True, "acceptance_passed": True} and all(ok.values())
    assert not all(bare_components(session_ok=True, changed=True,
                                   acceptance_passed=False).values())
    late = bare_components(session_ok=True, changed=True, acceptance_passed=True, timed_out=True)
    assert late["within_time_limit"] is False and not all(late.values())


def test_a_cross_level_comparison_is_refused_unless_asked_for():
    base = {"run": {"status": "completed", "suite_sha256": "x", "engine": "scripted",
                    "level": "delegate"}}
    cand = copy.deepcopy(base)
    cand["run"]["level"] = "bare"
    why = comparable(base, cand)
    assert any("scaffold levels" in r and "--cross-level" in r for r in why)
    assert comparable(base, cand, allow_cross_level=True) == []


def test_a_bare_run_is_never_scored_against_a_pipeline_baseline_by_default():
    assert default_baseline_name("scripted", "delegate") == "scripted"
    assert default_baseline_name("scripted", "bare") == "scripted-bare"
    assert default_baseline_name("claude_code", "bare") == "claude_code-bare"
    assert default_baseline_name("native:qwen2.5-coder", "bare") == "native-bare"
    assert default_baseline_name("codex", None) == "codex"


def test_unknown_telemetry_is_flagged_in_a_comparison():
    from boss_engine.harness_bench.compare import compare

    def report(unknown=False):
        m = {"wall_s": 1, "tokens_in": 10, "tokens_out": 2, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "cache_share": 0.0, "cost_usd": (None if unknown else 0.1),
             "model_calls": 1, "turns": 1, "repair_rounds": 0, "interventions": 0}
        r = {"task_id": "t", "iteration": 1, "success": True, "metrics": m}
        if unknown:
            r["telemetry_unknown"] = ["cost_usd"]
        return {"run": {"status": "completed", "suite_sha256": "x", "engine": "codex",
                        "level": "bare", "config": {}}, "results": [r]}

    cmp = compare(report(), report(unknown=True),
                  {"k_stdev": 2.0, "metrics": {}, "guards": {"success_rate": {"max_drop": 0}}})
    assert any("unknown telemetry" in n and "cost_usd" in n for n in cmp["notes"])


# ── the committed baseline ───────────────────────────────────────────────────────────────────

def test_the_scripted_bare_baseline_was_made_on_this_suite_and_is_honest():
    path = BASELINES_DIR / "scripted-bare.json"
    assert path.is_file(), "bench/harness/baselines/scripted-bare.json is missing"
    base = json.loads(path.read_text())
    run, suite = base["run"], load_suite()
    assert run["suite_sha256"] == suite["_sha256"], (
        "the suite changed since the scripted-bare baseline was made: re-run "
        "`scripts/harness-bench run --engine scripted --level bare --save-baseline "
        "scripted-bare` and commit it")
    assert run["engine"] == "scripted" and run["level"] == "bare"
    assert run["status"] == "completed"
    assert sorted(run["task_ids"]) == sorted(t["id"] for t in suite["tasks"])
    # The bare config carries no pipeline knobs: the pipeline does not run at this level.
    assert run["config"]["settings"] == {}
    assert run["config"]["bare"] == {"max_turns": BARE_MAX_TURNS}
    by_task = {r["task_id"]: r for r in base["results"]}
    # The suite plays failing_test on new-slug-service; with no pipeline nothing repairs it, so
    # the honest bare baseline FAILS that task — the pipeline's value, measured.
    assert by_task["new-slug-service"]["success"] is False
    assert by_task["new-slug-service"]["components"]["acceptance_passed"] is False
    assert all(r["success"] for t, r in by_task.items() if t != "new-slug-service"), by_task
    for r in base["results"]:
        assert (r["metrics"]["cost_usd"] == 0 and r["metrics"]["model_calls"] == 1
                and r["metrics"]["repair_rounds"] == 0 and r["metrics"]["interventions"] == 0)


# ── the runner's refusals (no database is opened) ────────────────────────────────────────────

FAKE_DB = "postgresql+psycopg://u:p@localhost:5432/boss_test_x"


def _bench(*args, db_url: str, timeout: int = 600) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "BOSS_TEST_DATABASE_URL")}
    env["BOSS_BENCH_DATABASE_URL"] = db_url
    return subprocess.run([str(BENCH), *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_the_bare_level_refuses_pipeline_knobs():
    done = _bench("run", "--engine", "scripted", "--level", "bare",
                  "--set", "code_repair_max_retries=2", db_url=FAKE_DB)
    assert done.returncode == 2
    assert "bare level runs none" in done.stderr


def test_a_real_engine_on_the_bare_level_still_needs_a_budget():
    done = _bench("run", "--engine", "claude_code", "--level", "bare", "--allow-unisolated",
                  db_url=FAKE_DB)
    assert done.returncode == 2
    assert "budget" in done.stderr or "not on PATH" in done.stderr


def test_an_unknown_level_is_still_refused():
    done = _bench("run", "--engine", "scripted", "--level", "guided", db_url=FAKE_DB)
    assert done.returncode == 2 and "delegate, bare" in done.stderr


def test_a_dry_run_plans_a_bare_run_without_building():
    done = _bench("run", "--engine", "scripted", "--level", "bare", "--dry-run", db_url=FAKE_DB)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "level bare" in done.stdout and "dry run" in done.stdout
    assert f'"bare": {{"max_turns": {BARE_MAX_TURNS}}}' in done.stdout


# ── end to end on the scripted engine (a throwaway boss_test* database) ──────────────────────

@pytest.fixture(scope="module")
def bench_db():
    if not PG_URL:
        pytest.skip("needs BOSS_TEST_DATABASE_URL (a Postgres the suite may create databases on)")
    if not any((Path(p) / "node").exists() for p in os.environ.get("PATH", "").split(os.pathsep)):
        pytest.skip("the HelpDesk task needs node on PATH")
    url = make_url(PG_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create {DB_NAME}: {type(exc).__name__}")
    target = url.set(database=DB_NAME).render_as_string(hide_password=False)
    yield target
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)'))
    admin.dispose()


def test_a_bare_task_lands_in_the_same_store_and_runs_no_pipeline(bench_db, tmp_path):
    report_path = tmp_path / "bare.json"
    done = _bench("run", "--engine", "scripted", "--level", "bare", "--tasks",
                  "helpdesk-classify-case", "--report", str(report_path), db_url=bench_db)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    report = json.loads(report_path.read_text())
    assert report["run"]["status"] == "completed" and report["run"]["level"] == "bare"
    (r,) = report["results"]
    assert r["success"] is True, r.get("components")
    assert r["components"] == {"completed": True, "changed": True, "acceptance_passed": True}
    # The SAME acceptance checks as the pipeline lane: the task's own tests and the held tests.
    checks = {c["check"]: c["ok"] for c in r["acceptance"]["checks"]}
    assert checks.get("src/classify.js changed") is True
    assert any("held acceptance tests" in k and ok for k, ok in checks.items())
    m = r["metrics"]
    assert m["cost_usd"] == 0 and m["model_calls"] == 1 and m["turns"] == 1
    assert m["tokens_in"] > 0 and m["repair_rounds"] == 0 and m["interventions"] == 0
    assert r.get("code_task_run_id") is None and r["branch"].startswith("bare/")

    eng = create_engine(bench_db, future=True)
    try:
        with eng.connect() as c:
            row = c.execute(text("SELECT status, engine, level FROM harness_bench_runs "
                                 "WHERE id = :i"), {"i": report["run"]["id"]}).mappings().one()
            assert dict(row) == {"status": "completed", "engine": "scripted", "level": "bare"}
            stored = c.execute(text(
                "SELECT success, model_calls, turns, repair_rounds, interventions, "
                "cost_usd_micros FROM harness_bench_results WHERE bench_run_id = :i"),
                {"i": report["run"]["id"]}).one()
            assert tuple(stored) == (True, 1, 1, 0, 0, 0)
            # NO PIPELINE: the bare level starts no code task and runs no gate.
            assert c.execute(text("SELECT count(*) FROM code_task_runs")).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM quality_gates")).scalar() == 0
            # The engine's telemetry still landed, attached to the bench's project, no run.
            calls = c.execute(text("SELECT stage, code_task_run_id FROM llm_calls")).all()
            assert calls and all(s == "solo" and run_id is None for s, run_id in calls)
    finally:
        eng.dispose()

    # The same report compared with itself: SAME, exit 0 — the compare path reads bare rows
    # exactly as it reads pipeline rows.
    same = _bench("compare", str(report_path), "--baseline", str(report_path), db_url=bench_db)
    assert same.returncode == 0 and "VERDICT: SAME" in same.stdout, same.stdout

    # Across levels it refuses unless asked: the delegate twin of this report is another level.
    twin = json.loads(report_path.read_text())
    twin["run"] = {**twin["run"], "level": "delegate"}
    twin_path = tmp_path / "twin.json"
    twin_path.write_text(json.dumps(twin))
    refused = _bench("compare", str(report_path), "--baseline", str(twin_path), db_url=bench_db)
    assert refused.returncode == 2 and "scaffold levels" in refused.stdout
    allowed = _bench("compare", str(report_path), "--baseline", str(twin_path), "--cross-level",
                     db_url=bench_db)
    assert allowed.returncode == 0 and "VERDICT: SAME" in allowed.stdout, allowed.stdout

    # The harness score shows the lane, per engine and level.
    score = _bench("report", db_url=bench_db)
    assert score.returncode == 0 and "scripted / bare" in score.stdout


def test_the_fault_the_pipeline_repairs_honestly_fails_bare(bench_db, tmp_path):
    """new-slug-service plays failing_test. The pipeline lane repairs it and passes; the bare
    lane has no repair round, so the broken first implementation is what acceptance judges."""
    report_path = tmp_path / "bare-slug.json"
    done = _bench("run", "--engine", "scripted", "--level", "bare", "--tasks",
                  "new-slug-service", "--report", str(report_path), db_url=bench_db)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    (r,) = json.loads(report_path.read_text())["results"]
    assert r["success"] is False
    assert r["components"]["changed"] is True            # the agent did produce a change
    assert r["components"]["acceptance_passed"] is False  # but the change is wrong, and caught
    assert r["metrics"]["repair_rounds"] == 0
