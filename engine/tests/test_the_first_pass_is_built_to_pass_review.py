"""The first pass is built to pass review, and a fix round re-reviews only what it changed.

A HelpDesk-lite build with a PR (crun_4813f68c, 27.4 min) spent 10.5 min in two fix rounds and
6.5 min in its first review, on findings the implementer could have checked itself: async
handlers whose rejected queries crashed the process, an unvalidated id reaching SQL, an array in
a login body. So:
(a) the implementer gets the reviewers' checklist up front, with a one-minute self-check;
(b) a fix round's re-review sees the seat's own findings and only the files the fix changed
    (still in parallel, still blocking: an unresolved finding is raised again);
(c) every review seat's reasoning per step is bounded, not how much it may read.
"""
from __future__ import annotations

import inspect

from boss_engine.services import code_agent
from boss_engine.services import governed_code_task_service as g

CRITERIA = ["Customers file tickets"]


def test_the_implementer_gets_the_reviewers_checklist_and_a_self_check():
    rules = g.FIRST_PASS_CHECKLIST
    for words in ("BEFORE YOU HAND OFF", "one minute", "error handler", "role or ownership",
                  "never a 500", "from the environment", "no unused dependencies"):
        assert words in rules
    assert rules in g._impl_prompt("t", "i", ["t.test.js"], "npm test", criteria=CRITERIA)
    assert rules in g._solo_prompt("t", "i", CRITERIA, "npm test", web=True)
    # The test author does not get it: its own rules are about the tests.
    assert rules not in g._qa_prompt("t", "i", CRITERIA, "npm test", web=True)


def test_a_fix_round_re_review_sees_its_findings_and_only_the_fixed_files():
    text = g._fix_round_change(
        "FINDING: MAJOR server/users.js:21 — no numeric guard on :id",
        {"server/users.js": "router.patch('/:id', guard, handler)\n"})
    assert "FIX ROUND RE-REVIEW" in text and "no numeric guard on :id" in text
    assert "server/users.js" in text and "raise it again" in text
    assert "Do not re-review unchanged code" in text


def test_the_round_scopes_each_seat_and_falls_back_to_the_whole_change():
    src = inspect.getsource(g)
    block = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    for prior in ("_rc_prior", "_r6_prior", "_r4_prior"):
        assert f"change=_seat_change({prior})" in block
    assert "if not _fixed:\n                            return change_block" in block


def test_every_review_seat_has_a_bounded_reasoning_budget():
    for stage in ("review_code", "security_check", "consistency_check"):
        assert code_agent.STAGE_THINKING_CAPS[stage] <= 6000
