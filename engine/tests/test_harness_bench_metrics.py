"""The harness benchmark reads what a build cost and did from BOSS's own rows, never the agent's.

Seeded llm_calls / agent_turns / quality_gates / events / decisions rows for one code task run
(and a repair child) must come out as exactly these numbers: tokens in (prompt + cache read +
cache write) and out, cached share, USD, model calls, turns, tool calls, repair rounds,
interventions beyond the planned merge question, and success only when every component holds.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.harness_bench import metrics as m


def _call(stage, pt, ct, cr=0, cw=0, cost=0, turns=1, **kw):
    return {"stage": stage, "engine": "claude_code", "model": "sonnet", "route_id": "route_1",
            "prompt_tokens": pt, "completion_tokens": ct, "cache_read_tokens": cr,
            "cache_write_tokens": cw, "cost_usd_micros": cost, "turns": turns, "ok": True, **kw}


CALLS = [_call("implement", 1_000, 400, cr=8_000, cw=1_000, cost=250_000, turns=12),
         _call("review_code", 2_000, 100, cr=0, cw=0, cost=50_000, turns=3),
         _call("implement", 500, 300, cr=4_500, cost=100_000, turns=5, ok=False)]


def test_calls_sum_to_tokens_cache_share_and_dollars():
    s = m.summarize_calls(CALLS)
    assert s["tokens_in"] == 1_000 + 8_000 + 1_000 + 2_000 + 500 + 4_500
    assert s["tokens_out"] == 800
    assert s["cache_share"] == pytest.approx(12_500 / 17_000, abs=1e-6)
    assert s["cost_usd"] == pytest.approx(0.4)
    assert s["model_calls"] == 3 and s["turns"] == 20 and s["failed_calls"] == 1
    assert s["by_stage"]["implement"]["calls"] == 2
    assert s["routes"] == ["route_1"]


def test_no_calls_is_zero_not_a_division_error():
    s = m.summarize_calls([])
    assert (s["tokens_in"], s["cache_share"], s["cost_usd"], s["model_calls"]) == (0, 0.0, 0.0, 0)


def test_the_merge_question_is_planned_everything_else_is_an_intervention():
    decisions = [{"id": "dec_merge", "question": "Approve code change: Slug service"},
                 {"id": "dec_other_merge", "question": "Approve code change: again"},
                 {"id": "dec_plan", "question": "Approve the plan for Slug service?"},
                 {"id": "dec_waiver", "question": "Waive the failing security gate?"}]
    n, asked = m.count_interventions(decisions, merge_decision_id="dec_merge", phase="done")
    assert n == 2 and asked[0].startswith("Approve the plan")
    n, asked = m.count_interventions([], merge_decision_id=None, phase="awaiting_gate_override")
    assert n == 1 and "awaiting_gate_override" in asked[0]


def test_repair_rounds_count_implement_retries_and_review_rounds_not_continuations():
    kinds = ["code_task.started", "code_task.inrun_convergence_round",
             "code_task.inrun_convergence_round", "code_task.review_repair_round",
             "code_task.implement_continued"]
    assert m.count_repair_rounds(kinds, implement_retries=1) == 4


def _gate(status="pass", passed=("written_tests_pass", "code_review_passed"), failed=(),
          tests_passed=True, repair_attempts=0):
    return {"status": status, "tests_passed": tests_passed, "adversarial_verdict": "survived",
            "gates_passed": json.dumps(list(passed)), "gates_failed": json.dumps(list(failed)),
            "evidence": json.dumps({"repair_attempts": repair_attempts})}


def _record(run_status="completed", pr="https://github.com/o/r/pull/1", gate=None, events=(),
            decisions=(), calls=CALLS, phase="done", rid="crun_1"):
    return {"run": {"id": rid, "status": run_status, "phase": phase, "pull_request_url": pr,
                    "decision_id": "dec_merge", "finalize_ctx": {"work_branch": "b"}},
            "calls": list(calls), "agent_turn_rows": 30, "tool_calls": 9,
            "gate_row": gate if gate is not None else _gate(),
            "events": [{"event_type": e, "data": {}} for e in events],
            "decisions": list(decisions)}


def test_success_needs_every_component():
    ok = m.measure(_record(), wall_s=61.234, acceptance={"passed": True})
    assert ok["success"] is True
    assert ok["metrics"]["wall_s"] == 61.23 and ok["metrics"]["tool_calls"] == 9
    for bad, component in [
        (dict(run_status="failed"), "completed"),
        (dict(pr=None), "pr_opened"),
        (dict(gate=_gate(tests_passed=False)), "verify_passed"),
        (dict(gate=_gate(failed=("code_review_passed",), passed=("written_tests_pass",))),
         "review_passed"),
        (dict(gate=_gate(status="fail")), "gates_passed"),
        (dict(events=("code_task.pr_opened_with_checks_failing",)), "no_checks_failing_pr"),
    ]:
        out = m.measure(_record(**bad), wall_s=1, acceptance={"passed": True})
        assert out["success"] is False, component
        assert out["components"][component] is False, component
    no_accept = m.measure(_record(), wall_s=1, acceptance={"passed": False})
    assert no_accept["success"] is False and not no_accept["components"]["acceptance_passed"]


def test_a_missing_gate_row_is_not_a_pass():
    out = m.measure(_record(gate=None) | {"gate_row": None}, wall_s=1, acceptance={"passed": True})
    assert out["gates"]["status"] == "missing" and out["success"] is False


def test_a_lineage_sums_costs_and_is_judged_by_its_last_run():
    first = _record(run_status="failed", pr=None, gate=_gate(status="fail", repair_attempts=1),
                    events=("code_task.review_repair_round",), rid="crun_1",
                    decisions=[{"id": "d1", "question": "Waive?"}])
    child = _record(rid="crun_2", decisions=[{"id": "d1", "question": "Waive?"}],
                    calls=CALLS[:1])
    merged = m.merge_lineage([first, child])
    out = m.measure(merged, wall_s=10, acceptance={"passed": True})
    assert out["success"] is True                        # the lineage ended well
    assert out["metrics"]["model_calls"] == 4             # 3 + 1, both runs paid for
    assert out["metrics"]["repair_rounds"] == 2           # 1 implement retry + 1 cold round
    assert out["metrics"]["lineage_runs"] == 2
    assert out["metrics"]["interventions"] == 1           # the same decision counted once


# ── read_run on a real database ──────────────────────────────────────────────────────────────

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def db():
    if not PG_URL:
        pytest.skip("needs BOSS_TEST_DATABASE_URL (a boss_test* Postgres)")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def test_read_run_reads_the_ledger_rows_of_one_run(db):
    tag = uuid.uuid4().hex[:8]
    project, run_id = f"p_hbm{tag}", f"crun_hbm{tag}"
    with db.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_hbm', 'hbm') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_hbm', 'org_hbm', 'hbm') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_hbm', :p, :p)"),
                  {"p": project})
        since = c.execute(text("SELECT now()")).scalar()
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, pull_request_url, "
            "decision_id, finalize_ctx) VALUES (:i, :p, 't', 'completed', 'done', "
            "'https://github.com/o/r/pull/7', 'dec_m' || :t, CAST(:f AS JSONB))"),
            {"i": run_id, "p": project, "t": tag, "f": json.dumps({"work_branch": "boss/x"})})
        for n, call in enumerate(CALLS):
            c.execute(text(
                "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
                "prompt_tokens, completion_tokens, total_tokens, cache_read_tokens, "
                "cache_write_tokens, cost_usd_micros, turns, ok, route_id) VALUES (:id, :p, :r, "
                ":stage, :engine, :model, :pt, :ct, 0, :cr, :cw, :cost, :turns, :ok, :route)"),
                {"id": f"llmc_hbm{tag}{n}", "p": project, "r": run_id, "stage": call["stage"],
                 "engine": call["engine"], "model": call["model"], "pt": call["prompt_tokens"],
                 "ct": call["completion_tokens"], "cr": call["cache_read_tokens"],
                 "cw": call["cache_write_tokens"], "cost": call["cost_usd_micros"],
                 "turns": call["turns"], "ok": call["ok"], "route": call["route_id"]})
        for n, direction in enumerate(["prompt", "tool_call", "tool_result", "tool_call", "completion"]):
            c.execute(text(
                "INSERT INTO agent_turns (id, project_id, code_task_run_id, stage, turn_index, "
                "direction, tool_name) VALUES (:id, :p, :r, 'implement', :n, :d, :tool)"),
                {"id": f"turn_hbm{tag}{n}", "p": project, "r": run_id, "n": n, "d": direction,
                 "tool": "Edit" if direction.startswith("tool") else None})
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, status, tests_passed, "
            "gates_passed, gates_failed, evidence) VALUES (:id, :p, :r, 'pass', true, "
            "CAST(:gp AS JSONB), '[]'::jsonb, CAST(:ev AS JSONB))"),
            {"id": f"qg_hbm{tag}", "p": project, "r": run_id,
             "gp": json.dumps(["written_tests_pass", "code_review_passed"]),
             "ev": json.dumps({"repair_attempts": 1})})
        for n, kind in enumerate(["code_task.started", "code_task.inrun_convergence_round"]):
            c.execute(text(
                "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data) "
                "VALUES (:id, :p, :k, 'boss', 'code_task_run', :r, '{}'::jsonb)"),
                {"id": f"evt_hbm{tag}{n}", "p": project, "k": kind, "r": run_id})
        for did, q in ((f"dec_m{tag}", "Approve code change: t"), (f"dec_x{tag}", "Waive the gate?")):
            c.execute(text("INSERT INTO decisions (id, project_id, question) VALUES (:i, :p, :q)"),
                      {"i": did, "p": project, "q": q})
    try:
        with db.begin() as c:
            until = c.execute(text("SELECT now()")).scalar()
            record = m.read_run(c, run_id, project_id=project, since=since, until=until)
        out = m.measure(record, wall_s=42.0, acceptance={"passed": True})
        assert out["success"] is True
        assert out["metrics"]["tokens_in"] == 17_000 and out["metrics"]["tokens_out"] == 800
        assert out["metrics"]["cost_usd"] == pytest.approx(0.4)
        assert out["metrics"]["model_calls"] == 3 and out["metrics"]["turns"] == 20
        assert out["metrics"]["tool_calls"] == 2 and out["metrics"]["agent_turn_rows"] == 5
        assert out["metrics"]["repair_rounds"] == 2       # 1 implement retry + 1 warm round
        assert out["metrics"]["interventions"] == 1       # the waiver; the merge is planned
        assert out["interventions_asked"] == ["Waive the gate?"]
    finally:
        with db.begin() as c:
            for table, col in (("llm_calls", "code_task_run_id"), ("agent_turns", "code_task_run_id"),
                               ("quality_gates", "code_task_run_id"), ("events", "entity_id")):
                c.execute(text(f"DELETE FROM {table} WHERE {col} = :r"), {"r": run_id})
            c.execute(text("DELETE FROM decisions WHERE project_id = :p"), {"p": project})
            c.execute(text("DELETE FROM code_task_runs WHERE id = :r"), {"r": run_id})
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project})
