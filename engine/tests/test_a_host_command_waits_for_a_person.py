"""A command that changes a machine waits for a person to press the button.

The operator drew the line after the 2026-09-02 wipe, and it is the right one: an agent working
WITH a person can be trusted with real power, because the person is the gate. An agent acting
alone, autonomously, destroying things nobody asked about, is a different thing entirely.

So `run_on_host` is a real shell on a real host — it can delete containers and take the box
down — and it cannot fire on the model's own initiative. Two independent gates:

  1. BOSS_AGENT_HOST_SHELL must be on. Off by default; nobody inherits a shell by upgrading.
  2. a privileged confirmation argument, which the action endpoint sets after
     clicking a proposed action, which deliberately does not go back through the model.

Refusing tells the model what to do instead: propose it, so the exact host and the exact
command appear on a button. That is the whole feature — not that the command is filtered, but
that a person sees it before it runs.
"""

from __future__ import annotations

from boss_engine.tools import build_default_registry, build_discuss_read_registry


class _Settings:
    def __init__(self, shell: bool, edition: str = "enterprise_self_hosted"):
        self.agent_host_shell = shell
        self.boss_edition = edition


class _Ctx:
    """Enough context to reach the guards and no further — if a test ever gets past them it
    will fail on the missing database rather than quietly SSH somewhere."""

    durable_workspace_configured = True

    def __init__(self, shell: bool = True, edition: str = "enterprise_self_hosted"):
        self.settings = _Settings(shell, edition)

    def begin(self):
        raise AssertionError(
            "run_on_host reached the database — it should have refused before this"
        )


def _run(args, *, shell=True):
    reg = build_default_registry()
    return reg.invoke("run_on_host", _Ctx(shell), project_id="p", actor="ceo", args=args)


class TestItWillNotRunOnTheModelsSayS0:
    def test_a_direct_call_is_refused(self):
        out = _run({"host": "hetzner-ci-01", "command": "docker rm -f $(docker ps -aq)"})
        assert out["ok"] is False
        assert out["requires_confirmation"] is True

    def test_the_refusal_says_how_to_get_it_in_front_of_the_operator(self):
        out = _run({"host": "hetzner-ci-01", "command": "docker ps"})
        # `propose_action`, not `propose_command` — which this refusal named for months and
        # which has never been a registered tool. The refusal was sending the model to nothing.
        assert "propose_action" in out["do_this"]
        # The args come back so the model proposes the SAME command, not a paraphrase of it.
        assert out["proposed_args"]["command"] == "docker ps"
        assert out["proposed_args"]["host"] == "hetzner-ci-01"

    def test_the_command_is_never_reworded_between_asking_and_running(self):
        """A confirmation button is worthless if what runs is not what was shown."""
        command = "docker rm -f bugbox shorty && docker system prune -af"
        out = _run({"host": "h", "command": command})
        assert out["proposed_args"]["command"] == command


class TestTheFlagIsTheOuterGate:
    def test_community_refuses_even_with_flag_and_confirmation(self):
        out = build_default_registry().invoke(
            "run_on_host", _Ctx(True, "community"), project_id="p", actor="ceo",
            args={"host": "h", "command": "docker ps"}, operator_confirmed=True,
        )
        assert out["ok"] is False
        assert "Community host shell" in out["error"]

    def test_disabled_refuses_even_with_confirmation(self):
        out = build_default_registry().invoke(
            "run_on_host", _Ctx(False), project_id="p", actor="ceo",
            args={"host": "h", "command": "docker ps"}, operator_confirmed=True)
        assert out["ok"] is False
        assert "BOSS_AGENT_HOST_SHELL" in out["error"]

    def test_disabled_is_the_default(self):
        from boss_engine.config import Settings

        assert Settings.model_fields["agent_host_shell"].default is False


class TestOnlyTheAcceptPathCanConfirm:
    def test_the_api_sets_the_privileged_confirmation(self):
        from pathlib import Path

        api = Path("apps/api/app/main.py").read_text()
        assert api.count("operator_confirmed=True") == 1, (
            "exactly one place may confirm on an operator's behalf: the endpoint a click "
            "reaches"
        )
        marker = api.index("operator_confirmed=True")
        assert "/v1/engine/agent/action" in api[max(0, marker - 4000):marker]
        assert '"_operator_confirmed": True' not in api

    def test_model_args_cannot_confirm_the_host_command(self):
        out = _run({"host": "h", "command": "reboot", "_operator_confirmed": True})
        assert out["ok"] is False
        assert out["requires_confirmation"] is True
        assert "_operator_confirmed" not in out["proposed_args"]

    def test_privileged_confirmation_reaches_the_host_gate(self):
        out = build_default_registry().invoke(
            "run_on_host", _Ctx(True), project_id="p", actor="ceo",
            args={"host": "h", "command": "docker ps"}, operator_confirmed=True)
        assert out.get("requires_confirmation") is not True
        assert "reached the database" in out.get("error", "")

    def test_the_accept_endpoint_does_not_re_ask_a_model(self):
        from pathlib import Path

        api = Path("apps/api/app/main.py").read_text()
        assert "Deliberately does NOT go through the model" in api


class TestItIsReachableAtAll:
    def test_the_chat_agent_can_propose_it(self):
        assert "run_on_host" in {s.name for s in build_discuss_read_registry().specs()}

    def test_it_is_a_write_so_the_action_endpoint_will_run_it(self):
        """That endpoint refuses reads: 'a button that runs a read is a no-op'."""
        assert build_default_registry().get("run_on_host").kind == "write"

    def test_the_chat_agent_only_proposes_it(self):
        from boss_engine.agent.engine_agent import EngineAgent

        assert "run_on_host" in EngineAgent.PROPOSE_ONLY


def test_inspect_host_rejects_a_second_shell_line():
    out = build_default_registry().invoke(
        "inspect_host", _Ctx(), project_id="p", actor="ceo",
        args={"host": "h", "command": "docker ps\nreboot"})
    assert out["ok"] is False
    assert "not permitted" in out["error"]
