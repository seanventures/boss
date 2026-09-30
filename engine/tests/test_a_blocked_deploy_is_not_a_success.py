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
        reason = str(output.get("error") or detail or output.get("note") or "")
        return reason if reason.strip() else runner._reason_of_last_resort(output, {})

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


class TestTheSentenceThatExplainedNothing:
    """On the Deploy step of the Interview Scorecard App an operator was shown, in full:

        the capability reported failure without a reason

    That is not an error message. It names nothing to look at, suggests nothing to try, and
    implies the system knows what happened when it does not. The operator asked BOSS what it
    meant; BOSS could not tell them either, because the step's stored `output` was `{}` — the
    runner had thrown the capability's return value away on the failure path.
    """

    def test_the_placeholder_is_gone_from_the_runner(self):
        import inspect

        body = inspect.getsource(runner)
        # Only in the comment explaining why it was removed, never as a value.
        assert 'or "the capability reported failure without a reason"' not in body

    def test_whatever_the_capability_did_return_is_shown(self):
        """A dict with keys nobody planned for is still evidence."""
        reason = runner._reason_of_last_resort(
            {"capability": "deploy_app", "status": "failed",
             "exit_code": 125, "host": "203.0.113.220"}, {})
        assert "exit_code=125" in reason
        assert "host=203.0.113.220" in reason

    def test_a_truly_empty_failure_says_so_and_names_what_to_do(self):
        reason = runner._reason_of_last_resort(
            {"capability": "deploy_app", "status": "failed", "tool": "redeploy"}, {})
        assert "returned nothing at all" in reason
        assert "redeploy" in reason
        assert "read_logs(service='worker')" in reason
        assert "diagnose_run" in reason

    def test_it_says_whose_defect_it_is(self):
        """An operator reading a blank failure assumes their app is broken. It is not."""
        reason = runner._reason_of_last_resort({"capability": "deploy_app"}, {})
        assert "defect in the capability, not a description of your app" in reason

    def test_it_names_the_step_when_the_capability_is_unnamed(self):
        assert "deploy" in runner._reason_of_last_resort({}, {"step_id": "deploy"})

    def test_it_never_returns_an_empty_string(self):
        for output in ({}, {"status": "failed"}, {"capability": None}):
            assert runner._reason_of_last_resort(output, {}).strip()

    def test_the_bookkeeping_keys_are_not_reported_as_findings(self):
        """`status: failed` as the explanation for a failure is a tautology."""
        reason = runner._reason_of_last_resort(
            {"capability": "deploy_app", "status": "failed", "inputs": {"app": "x"}}, {})
        assert "returned nothing at all" in reason

    def test_the_evidence_survives_the_failure(self):
        """The step stored `{}`, so there was nothing for anyone — operator, BOSS, or the next
        engineer — to read afterwards."""
        import inspect

        body = inspect.getsource(runner)
        assert "reason = _reason_of_last_resort(output, claimed)" in body
        assert body.index("_reason_of_last_resort(output, claimed)") < body.index("output=output,")

    def test_fail_step_merges_rather_than_overwrites(self):
        """A capability's output and the runner's own bookkeeping both belong on the step."""
        import inspect

        from boss_engine.services import workflows

        body = inspect.getsource(workflows.fail_step)
        assert "COALESCE(output, '{}'::jsonb) || CAST(:output AS JSONB)" in body
