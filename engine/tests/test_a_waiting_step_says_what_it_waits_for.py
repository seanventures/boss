""""waiting on a decision" must mean there is a decision.

A run was building normally -- its research step had started a discovery run and was waiting
for it to finish -- and the console said:

    waiting  0 of 10  started 21 Sep 03:10Z  waiting on a decision

There was no decision. `decision_id` was null on the step and on the run. The operator went
looking for a gate that did not exist, on a run that was working.

The cause was a fallback: any waiting step without an approver was labelled "a decision".
Sending someone after a decision that is not there costs them the trip, and then it costs the
console its credibility -- the next genuine "waiting on you" reads like noise.
"""

from __future__ import annotations

from boss_engine.services.workflows import waiting_on_label


def test_a_step_awaiting_research_does_not_claim_a_decision():
    """The exact shape recorded by the run that exposed this."""
    step = {
        "status": "waiting",
        "output": {
            "tool": "create_discovery",
            "await": {"id": "drun_864aa33554b14eb0b541", "kind": "discovery"},
            "status": "awaiting",
            "capability": "research",
        },
    }
    assert waiting_on_label(step) == "the research to finish"


def test_each_kind_of_awaited_work_is_named_in_the_operators_words():
    """The four kinds a step can await, each named after the step the console already shows,
    so "waiting on ..." points at something visible on the same screen."""
    expected = {
        "discovery": "the research to finish",
        "meeting": "the panel to finish",
        "code_task": "the build to finish",
        "preview": "the preview to come up",
    }
    for kind, label in expected.items():
        assert waiting_on_label({"output": {"await": {"kind": kind, "id": "x"}}}) == label


def test_a_named_approver_still_wins():
    """The case that always worked: a gate with a role attached names the role, because that
    tells the operator whether it is theirs to clear."""
    step = {"approval": {"approver_role": "release_manager"}, "decision_id": "dec_1"}
    assert waiting_on_label(step) == "release_manager"


def test_a_real_decision_is_still_called_a_decision():
    """The fallback was not wrong in every case -- only in the ones with no decision. A step
    carrying a decision id is genuinely waiting on one."""
    assert waiting_on_label({"decision_id": "dec_36860513f7964096a13b"}) == "a decision"


def test_an_unattributed_wait_says_nothing_rather_than_guessing():
    """Silence is the honest answer to a question we cannot answer. The status pill already
    says `waiting`; inventing a reason is what caused this bug."""
    assert waiting_on_label({"status": "waiting", "output": {}}) is None
    assert waiting_on_label({}) is None


def test_an_unknown_await_kind_does_not_invent_a_label():
    """A kind this map has not seen yet must not be reported as a decision either."""
    assert waiting_on_label({"output": {"await": {"kind": "something_new", "id": "x"}}}) is None


def test_the_endpoint_uses_the_helper_rather_than_its_own_fallback():
    """The literal fallback must not survive anywhere in the endpoint, or the bug comes back
    for whichever code path still carries it."""
    from pathlib import Path

    main = Path(__file__).resolve().parents[2] / "apps/api/app/main.py"
    source = main.read_text(encoding="utf-8")
    assert "waiting_on_label(current)" in source
    assert 'approver_role") or "a decision"' not in source


def test_a_code_task_that_is_not_the_build_is_not_called_the_build():
    """Readiness extends the build's code task; its run said "waiting on the build to finish"
    while the build step beside it read succeeded."""
    plrr = {"capability": "plrr", "output": {"await": {"kind": "code_task", "id": "crun_2"}}}
    assert waiting_on_label(plrr) == "the readiness fixes to finish"
    other = {"output": {"capability": "document", "await": {"kind": "code_task", "id": "crun_3"}}}
    assert waiting_on_label(other) == "the code work to finish"
    build = {"capability": "build", "output": {"await": {"kind": "code_task", "id": "crun_1"}}}
    assert waiting_on_label(build) == "the build to finish"
