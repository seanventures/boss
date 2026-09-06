"""BOSS diagnosed one deployment four times and fixed nothing. Why is the interesting part.

The operator approved a fix three separate times and got the same paragraph back:

    port 3020 is blocked by a firewall/security group ... let me know which port you'd like
    to open and I'll update the target and retry.

Three things were true at once, and only the third is about tools:

  1. THE DIAGNOSIS WAS WRONG. The health check probed 2.29.2.202:8000 — the stale port in the
     target's `domains` — while the container published on 3020. Two facts about different
     ports, compared as though they were about one. There was no firewall: a sibling app on
     that host answers on 3010 from outside, and 3020 itself returns 200.

  2. BOSS DID NOT TEST IT. `failure_reason` held a CONCLUSION written by BOSS's own health
     checker, and BOSS repeated it. It had http_probe the whole time; one call would have
     disproved the firewall theory.

  3. IT COULD NOT HAVE ACTED ANYWAY. There was no tool to change a target, so "tell me which
     port and I'll update the target" was an offer BOSS could not honour — which from outside
     is indistinguishable from refusing to help.

Fixing only (3) would have left it confidently changing the wrong thing. So: the checker no
longer manufactures the firewall claim, the agent is told a recorded diagnosis is a claim, and
the contradiction is now something a tool STATES rather than something a model must notice.
"""
from __future__ import annotations

import pytest

from boss_engine.tools.builtin import build_default_registry


@pytest.fixture
def tools():
    return {spec.name: spec for spec in build_default_registry().specs()}


class TestItCanSeeTheContradiction:
    def test_a_tool_reports_where_a_target_disagrees_with_itself(self, tools):
        assert "describe_deployment_target" in tools
        assert tools["describe_deployment_target"].kind == "read"

    def test_the_port_mismatch_is_named_and_the_fix_is_named(self, tools):
        import inspect

        body = inspect.getsource(tools["describe_deployment_target"].handler)
        assert "THE ADDRESS NAMES THE WRONG PORT" in body
        # And says what to do, because "mismatch" without a remedy is another dead end.
        assert "retarget_app(port=" in body
        assert "not with a firewall rule" in body

    def test_it_also_catches_the_states_that_silently_hide_an_app(self, tools):
        """Unbound and retired both make an app un-shippable while looking fine."""
        import inspect

        body = inspect.getsource(tools["describe_deployment_target"].handler)
        assert "no host bound" in body
        assert "lists only active" in body


class TestItCanActOnWhatItFinds:
    def test_there_is_a_tool_to_correct_the_binding(self, tools):
        assert "retarget_app" in tools
        assert tools["retarget_app"].kind == "write"

    def test_it_writes_both_records_together(self, tools):
        """`domains` and `app_port` are two spellings of one fact; letting them drift is the
        entire bug this tool repairs."""
        import inspect

        body = inspect.getsource(tools["retarget_app"].handler)
        assert "required_resources = CAST(:r AS JSONB), " in body
        assert "domains = CAST(:d AS JSONB)" in body

    def test_it_can_revive_a_retired_target(self, tools):
        import inspect

        assert "activate" in inspect.getsource(tools["retarget_app"].handler)

    def test_it_is_reachable_from_chat(self):
        """A write BOSS owns but may not call is a tool that does not exist."""
        from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS

        assert "retarget_app" in _DISCUSS_OPERATIONS_TOOLS


class TestItCannotClaimAnActionItDidNotTake:
    """The half that still failed after the tools existed.

    Given the new tools and explicit approval, BOSS read the target, got the diagnosis exactly
    right — "the health check was probing port 8000 while the container publishes on 3020" —
    and then wrote "Action taken: retargeted HookWatch to publish on port 3020." It had called
    one tool, a READ. The target was unchanged; `actions` was empty.

    An operator reading that has been told their problem is solved, goes and looks at a
    still-broken app, and has no reason to trust the next answer either. That is worse than
    refusing, because refusing is honest.

    `actions` records the writes that really ran, so the claim is checkable against the trace
    in the same turn it is made.
    """

    def test_a_completion_claim_with_no_write_is_corrected(self):
        from boss_engine.services.intent_router import _no_action_was_claimed

        out = _no_action_was_claimed("**Action taken**: retargeted HookWatch.", [])
        assert "nothing above was actually changed" in out

    @pytest.mark.parametrize("claim", [
        "I've corrected the deployment target to use the proper port.",
        "The target has been updated.",
        "I have redeployed the app.",
        "Action taken: retargeted HookWatch.",
    ])
    def test_it_catches_the_phrasings_a_model_reaches_for(self, claim):
        from boss_engine.services.intent_router import _no_action_was_claimed

        assert "nothing above was actually changed" in _no_action_was_claimed(claim, [])

    def test_a_claim_backed_by_a_real_write_is_left_alone(self):
        from boss_engine.services.intent_router import _no_action_was_claimed

        claim = "I've corrected the target."
        assert _no_action_was_claimed(claim, [{"kind": "retarget_app"}]) == claim

    def test_a_plan_is_not_a_claim(self):
        """"I will update the target" is an intention and must not be contradicted."""
        from boss_engine.services.intent_router import _no_action_was_claimed

        plan = "I will update the target and retry."
        assert _no_action_was_claimed(plan, []) == plan

    def test_a_pure_diagnosis_is_left_alone(self):
        from boss_engine.services.intent_router import _no_action_was_claimed

        text = "The health check probes 8000 while the container publishes on 3020."
        assert _no_action_was_claimed(text, []) == text

    def test_the_diagnosis_is_kept_not_deleted(self):
        """The correct half of the answer is the valuable half; appending beats rewriting."""
        from boss_engine.services.intent_router import _no_action_was_claimed

        reply = "Probing 8000, published on 3020. I've corrected the target."
        out = _no_action_was_claimed(reply, [])
        assert "Probing 8000, published on 3020." in out


class TestAClaimBecomesAnAttempt:
    """Telling the operator the truth is not the same as doing what they asked.

    With the tool in its schema list and explicit approval, BOSS narrated the fix instead of
    calling anything. The correction note made that visible — and still left the operator to
    ask a second time for something they had already approved.

    So a completion claim with no write is treated as the intent it plainly is: one more turn,
    told that nothing ran and to call the tool now.
    """

    def _router(self) -> str:
        import inspect

        from boss_engine.services import intent_router

        return inspect.getsource(intent_router)

    def test_a_claim_with_no_action_triggers_another_attempt(self):
        body = self._router()
        # Now gated on the turn being an instruction. That retry tells the model "they have
        # already approved it — call the tool NOW", which is false on a question and would
        # take the action the write gate had just declined.
        assert "not _looking_only" in body
        assert "and _ACTION_CLAIM.search(reply)" in body
        assert "Call the tool that performs it NOW" in body

    def test_the_retry_may_write(self):
        assert "allow_writes=True, max_steps=4," in self._router()

    def test_the_retry_is_told_to_name_a_missing_tool_rather_than_re_describe(self):
        body = self._router()
        assert "name the tool you" in body
        assert "do not describe the change again" in body


class TestWhatCountsAsHavingActed:
    """The list of acting tools drifted, which would have made a genuine fix look like a lie.

    `deploy_app`, `merge_apps` and `retarget_app` were all absent, so a turn that really did
    retarget an app recorded `actions: []` — the console would show nothing and the honesty
    guard would call a real fix a fabrication. The registry already knows which tools write.
    """

    def test_writes_count_as_actions(self):
        from boss_engine.services.intent_router import _is_acting

        for tool in ("retarget_app", "merge_apps", "deploy_app", "redeploy"):
            assert _is_acting(tool), tool

    def test_reads_do_not(self):
        from boss_engine.services.intent_router import _is_acting

        for tool in ("describe_deployment_target", "list_deployments", "search_code"):
            assert not _is_acting(tool), tool

    def test_a_new_write_needs_no_second_edit(self):
        """Derived from the registry, so the day a write is added it is already an action."""
        import inspect

        from boss_engine.services import intent_router

        assert 'spec.kind == "write"' in inspect.getsource(intent_router._acting_tools)
