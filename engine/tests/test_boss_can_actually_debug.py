"""The operator's diagnosis, and it was right: "it is working based on the tools... it is not
debugging the issue like you do".

Three things were true at once. BOSS's chat agent ran on `gpt-5.2-codex` — not Claude at all,
because `llm_engine` defaults to "auto" and the adapter factory selects Claude only on the
exact string "claude_cli", so the subscription went to the coding pipeline while the interface
customers actually talk to ran on the fallback. It had a step budget in the teens. And it had
75 tools, every one of which answers a question somebody anticipated in a shape somebody chose.

The third is the one that mattered most. Debugging is: form a theory, TEST it, watch it fail,
form another. That needs a shell, a checkout, and the freedom to ask a question nobody wrapped
in a tool. No model in a menu can do it.

So three changes, and they are not equal:

  A  the chat agent gets its own brain, on Opus, chosen separately from everything else
  B  a real Claude Code session — shell, repo, read-only SQL, the fleet — for the hard ones
  C  a bigger step budget, and a write is not finished until a read has confirmed it

B is the one that changes what BOSS can do. A raises the ceiling on every turn. C stops it
declaring victory it has not checked.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.config import Settings
from boss_engine.services.claude_cli_adapter import (
    ClaudeCliAdapter,
    FallingBackAdapter,
    make_agent_chat_adapter,
)
from boss_engine.services.debug_session import (
    host_command_allowed,
    sql_is_read_only,
)
from boss_engine.tools import build_default_registry, build_discuss_read_registry


# --------------------------------------------------------------------------- A
class TestTheChatAgentHasItsOwnBrain:
    def test_production_can_put_it_on_claude_without_moving_the_worker(self):
        """`llm_engine` is global. Flipping THAT to claude_cli would take the worker with it,
        and the worker image has no `claude` binary — every worker-side completion would start
        raising "claude CLI not found"."""
        adapter = make_agent_chat_adapter(
            Settings(BOSS_CHAT_LLM_ENGINE="claude_cli", BOSS_CHAT_MODEL="opus"))
        assert isinstance(adapter, FallingBackAdapter)
        assert isinstance(adapter.primary, ClaudeCliAdapter)
        assert adapter.primary.model_override == "opus"

    def test_the_model_is_passed_to_the_cli(self):
        body = inspect.getsource(ClaudeCliAdapter._invoke)
        assert "self.model_override or getattr(self.settings" in body
        assert '"--model", model' in body

    def test_unset_inherits_rather_than_forcing_a_binary_nobody_has(self):
        """Local development and CI have no Claude CLI. A default that required one would break
        every developer's console the moment this landed."""
        import boss_engine.services.claude_cli_adapter as mod

        body = inspect.getsource(mod.make_agent_chat_adapter)
        assert "if not engine:" in body
        assert "return make_chat_adapter(settings)" in body

    def test_the_router_chooses_it_explicitly(self):
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert "adapter=make_agent_chat_adapter(self.settings)" in body

    def test_the_agent_accepts_the_choice(self):
        body = inspect.getsource(
            __import__("boss_engine.agent.engine_agent", fromlist=["x"]).EngineAgent.__init__)
        assert "adapter: Any = None" in body
        assert "self._adapter = adapter" in body


class TestRaisingTheBrainCannotTakeChatDown:
    """A better model that sometimes cannot be reached is worse than a worse model that can."""

    class _Boom:
        def __init__(self, error):
            self.error = error

        def create_chat_completion(self, messages, tools=None, **kw):
            raise self.error

    class _Fine:
        def create_chat_completion(self, messages, tools=None, **kw):
            return ({"content": "answered"}, {})

    @pytest.mark.parametrize("error", [
        RuntimeError("claude CLI not found. Install Claude Code and ensure `claude` is on PATH."),
        RuntimeError("claude CLI timed out after 240s"),
        RuntimeError("claude CLI exited 1: not logged in"),
    ])
    def test_an_unreachable_backend_falls_back(self, error):
        adapter = FallingBackAdapter(self._Boom(error), self._Fine())
        assert adapter.create_chat_completion([])[0]["content"] == "answered"
        assert adapter.last_backend == "_Fine"

    def test_a_bad_ANSWER_does_not_fall_back(self):
        """Otherwise one bad turn silently downgrades the brain and nobody ever sees it happen.
        A malformed completion is the caller's retry problem — it already knows how to press."""
        adapter = FallingBackAdapter(self._Boom(ValueError("returned non-JSON: {oops")),
                                     self._Fine())
        with pytest.raises(ValueError):
            adapter.create_chat_completion([])

    def test_the_primary_answers_when_it_can(self):
        adapter = FallingBackAdapter(self._Fine(), self._Boom(RuntimeError("unused")))
        assert adapter.create_chat_completion([])[0]["content"] == "answered"
        assert adapter.last_backend == "_Fine"


# --------------------------------------------------------------------------- B
class TestARealDebuggingSession:
    def test_the_tool_exists_and_boss_can_start_one_itself(self):
        spec = build_discuss_read_registry().get("investigate")
        assert spec.kind == "write" and spec.self_executable

    def test_and_it_can_read_the_result(self):
        assert "get_investigation" in build_discuss_read_registry().names()

    def test_it_runs_the_same_claude_code_session_the_builder_uses(self):
        """Not a new agent implementation. The coding pipeline already drives `claude -p` with
        Bash, Read, Grep and Glob in a sandbox; this points that at a diagnosis."""
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert "ClaudeCodeAgent(settings)._invoke(" in body
        assert "Read,Grep,Glob,Bash" in body
        assert "EphemeralSandbox" in body

    def test_it_gets_a_big_turn_budget(self):
        """The failure being fixed is an agent that stopped after one hypothesis."""
        assert Settings().investigation_max_turns >= 40

    def test_it_runs_on_opus_by_default(self):
        assert Settings().investigation_model == "opus"

    def test_it_starts_from_what_boss_already_knows(self):
        from boss_engine.services.debug_session import _briefing

        body = inspect.getsource(_briefing)
        for table in ("deployment_hosts", "deployment_targets", "code_task_runs",
                      "workflow_step_runs", "deployment_runs"):
            assert table in body, table

    def test_a_missing_briefing_row_does_not_fail_the_investigation(self):
        from boss_engine.services.debug_session import _briefing

        class Boom:
            def execute(self, *a, **kw):
                raise RuntimeError("no such table")

        out = _briefing(Boom(), project_id="p", question="why", run_id="crun_1")
        assert "could not read" in out
        assert "why" in out

    def test_the_result_lands_where_the_question_was_asked(self):
        """An investigation nobody sees is a report filed in a drawer."""
        from boss_engine.services.debug_session import _post_to_thread

        body = inspect.getsource(_post_to_thread)
        assert "INSERT INTO agent_messages" in body
        assert "'assistant'" in body

    def test_the_thread_id_is_supplied_rather_than_invented(self):
        """Asking the model for an `athr_...` produces a fabricated one — the exact failure
        `_placeholder_argument` exists for."""
        from boss_engine.agent import engine_agent
        from boss_engine.services import intent_router

        assert 'args={**(self.ambient or {}), **args},' in inspect.getsource(engine_agent)
        assert 'ambient = {"_thread_id": thread_id}' in inspect.getsource(intent_router)

    def test_only_one_runs_at_a_time_per_project(self):
        """An agent that can open investigations would otherwise open one per turn while it
        waits for the first — a loop with a bill attached."""
        from boss_engine.services.debug_session import open_investigation

        body = inspect.getsource(open_investigation)
        assert "status IN ('queued', 'running')" in body
        assert "already_running" in body

    def test_it_is_queued_rather_than_awaited(self):
        from boss_engine.services.debug_session import open_investigation

        body = inspect.getsource(open_investigation)
        assert 'enqueue_job(connection, project_id, "investigation.run"' in body

    def test_the_worker_will_actually_lease_that_job(self):
        """Declared nowhere, the job sits queued forever and the investigation never happens."""
        import pathlib

        runner = pathlib.Path("apps/worker/boss_worker/runner.py").read_text()
        assert '"investigation.run",' in runner
        assert 'if job.kind == "investigation.run":' in runner

    def test_it_executes_where_the_cli_lives(self):
        """The API image installs @anthropic-ai/claude-code; the worker's does not. Code tasks
        already hop the same way, and for the same reason."""
        import pathlib

        assert "claude-code" in pathlib.Path("infra/containers/api.Dockerfile").read_text()
        client = pathlib.Path("apps/worker/boss_worker/control_plane.py").read_text()
        assert "/v1/investigations/" in client
        main = pathlib.Path("apps/api/app/main.py").read_text()
        assert '@app.post("/v1/investigations/{investigation_id}/run")' in main


class TestFreedomToLookIsNotFreedomToAct:
    """The line that lets this be turned loose at all."""

    def test_reading_logs_is_allowed(self):
        assert host_command_allowed("docker logs app", allow_write=False)[0]

    def test_and_piping_them_into_a_filter(self):
        """`docker logs app | tail -100` is the single most useful command in a deployment
        investigation. An allow-list without pipes cannot express it, which is why the previous
        read-only host tool was never enough to debug anything."""
        assert host_command_allowed("docker logs app | grep -i error | tail -50",
                                    allow_write=False)[0]

    @pytest.mark.parametrize("command", [
        "docker restart app",
        "docker rm -f app",
        "systemctl stop nginx",
        "echo x > /etc/hosts",
        "cat /etc/passwd; rm /tmp/x",
        "docker logs app | tee /tmp/out",
        "cat $(ls /root)",
    ])
    def test_changing_the_machine_is_refused_when_reading(self, command):
        allowed, why = host_command_allowed(command, allow_write=False)
        assert allowed is False, command
        assert why

    def test_a_fix_investigation_may_change_the_machine(self):
        assert host_command_allowed("docker restart app", allow_write=True)[0]

    @pytest.mark.parametrize("command", [
        "rm -rf /var/lib/boss",
        "docker system prune -af",
        "docker volume rm boss_data",
        "mkfs.ext4 /dev/sda1",
        "shutdown -h now",
    ])
    def test_but_never_the_irreversible_ones(self, command):
        """`fix=True` opens the shell. It does not open these — no diagnosis needs them."""
        allowed, why = host_command_allowed(command, allow_write=True)
        assert allowed is False, command
        assert "every mode" in why

    def test_the_writable_shell_needs_an_operator(self):
        """A model cannot license its own writable shell on a production host. On 2026-09-02 an
        agent holding production credentials deleted 204 tables."""
        class Ctx:
            def begin(self):
                raise AssertionError("OPENED — the confirmation gate did not run")

        out = build_default_registry().invoke(
            "investigate", Ctx(), project_id="p", actor="ceo",
            args={"question": "fix the deploy", "fix": True})
        assert out["ok"] is False
        assert out["requires_confirmation"] is True
        assert out["proposed_args"]["fix"] is True

    def test_a_read_only_investigation_needs_nobody(self):
        """Requiring a click to LOOK would leave the same dead end this exists to remove."""
        class Ctx:
            def begin(self):
                raise AssertionError("REACHED THE DATABASE")

        out = build_default_registry().invoke(
            "investigate", Ctx(), project_id="p", actor="ceo",
            args={"question": "why is the deploy failing"})
        assert "REACHED THE DATABASE" in str(out.get("error") or "")

    @pytest.mark.parametrize("statement,ok", [
        ("SELECT * FROM deployment_targets", True),
        ("WITH x AS (SELECT 1) SELECT * FROM x", True),
        ("EXPLAIN SELECT 1", True),
        ("DELETE FROM deployment_targets", False),
        ("UPDATE jobs SET status = 'done'", False),
        ("DROP TABLE events", False),
        ("SELECT 1; DELETE FROM jobs", False),
        ("INSERT INTO deployment_targets (id) VALUES ('x')", False),
    ])
    def test_the_database_is_read_only(self, statement, ok):
        assert sql_is_read_only(statement)[0] is ok

    def test_the_credentials_are_not_inherited_from_the_environment(self):
        """`agent_environment` strips DATABASE_URL from the agent's subprocess, because an agent
        that inherited production credentials deleted 204 tables. Making this feature work by
        weakening that filter would be trading the incident for the convenience."""
        from boss_engine.services.agent_env import NEVER, is_allowed

        assert "DATABASE_URL" in NEVER
        assert is_allowed("DATABASE_URL") is False

    def test_and_the_limit_of_that_is_stated_rather_than_implied(self):
        """An agent with a shell can read the script that holds the connection string. The real
        containment is a Postgres role that cannot write; until then this is a speed bump on
        the accidental route, and the code says so instead of pretending otherwise."""
        from boss_engine.services.debug_session import _install_toolbelt

        body = inspect.getsource(_install_toolbelt)
        assert "HONEST LIMIT" in body
        assert "BOSS_INVESTIGATION_DB_URL" in body
        assert "0o700" in body

    def test_a_read_only_role_is_used_the_moment_one_exists(self):
        from boss_engine.services.debug_session import run_investigation

        assert 'getattr(settings, "investigation_db_url", "")' \
            in inspect.getsource(run_investigation)


class TestItMustEndWithEvidence:
    def test_the_verdict_shape_demands_what_it_RAN(self):
        from boss_engine.services import debug_session

        assert '"evidence": ["each thing you RAN that proves it' in debug_session._PROMPT
        assert "must be things you ran in this session" in debug_session._PROMPT

    def test_a_second_theory_is_asked_for_explicitly(self):
        from boss_engine.services import debug_session

        assert "try a different one" in debug_session._PROMPT
        assert "Repeating a diagnosis is the" in debug_session._PROMPT

    def test_the_machine_outranks_the_database(self):
        from boss_engine.services import debug_session

        assert "The database records what BOSS INTENDED" in debug_session._PROMPT

    def test_the_verdict_is_parsed_out_of_the_final_message(self):
        from boss_engine.services.debug_session import _verdict_from

        parsed = _verdict_from(
            'Some prose.\n```json\n{"cause": "port 8000 taken", "applied": false}\n```')
        assert parsed["cause"] == "port 8000 taken"

    def test_prose_without_a_verdict_is_not_lost(self):
        from boss_engine.services.debug_session import _verdict_from

        assert _verdict_from("I could not work it out.") == {}

    def test_a_malformed_fence_does_not_raise(self):
        from boss_engine.services.debug_session import _verdict_from

        assert _verdict_from("```json\n{not json}\n```") == {}


# --------------------------------------------------------------------------- C
class TestAWriteIsNotFinishedUntilItIsChecked:
    from boss_engine.services.intent_router import acted_without_checking as _check

    _check = staticmethod(_check)

    def test_acting_and_stopping_is_a_fault(self):
        assert self._check(
            [{"kind": "redeploy"}],
            [{"tool": "get_code_task"}, {"tool": "redeploy"}]) is True

    def test_reading_afterwards_settles_it(self):
        assert self._check(
            [{"kind": "redeploy"}],
            [{"tool": "redeploy"}, {"tool": "describe_deployment_target"}]) is False

    def test_a_second_write_is_not_a_check(self):
        assert self._check(
            [{"kind": "retarget_app"}, {"kind": "redeploy"}],
            [{"tool": "retarget_app"}, {"tool": "redeploy"}]) is True

    def test_a_turn_that_changed_nothing_is_not_judged(self):
        assert self._check([], [{"tool": "query_db"}]) is False

    def test_it_is_wired_into_the_resolution_loop(self):
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert "_unverified = acted_without_checking(actions, tool_calls)" in body
        assert "You changed something and did not check the result" in body

    def test_the_step_budget_is_configurable_and_not_small(self):
        assert Settings().chat_max_steps >= 12
        assert Settings().chat_continuation_steps >= 8


class TestTheOfferSurvivesTheGate:
    """Both bugs measured live, on the first real call to the new tool.

        TOOLS     [('investigate', False), ...]
        PROPOSED  [{"tool": "investigate", "args": {}, "label": "investigate", "why": ""}]

    Two failures in one line. The message was "Use the investigate tool now: find out why
    Change Calendar cannot be deployed" — an instruction — and the look-only gate read it as a
    question, because the new capability's NAME is a look-word. So the write was withheld and
    the operator was handed a button for the thing they had just told BOSS to do.

    And that button had no arguments. A withheld write comes back as {"proposed": {tool, args}}
    while `propose_action` returns {tool, args}; only the second shape was read. The offered
    `investigate` would have run with no question in it.
    """

    def test_naming_a_tool_and_saying_run_it_is_an_instruction(self):
        from boss_engine.services.intent_router import asked_only_to_look

        assert asked_only_to_look(
            "Use the investigate tool now: find out why Change Calendar cannot be deployed "
            "to staging, and what exactly needs to change.") is False

    @pytest.mark.parametrize("message", [
        "run investigate on this",
        "open an investigation",
        "call diagnose_run on crun_123",
        "kick off an investigation",
        "use the investigate tool",
    ])
    def test_the_imperatives(self, message):
        from boss_engine.services.intent_router import asked_only_to_look

        assert asked_only_to_look(message) is False, message

    @pytest.mark.parametrize("message", [
        "should I run investigate?",
        "can you use the investigate tool?",
        "would it be worth running an investigation?",
        "why is it stuck?",
        "what changed today?",
    ])
    def test_asking_about_running_one_is_still_a_question(self, message):
        """The gate exists because a question got a build cancelled. Widening it until every
        mention of a tool licenses a write would give that back."""
        from boss_engine.services.intent_router import asked_only_to_look

        assert asked_only_to_look(message) is True, message

    def test_a_withheld_write_keeps_its_arguments(self):
        from boss_engine.services.intent_router import _offered_args

        withheld = {"ok": False, "status": "confirmation_required",
                    "proposed": {"tool": "investigate",
                                 "args": {"question": "why can it not deploy",
                                          "run_id": "crun_e917"}}}
        assert _offered_args(withheld, {}) == {"question": "why can it not deploy",
                                               "run_id": "crun_e917"}

    def test_a_propose_action_offer_still_works(self):
        from boss_engine.services.intent_router import _offered_args

        assert _offered_args({"tool": "redeploy", "args": {"run_id": "crun_1"}}, {}) \
            == {"run_id": "crun_1"}

    def test_the_confirmation_shape_is_read_too(self):
        from boss_engine.services.intent_router import _offered_args

        assert _offered_args({"proposed_args": {"question": "q", "fix": True}}, {}) \
            == {"question": "q", "fix": True}

    def test_it_falls_back_to_the_call_the_model_made(self):
        """A withheld write IS the call the model tried to make; losing it would produce a
        button that does something other than what was proposed."""
        from boss_engine.services.intent_router import _offered_args

        assert _offered_args({"ok": False}, {"args": {"question": "from the call"}}) \
            == {"question": "from the call"}

    def test_internal_markers_never_reach_the_button(self):
        from boss_engine.services.intent_router import _offered_args

        assert _offered_args({"proposed_args": {"fix": True, "_operator_confirmed": True,
                                                "_thread_id": "athr_1"}}, {}) == {"fix": True}

    def test_the_fixing_investigation_offers_itself_with_words(self):
        class Ctx:
            def begin(self):
                raise AssertionError("should not have opened one")

        out = build_default_registry().invoke(
            "investigate", Ctx(), project_id="p", actor="ceo",
            args={"question": "fix it", "fix": True})
        assert out["label"] and out["why"]
        assert "writable shell" in out["why"]


class TestTheImperativeIsAnchored:
    """The first version of `told_to_run_a_tool` matched "which RUN failed and why" — a
    question about a run, read as an order to run something. That is the same class of mistake
    as the gate it was added to, made while fixing it, and the existing question corpus caught
    it. Anchoring the verb to a clause boundary is what makes "run investigate" an imperative
    and "which run failed" a noun."""

    @pytest.mark.parametrize("message", [
        "which run failed and why",
        "why did the run fail",
        "what happened to the build?",
        "the run started at 9am, when did it finish?",
        "is the open target still active?",
    ])
    def test_a_verb_in_the_middle_of_a_question_is_not_an_order(self, message):
        from boss_engine.services.intent_router import told_to_run_a_tool

        assert told_to_run_a_tool(message) is False, message

    @pytest.mark.parametrize("message", [
        "run investigate on this",
        "please run investigate",
        "look at the target, then run investigate",
        "Use the investigate tool now",
    ])
    def test_but_at_the_start_of_one_it_is(self, message):
        from boss_engine.services.intent_router import told_to_run_a_tool

        assert told_to_run_a_tool(message) is True, message
