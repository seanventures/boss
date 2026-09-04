"""Two screens disagreed about one deployment, and the workflow was the one lying.

An operator saw Deploy ✓ succeeded 0s in a completed workflow, opened Infra, and found the
same release marked BLOCKED. The app's URL answered nothing. Asked which was correct, the
honest answer was neither: Infra was right about the deployment, and the workflow was wrong
about it.

    deployment_connectors returns {"status": "blocked", "problems": [...]} when preflight
    refuses. The runner failed a step only on status == "failed". "blocked" fell through to
    the success path — so the run recorded Deploy as done and advanced to monitoring.

The underlying refusal was correct and its message was exact:

    "no ssh_key bound to this target and no global ssh key configured
     (bind a secret to the target, or set BOSS_SSH_HOST_PRIVATE_KEY_FILE)"

That sentence never reached the operator either. The step's detail rendered the OUTPUT'S SHAPE
— "{status, problems}" — instead of what was in it.
"""
from __future__ import annotations

import pytest

from boss_engine.services import workflow_capability_runner as runner


class TestWhatCountsAsNotShipping:
    @pytest.mark.parametrize("status", ["failed", "blocked", "refused", "error"])
    def test_a_step_that_did_not_do_the_work_is_not_a_success(self, status):
        assert status in runner._DID_NOT_SHIP

    @pytest.mark.parametrize("status", ["succeeded", "awaiting", "needs_decision", "skipped"])
    def test_ordinary_outcomes_are_left_alone(self, status):
        """`awaiting` especially: parking a step is how async work is tracked, not a failure."""
        assert status not in runner._DID_NOT_SHIP

    def test_blocked_is_the_one_that_was_missing(self):
        """The regression itself. Every deploy refusal reported success before this."""
        assert "blocked" in runner._DID_NOT_SHIP


class TestTheReasonReachesTheOperator:
    """`problems` carries the cause; the step used to show the dict's keys instead."""

    @staticmethod
    def _reason_for(output: dict) -> str:
        """The runner's extraction, exercised the way the runner does it."""
        problems = output.get("problems")
        if isinstance(problems, (list, tuple)) and problems:
            detail = "; ".join(
                str(p.get("detail") or p.get("message") or p.get("reason") or p)
                if isinstance(p, dict) else str(p)
                for p in problems)
        else:
            detail = ""
        return str(output.get("error") or detail or output.get("note")
                   or "the capability reported failure without a reason")

    def test_the_real_refusal_is_reported_verbatim(self):
        output = {
            "status": "blocked",
            "problems": ["no ssh_key bound to this target and no global ssh key configured"],
        }
        assert "no ssh_key bound to this target" in self._reason_for(output)

    def test_structured_problems_are_read_not_stringified_as_dicts(self):
        output = {"status": "blocked",
                  "problems": [{"detail": "host unreachable"}, {"message": "port in use"}]}
        assert self._reason_for(output) == "host unreachable; port in use"

    def test_an_explicit_error_wins_over_the_problem_list(self):
        output = {"status": "failed", "error": "connection refused", "problems": ["noise"]}
        assert self._reason_for(output) == "connection refused"

    def test_a_refusal_with_nothing_to_say_still_says_something(self):
        assert self._reason_for({"status": "blocked"}).strip()
