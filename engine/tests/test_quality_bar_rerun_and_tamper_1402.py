"""BOSSAPP-1402: Quality bar computed once per build, flaky re-run cost bounded,
and tampered-then-restored boss.verify.yaml labelled tampered rather than flaky."""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as gcts
from boss_engine.services import quality_bar as qb
from boss_engine.services.sandbox import CommandResult


def test_should_rerun_flaky_bounds_cost_and_tampering():
    assert qb.should_rerun_flaky(failing_count=1, duration_ms=1000) == (True, "ok")
    assert qb.should_rerun_flaky(tampered=True) == (False, "tampered_manifest")
    assert qb.should_rerun_flaky(timed_out=True) == (False, "timed_out")
    assert qb.should_rerun_flaky(install_failed=True) == (False, "install_failed")
    assert qb.should_rerun_flaky(has_command=False) == (False, "no_command")
    assert qb.should_rerun_flaky(missing_packages=True) == (False, "missing_packages")
    assert qb.should_rerun_flaky(failing_count=qb.FLAKY_MAX_FAILING_TESTS + 1) == (
        False,
        "too_many_failures",
    )
    assert qb.should_rerun_flaky(duration_ms=qb.FLAKY_MAX_SUITE_MS + 1) == (
        False,
        "suite_too_slow",
    )
    assert qb.should_rerun_flaky(checks_used=qb.FLAKY_MAX_CHECKS_PER_BUILD) == (
        False,
        "build_rerun_cap",
    )
    assert qb.should_rerun_flaky(rerun_ms_used=qb.FLAKY_MAX_TOTAL_RERUN_MS) == (
        False,
        "build_time_cap",
    )


def test_command_result_tampered_defaults_false_and_skips_flaky():
    clean = CommandResult(command="pytest", argv=["pytest"], exit_code=1, duration_ms=50,
                          timed_out=False, stdout="", stderr="fail")
    tampered = CommandResult(command="", argv=[], exit_code=1, duration_ms=0,
                             timed_out=False, stdout="", stderr="boss.verify.yaml was modified",
                             tampered=True)
    assert clean.tampered is False
    assert tampered.tampered is True


def test_bar_for_run_is_called_once_inside_run_governed_code_task():
    src = inspect.getsource(gcts._run_governed_code_task)
    assert src.count("_bar_for_run(") == 1
    assert "_manifest_tampered[0]" in src
    assert "should_rerun_flaky(" in src
    assert "tampered_verify_manifest" in src


def test_run_flaky_reruns_preserves_attempt1_output_when_third_is_budget_skipped():
    first_res = CommandResult(command="npm test", argv=["npm", "test"], exit_code=1,
                              duration_ms=10_000, timed_out=False, stdout="", stderr="FAIL test_a")
    second_res = CommandResult(command="npm test", argv=["npm", "test"], exit_code=0,
                               duration_ms=65_000, timed_out=False, stdout="PASS all", stderr="")

    calls = 0

    def _run_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            return second_res, "PASS all (attempt 2)", "npm test", "app"
        raise AssertionError("Attempt 3 must be skipped when rerun budget would be exceeded")

    budget = {"checks": 1, "rerun_ms": 0}
    flaky_tests: list[str] = []
    res, out, cmd, cwd = gcts._run_flaky_reruns(
        first_res,
        "FAIL test_a (attempt 1)",
        "npm test",
        "app",
        ["test_a"],
        run_once=_run_once,
        flaky_budget=budget,
        flaky_tests=flaky_tests,
        max_total_rerun_ms=120_000,
    )
    assert calls == 1
    assert res is first_res
    assert res.ok is False
    assert out == "FAIL test_a (attempt 1)"
    assert cmd == "npm test"
    assert cwd == "app"
    assert flaky_tests == []
    assert budget == {"checks": 2, "rerun_ms": 65_000}


def test_run_flaky_reruns_records_flaky_when_second_and_third_pass_within_budget():
    first_res = CommandResult(command="npm test", argv=["npm", "test"], exit_code=1,
                              duration_ms=5_000, timed_out=False, stdout="", stderr="FAIL test_a")
    pass_res = CommandResult(command="npm test", argv=["npm", "test"], exit_code=0,
                             duration_ms=5_000, timed_out=False, stdout="PASS all", stderr="")

    calls = 0

    def _run_once():
        nonlocal calls
        calls += 1
        return pass_res, f"PASS attempt {calls + 1}", "npm test", "app"

    budget = {"checks": 1, "rerun_ms": 0}
    flaky_tests: list[str] = []
    res, out, _cmd, _cwd = gcts._run_flaky_reruns(
        first_res,
        "FAIL test_a (attempt 1)",
        "npm test",
        "app",
        ["test_a"],
        run_once=_run_once,
        flaky_budget=budget,
        flaky_tests=flaky_tests,
        max_total_rerun_ms=120_000,
    )
    assert calls == 2
    assert res is pass_res
    assert res.ok is True
    assert out == "PASS attempt 3"
    assert flaky_tests == ["test_a"]
    assert budget == {"checks": 2, "rerun_ms": 10_000}


def test_run_flaky_reruns_returns_tampered_second_attempt_without_third():
    first_res = CommandResult(command="npm test", argv=["npm", "test"], exit_code=1,
                              duration_ms=1_000, timed_out=False, stdout="", stderr="FAIL")
    tampered_res = CommandResult(command="", argv=[], exit_code=1, duration_ms=0,
                                 timed_out=False, stdout="", stderr="tampered", tampered=True)

    calls = 0

    def _run_once():
        nonlocal calls
        calls += 1
        return tampered_res, "[boss] boss.verify.yaml modified", "", "."

    budget = {"checks": 1, "rerun_ms": 0}
    flaky_tests: list[str] = []
    res, out, _cmd, _cwd = gcts._run_flaky_reruns(
        first_res,
        "FAIL attempt 1",
        "npm test",
        ".",
        ["test_a"],
        run_once=_run_once,
        flaky_budget=budget,
        flaky_tests=flaky_tests,
        max_total_rerun_ms=120_000,
    )
    assert calls == 1
    assert res is tampered_res
    assert out == "[boss] boss.verify.yaml modified"
    assert flaky_tests == []
