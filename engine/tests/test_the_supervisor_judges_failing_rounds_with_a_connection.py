"""The run supervisor judges failing rounds, through a real connection (2026-10-31).

boss-1, crun_73e64e45: the code-task hook handed `decide` the Engine, so every judgement died
with "'Engine' object has no attribute 'execute'" and fell closed to stop_report; the
supervisor had never consulted a model or Jev. And the round cap tripped on a round with no
faults, so that fail-closed stop parked a build that had just converged ("awaiting gate
override, failing gates: []")."""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import run_supervisor


def _signal(faults, rounds_used):
    return run_supervisor.evaluate_signal(
        faults=faults, rounds_used=rounds_used, max_repair_rounds=3, history=[],
        spend_usd_micros=0, started_at=None, stall_minutes=15, elapsed_seconds=60)


def test_a_round_with_no_faults_is_never_capped():
    signal = _signal({}, rounds_used=5)
    assert not signal.forced and not signal.tripped


def test_a_failing_round_past_the_cap_is_still_judged():
    signal = _signal({"code_review_passed": run_supervisor.WORK}, rounds_used=3)
    assert signal.forced


def test_the_code_task_hook_judges_only_failing_rounds_and_on_a_connection():
    src = inspect.getsource(g)
    assert "if _signal_on and gate_faults:" in src
    hook = src[src.index("_decision = _supervisor.decide(") - 200:
               src.index("_decision = _supervisor.decide(") + 120]
    assert "with _transaction(engine) as _jc:" in hook
    assert "_supervisor.decide(\n                                    _jc," in hook



def test_a_failing_round_reaches_the_judgement_and_its_action_stands():
    seen = []

    def adapter(evidence):
        seen.append(evidence)
        return {"action": run_supervisor.CHANGE_APPROACH}

    signal = _signal({"code_review_passed": run_supervisor.WORK}, rounds_used=3)
    ok = run_supervisor.decide(object(), None, "p", run_id="r", signal=signal, history=[],
                               adapter=adapter)
    assert ok["action"] == run_supervisor.CHANGE_APPROACH and seen
