"""A security finding is fixed in the same warm round as a review finding, not shipped advisory.

A HelpDesk-lite build (crun_835c2c5c) finished with its reviewer AND its security seat naming one
real bug: async Express handlers with no error forwarding, so a bad id or an array in a login
body crashed the whole process. `security_clean` was not convergeable, so the build skipped the
in-run fix round and shipped both findings as "advisory". Now the security findings go to the
implementer's warm session with the review's, and the security seat re-reviews the delta.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g


def test_security_is_convergeable_and_its_findings_reach_the_implementer():
    src = inspect.getsource(g)
    block = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    assert "security_clean" in g._CONVERGEABLE
    assert 'header="SECURITY FINDINGS:"' in block
    # The failed security seat re-reviews the delta, and only it decides the gate.
    assert "security_agent.run_role(" in block
    assert 'gates["security_clean"] = sec_verdict == "clean"' in block
    assert '"security_clean", "tests_strong_enough"}:\n                        break' in block


def test_the_failed_seats_re_review_side_by_side():
    src = inspect.getsource(g)
    block = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    # Side by side, under the same per-seat deadline as the first panel (review_seats.run_seats).
    assert "_rs_run = _run_seats(" in block
    assert "seat_deadline_s=_rs_deadline" in block
    # Every READ-ONLY seat call lives in a seat closure, none runs inline after the wait. The
    # one exception is the weak-suite probe, which edits the tree and so runs after them, alone.
    after = block[block.index("_rs_run = _run_seats("):]
    inline = after[:after.index("if not failing:")]
    assert inline.count(".run_role(") == 1
    assert "a_mu.run_role(_mutate_prompt(" in inline
