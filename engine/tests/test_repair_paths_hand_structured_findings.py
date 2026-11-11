"""BOSSAPP-960: every repair path hands the next attempt structured findings, not a stdout tail.

The only repair channel that worked was the structured one (review comments: severity, path,
line, quoted source). Tests here pin the shared shape (repair_findings), that the paths which
fed prose now feed it, and that nothing parseable is required — evidence that does not parse
still arrives as the legacy tail.
"""
from __future__ import annotations

import inspect

from boss_engine.services import repair_findings as rf


PYTEST_OUTPUT = """\
tests/test_cart.py::test_empty_cart_total PASSED
tests/test_cart.py::test_discount_applies FAILED
=================================== FAILURES ===================================
E   assert total == 90
FAILED tests/test_cart.py::test_discount_applies - AssertionError: assert 100 == 90
ERROR tests/test_api.py - ImportError: cannot import name 'app'
=========================== short test summary info ============================
"""

TSC_OUTPUT = """\
> tsc --noEmit
src/App.tsx(12,5): error TS2304: Cannot find name 'cart'.
src/lib/total.ts:7:3 - error TS2322: Type 'string' is not assignable to type 'number'.
"""


def test_pytest_failures_parse_to_test_file_and_assertion():
    found = rf.find(PYTEST_OUTPUT)
    tests = {f.failing_test: f for f in found if f.failing_test}
    key = "tests/test_cart.py::test_discount_applies"
    assert key in tests
    assert tests[key].path == "tests/test_cart.py"
    assert "assert 100 == 90" in (tests[key].assertion or "")
    assert tests[key].quote and "FAILED" in tests[key].quote
    errors = [f for f in found if f.severity == "BLOCKER"]
    assert any("ImportError" in (f.assertion or f.text) for f in errors)


def test_compiler_errors_parse_with_path_and_line():
    found = rf.find(TSC_OUTPUT)
    by_path = {(f.path, f.line): f for f in found}
    assert ("src/App.tsx", 12) in by_path
    assert ("src/lib/total.ts", 7) in by_path
    assert all(f.severity == "BLOCKER" for f in found)


def test_render_speaks_the_review_channels_finding_grammar():
    from boss_engine.services.review_comments import parse_findings

    found = rf.find(TSC_OUTPUT)
    rendered = rf.render(found)
    comments = parse_findings(rendered)
    assert {(c.path, c.line) for c in comments} >= {("src/App.tsx", 12), ("src/lib/total.ts", 7)}


def test_unparseable_output_still_arrives_as_the_legacy_tail():
    block = rf.evidence_block("segfault at 0xdeadbeef\n" * 3, command="npm test", exit_code=1)
    assert block.startswith("$ npm test (exit 1)")
    assert "segfault" in block
    assert "STRUCTURED FINDINGS" not in block


def test_evidence_block_leads_with_findings_and_keeps_a_short_tail():
    block = rf.evidence_block(PYTEST_OUTPUT, command="pytest -q", exit_code=1)
    assert block.startswith("$ pytest -q (exit 1)")
    assert "STRUCTURED FINDINGS" in block
    assert "test_discount_applies" in block
    assert "(end of raw output, for context)" in block


def test_a_ci_failure_becomes_blocker_findings_naming_each_check():
    found = rf.from_ci(["unit-tests", "lint"], summary="CI failed: 'unit-tests' failed",
                       url="https://ci.example/run/1")
    names = {f.failing_test for f in found if f.failing_test}
    assert names == {"unit-tests", "lint"}
    assert all(f.severity == "BLOCKER" for f in found if f.failing_test)
    assert any("https://ci.example/run/1" in f.text for f in found)


def test_a_cold_retry_is_told_the_failing_tests_not_only_the_tail():
    from boss_engine.services.code_task_service import previous_failure_block

    prev = {"state": {"last_verify_output": PYTEST_OUTPUT},
            "finalize_ctx": {"failure_class": "terminal"}}
    block = previous_failure_block(prev, gates_failed=["written_tests_pass"])
    assert "Findings" in block
    assert "tests/test_cart.py::test_discount_applies" in block
    assert "Gates failed: written_tests_pass" in block


def test_the_shepherds_fix_task_and_the_warm_loop_use_the_shared_shape():
    import boss_engine.services.governed_code_task_service as gov
    import boss_engine.services.shepherd_service as shepherd

    assert "evidence_block" in inspect.getsource(shepherd)
    assert "repair_findings.evidence_block" in inspect.getsource(gov)


def test_a_ci_failure_triggers_one_bounded_repair_round(monkeypatch):
    """The guard logic, without a database: no repair on pass, one on failure, never a second
    on the attempt that IS the CI repair."""
    import boss_engine.services.governed_code_task_service as gov

    calls = []
    monkeypatch.setattr("boss_engine.services.code_task_service.retry_code_task",
                        lambda *a, **k: calls.append(k) or {"run_id": "crun_repair"})
    events = []

    class _Tx:
        def __enter__(self):
            class C:
                def execute(self, *a, **k):
                    return None
            return C()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gov, "_transaction", lambda engine: _Tx())
    monkeypatch.setattr(gov, "emit_event", lambda *a, **k: events.append(a[2]))

    # CI passed: nothing starts.
    assert gov.repair_after_ci_failure(None, None, None, "r1", "p1", {},
                                       {"status": "passed"}) is None
    assert not calls
    # CI failed: one repair, carrying the failing check as a structured finding.
    got = gov.repair_after_ci_failure(None, None, None, "r1", "p1", {},
                                      {"status": "failed",
                                       "summary": "CI failed: 'unit-tests' failed",
                                       "failing_url": "https://ci/x"})
    assert got == "crun_repair"
    assert len(calls) == 1 and calls[0]["automatic"] is True
    assert "unit-tests" in calls[0]["fix_note"]
    # The attempt that IS the CI repair is never repaired again.
    assert gov.repair_after_ci_failure(None, None, None, "r2", "p1",
                                       {gov.CI_REPAIR_KEY: True},
                                       {"status": "failed", "summary": "x"}) is None
    assert len(calls) == 1
