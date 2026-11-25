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
