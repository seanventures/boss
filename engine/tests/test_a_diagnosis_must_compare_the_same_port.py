"""A confident wrong diagnosis is worse than "unreachable" — it sends somebody to fix the
wrong machine.

HookWatch failed its health check four times. Each time BOSS reported:

    the app IS serving on the host (localhost:3020 returns 200), so port 3020 is being
    blocked before it reaches BOSS — a firewall or cloud security group, not the deployment.

and each time the operator was told to open a firewall rule. It would have changed nothing.

The external check had probed `http://192.0.2.202:8000/` — the stale port in the target's
`domains` — while the container was published on 3020. `_why_unreachable` compared "3020 works
locally" against "the external check failed" without noticing those were different ports, and
leapt to the network.

The port was never blocked. A sibling app on the same host answers on 3010 from outside.

This string is written into `failure_reason`, so the agent repeats it verbatim, forever. The
agent was not making it up: BOSS manufactured it.
"""
from __future__ import annotations

import inspect

from boss_engine.services.deployment_connectors import SSHContainerConnector


class TestItChecksTheObviousThingFirst:
    def _source(self) -> str:
        return inspect.getsource(SSHContainerConnector._why_unreachable)

    def test_the_probed_url_is_available_to_the_diagnosis(self):
        """It could not compare the ports because it was never told which one failed."""
        sig = inspect.signature(SSHContainerConnector._why_unreachable)
        assert "probed_url" in sig.parameters

    def test_a_port_mismatch_is_reported_as_a_mismatch(self):
        body = self._source()
        assert 'probed_port != str(app_port)' in body
        assert "stale address on the target, not a firewall" in body

    def test_it_names_both_ports_so_the_operator_can_check(self):
        body = self._source()
        assert "{probed_port}" in body and "{app_port}" in body

    def test_the_firewall_verdict_is_still_available_for_the_real_case(self):
        """A genuinely blocked port is a real failure mode; it just is not this one."""
        body = self._source()
        assert "firewall or cloud security group" in body

    def test_the_mismatch_is_checked_before_the_network_is_blamed(self):
        body = self._source()
        assert body.index("stale address on the target") < body.index("firewall or cloud")


class TestObservePassesTheUrl:
    def test_the_failing_url_reaches_the_diagnosis(self):
        body = inspect.getsource(SSHContainerConnector.observe)
        assert "self._why_unreachable(target, settings, url)" in body


class TestTheAgentIsToldToTestBeforeRepeating:
    """Why BOSS repeated it four times: the failure_reason held a confident conclusion, and
    repeating a stored conclusion is cheaper than testing it.

    The counter-evidence was one http_probe away — a sibling app answering on its own port from
    outside the same host — and the agent already has that tool.
    """

    def _prompt(self) -> str:
        from boss_engine.services import intent_router

        return inspect.getsource(intent_router)

    def test_it_is_told_a_recorded_diagnosis_is_a_claim(self):
        assert "A RECORDED DIAGNOSIS IS A CLAIM, NOT A FACT" in self._prompt()

    def test_it_is_told_to_look_for_disproof(self):
        assert "DISPROVE" in self._prompt()

    def test_it_is_told_not_to_restate_the_problem_after_approval(self):
        prompt = self._prompt()
        assert "DO NOT RESTATE THE PROBLEM" in prompt
        assert "say plainly which tool you lack" in prompt
