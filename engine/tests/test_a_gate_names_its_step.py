"""A gate's sentence names the STEP it holds, not the capability under it.

Command Center re-test (2026-10-04): every gate on a "Release brief" run said "Approve before
'Notify' runs", because the sentence used the capability's label. Three cards, one sentence.
"""
from boss_engine.services import workflow_capability_runner as runner


class _NoDb:
    def execute(self, *a, **k):  # the manual path reads nothing
        raise AssertionError("a manual capability gate needs no read")


def test_a_manual_capability_gate_says_the_steps_own_name():
    step = {"id": "gate", "kind": "capability", "label": "Go live", "captain": "release_manager",
            "binding": {"capability": "notify", "inputs": {}},
            "approval": {"mode": "manual", "risk_tier": 1}}
    got = runner._gate_subject(_NoDb(), "p", step)
    assert got is not None and got["summary"] == "Approve before 'Go live' runs."


def test_a_step_with_no_name_falls_back_to_its_capability():
    assert runner._step_word({}, "Notify") == "Notify"
    assert runner._step_word({"label": "  "}, "Notify") == "Notify"
    assert runner._step_word({"name": "Send it"}, "Notify") == "Send it"
