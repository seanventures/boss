"""The harness benchmark's comparison (harness_bench/compare.py) says better, same or worse by
stated rules, never on noise, and a failed guard is a non-zero exit code.

Every later harness change (BOSSAPP-958) is accepted or rejected by these rules, so each one is
pinned here: the noise band, each guard kind, the verdict, and the cases that are not comparable.
"""
from __future__ import annotations

import copy

import pytest

from boss_engine.harness_bench.compare import (
    EXIT_NOT_COMPARABLE, EXIT_OK, EXIT_WORSE, compare, pass_values, render, stats,
)

RULES = {
    "k_stdev": 2.0,
    "metrics": {"wall_s": {"abs_floor": 30, "rel_floor": 0.10},
                "cost_usd": {"abs_floor": 0.05, "rel_floor": 0.05},
                "tokens_in": {"abs_floor": 2000, "rel_floor": 0.05}},
    "guards": {"success_rate": {"max_drop": 0.0}, "cost_usd": {"max_rise_pct": 15},
               "wall_s": {"max_rise_pct": 25}, "interventions": {"max_rise": 0}},
}
TASKS = ("t-fix", "t-feature", "t-new")


def _result(task, it, *, success=True, **m):
    metrics = {"wall_s": 100.0, "tokens_in": 50_000, "tokens_out": 5_000,
               "cache_read_tokens": 20_000, "cost_usd": 1.0, "model_calls": 10, "turns": 40,
               "repair_rounds": 0, "interventions": 0}
    metrics.update(m)
    return {"task_id": task, "iteration": it, "success": success, "metrics": metrics,
            "components": {"acceptance_passed": success}}


def _report(results, *, engine="claude_code", level="delegate", suite_sha="s1",
            status="completed", config=None, rid="hbr_x"):
    return {"run": {"id": rid, "engine": engine, "level": level, "suite_sha256": suite_sha,
                    "status": status, "config": config or {"settings": {"code_repair_max_retries": 0}},
                    "repeat": max((r["iteration"] for r in results), default=1)},
            "results": results}


def _run(n_passes=1, per_pass=None, **overrides):
    """A report of `n_passes` passes over TASKS; per_pass(pass) -> metric overrides."""
    out = []
    for p in range(1, n_passes + 1):
        extra = per_pass(p) if per_pass else {}
        for t in TASKS:
            out.append(_result(t, p, **{**overrides, **extra}))
    return out


def test_identical_runs_are_the_same_and_exit_zero():
    base = _report(_run())
    cmp = compare(base, copy.deepcopy(base), RULES)
    assert cmp["verdict"] == "same"
    assert cmp["exit_code"] == EXIT_OK
    assert all(m["verdict"] == "same" for m in cmp["metrics"].values())
    assert all(g["status"] == "pass" for g in cmp["guards"])


def test_a_cheaper_faster_run_is_better():
    base = _report(_run())
    cand = _report(_run(cost_usd=0.5, wall_s=60.0))
    cmp = compare(base, cand, RULES)
    assert cmp["verdict"] == "better"
    assert cmp["metrics"]["cost_usd"]["verdict"] == "better"
    assert cmp["metrics"]["wall_s"]["verdict"] == "better"   # 3 x 40 s = 120 s > band 30 s
    assert cmp["exit_code"] == EXIT_OK


def test_a_success_drop_fails_its_guard_and_exits_one():
    base = _report(_run())
    cand_results = _run()
    cand_results[0]["success"] = False
    cmp = compare(base, _report(cand_results), RULES)
    assert cmp["verdict"] == "worse"
    assert cmp["exit_code"] == EXIT_WORSE
    guard = next(g for g in cmp["guards"] if g["metric"] == "success_rate")
    assert guard["status"] == "fail"
    assert "t-fix" in render(cmp)          # the task that passes less often is named


def test_cost_within_the_allowed_rise_passes_beyond_it_fails():
    base = _report(_run())                                   # 3 tasks x $1 = $3
    ok = compare(base, _report(_run(cost_usd=1.10)), RULES)  # +10% < 15%
    assert next(g for g in ok["guards"] if g["metric"] == "cost_usd")["status"] == "pass"
    assert ok["exit_code"] == EXIT_OK
    bad = compare(base, _report(_run(cost_usd=1.20)), RULES)  # +20% > 15%, +$0.60 > band
    assert next(g for g in bad["guards"] if g["metric"] == "cost_usd")["status"] == "fail"
    assert bad["verdict"] == "worse" and bad["exit_code"] == EXIT_WORSE


def test_a_rise_inside_the_noise_band_is_not_a_regression():
    # +60% on a tiny baseline: past the 15% limit, but $0.03 is under the $0.05 floor.
    base = _report(_run(cost_usd=0.0166))
    cand = _report(_run(cost_usd=0.0266))
    cmp = compare(base, cand, RULES)
    assert cmp["metrics"]["cost_usd"]["verdict"] == "same"
    assert next(g for g in cmp["guards"] if g["metric"] == "cost_usd")["status"] == "pass"


def test_spread_widens_the_band_so_noise_is_never_called_a_change():
    # Baseline wall time swings 300..600 s per pass (3 tasks summed); the candidate's mean is
    # 25%+ higher, past the limit, but inside 2 x stdev: not significant, the guard passes.
    swings = {1: 100.0, 2: 200.0, 3: 150.0}
    base = _report(_run(3, per_pass=lambda p: {"wall_s": swings[p]}))
    cand = _report(_run(3, per_pass=lambda p: {"wall_s": swings[p] * 1.3}))
    cmp = compare(base, cand, RULES)
    wall = cmp["metrics"]["wall_s"]
    assert wall["baseline"]["n"] == 3 and wall["baseline"]["stdev"] > 0
    assert wall["delta"] > 0.25 * wall["baseline"]["mean"]
    assert wall["noise_band"] > wall["delta"]
    assert wall["verdict"] == "same"
    assert next(g for g in cmp["guards"] if g["metric"] == "wall_s")["status"] == "pass"
    # The same rise with no spread is a real regression.
    steady_b = _report(_run(3, wall_s=150.0))
    steady_c = _report(_run(3, wall_s=195.0))
    hard = compare(steady_b, steady_c, RULES)
    assert hard["metrics"]["wall_s"]["verdict"] == "worse"
    assert hard["exit_code"] == EXIT_WORSE


def test_one_new_intervention_fails_the_guard():
    cmp = compare(_report(_run()), _report(_run(interventions=1)), RULES)
    assert next(g for g in cmp["guards"] if g["metric"] == "interventions")["status"] == "fail"
    assert cmp["exit_code"] == EXIT_WORSE


def test_a_pct_guard_on_a_zero_baseline_allows_no_rise_beyond_the_band():
    rules = {**RULES, "metrics": {"cost_usd": {"abs_floor": 0.0}},
             "guards": {"cost_usd": {"max_rise_pct": 50}}}
    cmp = compare(_report(_run(cost_usd=0.0)), _report(_run(cost_usd=0.01)), rules)
    assert cmp["exit_code"] == EXIT_WORSE


def test_an_unguarded_metric_that_got_worse_is_reported_and_blocks_better():
    rules = {**RULES, "guards": {"success_rate": {"max_drop": 0.0}}}
    cand = _report(_run(cost_usd=0.5, tokens_in=80_000))    # cheaper, but more tokens
    cmp = compare(_report(_run()), cand, rules)
    assert cmp["metrics"]["tokens_in"]["verdict"] == "worse"
    assert cmp["verdict"] == "same" and cmp["exit_code"] == EXIT_OK
    assert any("unguarded" in n for n in cmp["notes"])


def test_higher_is_better_metrics_are_judged_the_right_way_round():
    rules = {**RULES, "metrics": {"cache_share": {"abs_floor": 0.02}}, "guards": {}}
    up = compare(_report(_run()), _report(_run(cache_read_tokens=40_000)), rules)
    assert up["metrics"]["cache_share"]["verdict"] == "better"
    down = compare(_report(_run()), _report(_run(cache_read_tokens=0)), rules)
    assert down["metrics"]["cache_share"]["verdict"] == "worse"


@pytest.mark.parametrize("change, words", [
    ({"suite_sha": "s2"}, "suite changed"),
    ({"engine": "codex"}, "different engines"),
    ({"level": "guided"}, "scaffold levels"),
    ({"status": "incomplete"}, "not completed"),
])
def test_runs_that_cannot_be_compared_say_why_and_exit_two(change, words):
    cmp = compare(_report(_run()), _report(_run(), **change), RULES)
    assert cmp["verdict"] == "not_comparable"
    assert cmp["exit_code"] == EXIT_NOT_COMPARABLE
    assert any(words in r for r in cmp["reasons"])


def test_a_suite_or_engine_change_can_be_compared_on_purpose():
    cmp = compare(_report(_run()), _report(_run(), engine="codex", suite_sha="s2"), RULES,
                  allow_suite_change=True, allow_cross_engine=True)
    assert cmp["verdict"] == "same"


def test_only_shared_tasks_and_complete_passes_are_compared():
    base = _report(_run() + [_result("t-extra", 1)])
    cand_results = _run(2)
    cand_results = [r for r in cand_results if not (r["iteration"] == 2 and r["task_id"] == "t-new")]
    cand = _report(cand_results)
    cmp = compare(base, cand, RULES)
    assert cmp["tasks"] == sorted(TASKS)
    assert cmp["only_in_baseline"] == ["t-extra"]
    # pass 2 lacks a task: it is not a sample of the suite, so only pass 1 counts.
    assert cmp["metrics"]["wall_s"]["candidate"]["n"] == 1


def test_the_config_change_is_named():
    cand = _report(_run(), config={"settings": {"code_repair_max_retries": 2}})
    cmp = compare(_report(_run()), cand, RULES)
    assert cmp["config_changed"] == {"settings.code_repair_max_retries": [0, 2]}
    assert "code_repair_max_retries" in render(cmp)


def test_pass_aggregates_sum_and_ratio():
    vals = pass_values(_run(2), list(TASKS))
    assert vals["cost_usd"] == [3.0, 3.0]
    assert vals["success_rate"] == [1.0, 1.0]
    assert vals["cache_share"] == [pytest.approx(0.4), pytest.approx(0.4)]
    assert stats([1.0, 3.0]) == {"n": 2, "mean": 2.0, "stdev": pytest.approx(1.41421356), "min": 1.0,
                                 "max": 3.0}
