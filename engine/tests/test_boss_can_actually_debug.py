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
        adapter = make_agent_chat_adapter(Settings(
            BOSS_CHAT_LLM_ENGINE="claude_cli", BOSS_CHAT_MODEL="opus",
            OPENAI_API_KEY="fallback-is-configured"))
        assert isinstance(adapter, FallingBackAdapter)
        assert isinstance(adapter.primary, ClaudeCliAdapter)
        assert adapter.primary.model_override == "opus"

    def test_the_model_is_passed_to_the_cli(self):
        # One command builder for the one-shot and the streaming call (`_command`).
        body = inspect.getsource(ClaudeCliAdapter._command)
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
        # The project's routed model (services/model_routing.py), chosen per turn.
        assert "model_routing.project_chat_adapter(" in body
        assert "adapter=(self._answer_adapter" in body

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

        def create_text_response(self, prompt, system_prompt=None, model=None):
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

    @pytest.mark.parametrize("method,args", [
        ("create_chat_completion", ([],)),
        ("create_text_response", ("hello",)),
    ])
    def test_both_failures_report_the_primary_cause(self, method, args):
        adapter = FallingBackAdapter(
            self._Boom(RuntimeError("claude CLI not found")),
            self._Boom(RuntimeError("fallback also failed")))
        with pytest.raises(RuntimeError, match="claude CLI not found.*fallback also failed"):
            getattr(adapter, method)(*args)


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

        assert 'args={**(self.ambient or {}), **args,' in inspect.getsource(engine_agent)
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

    def test_no_credential_reaches_the_sandbox_at_all(self):
        """The first version baked the connection string into the working directory, and said
        so honestly. It was still the wrong design — and worse than it looked, because the
        commands ran in the sandbox's own stripped environment, so the first SSH call
        auto-provisioned a REPLACEMENT KMS master key. The investigation found that itself and
        called it "a silent wrong-key fallback if that var were ever unset".

        The commands are HTTP clients now. The database url, the KMS key and the SSH keys never
        leave the API process."""
        from boss_engine.services import debug_session

        for script in (debug_session._DB_SCRIPT, debug_session._HOST_SCRIPT,
                       debug_session._DO_SCRIPT):
            assert "DATABASE_URL" not in script
            assert "create_engine" not in script
            assert "resolve_target_secret" not in script
            assert "urllib.request" in script

    def test_the_only_thing_it_carries_is_a_token_for_itself(self):
        from boss_engine.services.debug_session import _install_toolbelt

        body = inspect.getsource(_install_toolbelt)
        assert "__BOSS_TOKEN__" in body
        assert "0o700" in body

    def test_the_token_authorises_one_investigation_while_it_runs(self):
        from boss_engine.services.debug_session import authorise

        body = inspect.getsource(authorise)
        assert "WHERE id = :i AND token = :t" in body
        assert "status\"] not in (\"queued\", \"running\")" in body

    def test_and_is_cleared_when_it_ends(self):
        """A sandbox is torn down, but a token recovered from one must open nothing."""
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert body.count("token = NULL") == 2      # the completed path and the failed one


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


class TestTheHostDescriptionQueryIsValid:
    """`describe_deployment_host` selected `name`, `app_port` and `domain` from
    `deployment_targets`. None of those columns exist — the table has `app_name`, `domains` and
    no port column — so the tool raised `column "name" does not exist` on every call, and the
    one tool for "what is running on this host" had never worked.

    Found by BOSS, mid-answer, once it was on a model that noticed."""

    def _columns(self):
        import pathlib
        import re

        schema = pathlib.Path("db/schema.sql").read_text()
        block = schema[schema.index("CREATE TABLE IF NOT EXISTS deployment_targets"):]
        block = block[:block.index("\n);")]
        columns = {m.group(1) for m in re.finditer(r"^\s{2}([a-z_]+)\s", block, re.M)}
        for m in re.finditer(r"ALTER TABLE deployment_targets[\s\S]{0,200}?"
                             r"ADD COLUMN IF NOT EXISTS ([a-z_]+)", schema):
            columns.add(m.group(1))
        return columns

    def test_every_selected_column_exists(self):
        import inspect

        from boss_engine.tools import builtin

        body = inspect.getsource(builtin)
        i = body.index("FROM deployment_targets WHERE project_id=:p AND host_id=:h")
        select = body[body.rindex("SELECT", 0, i):i]
        selected = {c.strip().strip('"') for c in
                    select.replace("SELECT", "").replace('"', " ").split(",") if c.strip()}
        missing = selected - self._columns()
        assert not missing, f"columns that do not exist: {sorted(missing)}"

    def test_the_names_it_used_to_ask_for_are_gone(self):
        import inspect

        from boss_engine.tools import builtin

        body = inspect.getsource(builtin)
        i = body.index("FROM deployment_targets WHERE project_id=:p AND host_id=:h")
        select = body[body.rindex("SELECT", 0, i):i]
        assert "app_port" not in select and " domain," not in select


class TestWritingIsAnOperationBeforeItIsAStatement:
    """The operator's call: "we need to have write access to the postgres DB url to kick off the
    retries, performing additional operations etc... if we create any operations through APIs it
    is working right.. the BOSS agent also should take same path".

    Exactly right, and it settles the design question the first version dodged. An investigation
    that can diagnose a stuck retry and not clear it is half a capability. But the path is
    BOSS's own operations, not a shell with a connection string: `redeploy` resolves the
    target's key, reserves budget, records a deployment run and health-checks the result, and a
    hand-written UPDATE gets some of that right and leaves no trace of the rest — which is what
    the INSERT against deployment_targets already demonstrated.
    """

    @pytest.mark.parametrize("statement", [
        "SELECT * FROM jobs",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ])
    def test_reading_needs_no_permission(self, statement):
        from boss_engine.services.debug_session import sql_is_allowed

        assert sql_is_allowed(statement, allow_write=False)[0]

    @pytest.mark.parametrize("statement", [
        "UPDATE workflow_step_runs SET status = 'queued' WHERE id = 'wsr_1'",
        "INSERT INTO deployment_targets (id) VALUES ('deptgt_1')",
        "DELETE FROM jobs WHERE id = 'job_1'",
    ])
    def test_a_fixing_investigation_may_write_rows(self, statement):
        from boss_engine.services.debug_session import sql_is_allowed

        assert sql_is_allowed(statement, allow_write=True)[0], statement

    @pytest.mark.parametrize("statement", [
        "UPDATE jobs SET status = 'done'",
        "DELETE FROM workflow_step_runs",
    ])
    def test_but_never_unqualified(self, statement):
        """One wrong statement becoming an outage rather than a fix."""
        from boss_engine.services.debug_session import sql_is_allowed

        allowed, why = sql_is_allowed(statement, allow_write=True)
        assert allowed is False
        assert "every row" in why

    @pytest.mark.parametrize("statement", [
        "WITH x AS (SELECT 1) UPDATE jobs SET status = 'done'",
        "WITH x AS (SELECT 1) DELETE FROM workflow_step_runs",
        "WITH x AS (SELECT 1) INSERT INTO jobs (id) VALUES ('x')",
        "EXPLAIN ANALYZE UPDATE jobs SET status = 'done' WHERE id = 'x'",
    ])
    def test_a_read_only_investigation_cannot_write_through_a_prefix(self, statement):
        from boss_engine.services.debug_session import sql_is_allowed

        allowed, why = sql_is_allowed(statement, allow_write=False)
        assert allowed is False, (statement, why)

    @pytest.mark.parametrize("statement", [
        "WITH x AS (SELECT id FROM jobs WHERE status = 'dead') "
        "UPDATE jobs SET status = 'queued'",
        "UPDATE jobs SET status = (SELECT status FROM jobs WHERE id = 'other')",
        "UPDATE jobs SET last_error = 'where is the port'",
        "UPDATE jobs SET last_error = $$where is the port$$",
        "UPDATE jobs SET last_error = $message$where is the port$message$",
        "DELETE FROM jobs /* WHERE id = 'x' */",
    ])
    def test_an_inner_or_quoted_where_cannot_qualify_an_outer_write(self, statement):
        from boss_engine.services.debug_session import sql_is_allowed

        allowed, _why = sql_is_allowed(statement, allow_write=True)
        assert allowed is False, statement

    def test_a_qualified_cte_write_is_allowed_only_when_fixing(self):
        from boss_engine.services.debug_session import sql_is_allowed

        sql = ("WITH x AS (SELECT id FROM jobs WHERE status = 'dead') "
               "UPDATE jobs SET status = 'queued' WHERE id IN (SELECT id FROM x)")
        assert sql_is_allowed(sql, allow_write=False)[0] is False
        assert sql_is_allowed(sql, allow_write=True)[0] is True

    def test_a_data_changing_cte_is_refused_even_if_outer_query_reads(self):
        from boss_engine.services.debug_session import sql_is_allowed

        sql = "WITH x AS (DELETE FROM jobs RETURNING id) SELECT id FROM x"
        assert sql_is_allowed(sql, allow_write=False)[0] is False
        assert sql_is_allowed(sql, allow_write=True)[0] is False

    @pytest.mark.parametrize("statement", [
        "DROP TABLE events",
        "TRUNCATE jobs",
        "ALTER TABLE jobs ADD COLUMN x TEXT",
        "GRANT ALL ON jobs TO someone",
    ])
    def test_and_never_the_shape_of_the_database(self, statement):
        from boss_engine.services.debug_session import sql_is_allowed

        allowed, why = sql_is_allowed(statement, allow_write=True)
        assert allowed is False
        assert "migration" in why

    def test_a_read_only_investigation_is_told_what_to_do_instead(self):
        from boss_engine.services.debug_session import sql_is_allowed

        _allowed, why = sql_is_allowed("UPDATE jobs SET status='x' WHERE id='1'",
                                       allow_write=False)
        assert "put it in `fix`" in why

    def test_operations_run_through_the_registry_the_console_uses(self):
        from boss_engine.services.debug_session import run_operation

        body = inspect.getsource(run_operation)
        assert "build_default_registry()" in body
        assert "if not spec.self_executable" in body
        assert "operator_confirmed=True" not in body

    def test_a_read_only_investigation_may_not_run_them(self):
        from boss_engine.services.debug_session import run_operation

        out = run_operation(None, {"allow_fix": False, "project_id": "p", "actor": "ceo"},
                            "redeploy", {"run_id": "crun_1"})
        assert out["ok"] is False
        assert "read-only" in out["error"]
        assert "redeploy" in out["available_in_a_fixing_investigation"]

    @pytest.mark.parametrize("tool", ["cancel_job", "cancel_code_task", "close_orphaned_runs",
                                      "waive_gates", "run_on_host", "write_db", "decide",
                                      "delete_workflow"])
    def test_throwing_work_away_is_never_an_investigation(self, tool):
        """The operator's rule, in their words: cancelling "should be prevented at all cost".
        An investigation exists to make something work; nothing in that job needs to kill a
        running build, waive a gate, or run free-form SQL that the guarded path already covers.
        """
        from boss_engine.services.debug_session import run_operation

        out = run_operation(None, {"allow_fix": True, "project_id": "p", "actor": "ceo"},
                            tool, {})
        assert out["ok"] is False
        assert "not something an investigation may do" in out["error"]

    def test_the_fixes_it_needs_are_all_there(self):
        from boss_engine.services.debug_session import _NOT_FOR_INVESTIGATIONS
        from boss_engine.tools import build_default_registry

        writes = {s.name for s in build_default_registry().specs(kind="write")}
        available = writes - _NOT_FOR_INVESTIGATIONS
        for needed in ("redeploy", "retry_code_task", "create_deployment_target",
                       "retarget_app", "deploy_app", "extend_code_task", "steer_code_task"):
            assert needed in available, needed

    def test_a_read_only_investigation_is_still_the_default(self):
        from boss_engine.services.debug_session import open_investigation

        body = inspect.getsource(open_investigation)
        assert "allow_fix: bool = False" in body

    def test_the_prompt_uses_scoped_operations_instead_of_raw_sql(self):
        from boss_engine.services import debug_session

        assert "PREFER boss-do FOR CHANGES" in debug_session._PROMPT
        assert "leaves no trace of the rest" in debug_session._PROMPT
        assert "Raw SQL is held" in debug_session._PROMPT

    def test_and_to_report_the_operation_that_was_missing(self):
        """A missing scoped operation becomes a named product gap."""
        from boss_engine.services import debug_session

        assert "which operation is missing" in debug_session._PROMPT


class TestTheOperatorCanSeeItThinking:
    """"56s for a deep diagnostic against 2.4s before ==> this could be a concern, instead can we
    display all the debugging info that it's doing during the investigation... just to show that
    it's doing something rather giving the feeling to user that it's stuck."

    The chat turn already streamed its steps. The investigation did not, and it is the long one.
    An operator who believes a system is stuck interrupts it — which is how the incident that
    started all of this began."""

    def test_the_runner_can_report_each_turn_as_it_happens(self):
        from boss_engine.services.code_agent import ClaudeCodeAgent

        body = inspect.getsource(ClaudeCodeAgent._invoke)
        assert "on_turn: Optional[Callable[[dict], None]] = None" in body

    def test_reporting_can_never_break_the_work(self):
        """A progress indicator that can fail the thing it reports on is worse than none."""
        from boss_engine.services import code_agent

        body = inspect.getsource(code_agent)
        i = body.index("if on_turn:")
        assert "except Exception" in body[i:i + 260]

    def test_the_investigation_reports_into_the_thread_that_asked(self):
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert "_progress_reporter(project_id" in body
        assert "on_turn=_tell_the_operator" in body

    @pytest.mark.parametrize("turn,expected", [
        ({"direction": "tool_call", "tool_name": "Bash",
          "content": {"command": './.boss-bin/boss-db "SELECT * FROM deployment_targets"'}},
         'querying the database: "SELECT * FROM deployment_targets"'),
        ({"direction": "tool_call", "tool_name": "Bash",
          "content": {"command": "./.boss-bin/boss-host hetzner-ci-01 docker ps"}},
         "on the host: hetzner-ci-01 docker ps"),
        ({"direction": "tool_call", "tool_name": "Read",
          "content": {"file_path": "/x/src/server.ts"}}, "reading /x/src/server.ts"),
        ({"direction": "tool_call", "tool_name": "Grep", "content": {"pattern": "listen("}},
         "searching listen("),
    ])
    def test_it_says_what_it_is_doing_in_words(self, turn, expected):
        from boss_engine.services.debug_session import _what_it_is_doing

        assert _what_it_is_doing(turn) == expected

    def test_its_own_reasoning_is_the_most_informative_thing_on_offer(self):
        """"the container binds 8000 but the target says 3000" tells the operator more than any
        tool name could."""
        from boss_engine.services.debug_session import _what_it_is_doing

        assert _what_it_is_doing({
            "direction": "completion",
            "content": "The container binds 8000 but the target says 3000."}) \
            == "The container binds 8000 but the target says 3000."

    def test_noise_is_not_reported(self):
        from boss_engine.services.debug_session import _what_it_is_doing

        assert _what_it_is_doing({"direction": "tool_result", "content": "x"}) == ""
        assert _what_it_is_doing({"direction": "completion", "content": "   "}) == ""

    def test_a_line_is_bounded(self):
        from boss_engine.services.debug_session import _what_it_is_doing

        line = _what_it_is_doing({"direction": "completion", "content": "x" * 4000})
        assert 0 < len(line) <= 180

    def test_the_console_is_told_when_one_is_running(self):
        import pathlib

        main = pathlib.Path("apps/api/app/main.py").read_text()
        assert "FROM investigations \"\n                 \"WHERE thread_id = :t AND project_id = :p " in main
        assert "AND status IN ('queued', 'running')" in main
        assert '"investigation": ({"id": live["id"]' in main


class TestWhoDecidedNotWhichFlagWasSet:
    """"we need to have write access... to kick off the retries, performing additional
    operations etc.. including the investigation and applying the fixes."

    A fixing investigation required `_operator_confirmed` — the marker only a button click sets
    — so it needed a click even when the operator had just said "do anything to fix it". That is
    the same fault as the look-only gate refusing an explicit instruction, one layer up, and it
    made "applying the fixes" unreachable by asking for it.

    The authority is established before the tool runs: a write tool is only reached when the
    turn granted write permission, and the turn grants it only on an INSTRUCTION."""

    class _Ctx:
        def begin(self):
            raise AssertionError("OPENED")

    def _investigate(self, args, *, operator_confirmed=False):
        return build_default_registry().invoke(
            "investigate", self._Ctx(), project_id="p", actor="ceo", args=args,
            operator_confirmed=operator_confirmed)

    def test_an_instruction_opens_a_fixing_investigation(self):
        out = self._investigate({"question": "fix the deploy", "fix": True,
                                 "_writes_allowed": True})
        assert "OPENED" in str(out.get("error") or ""), out

    def test_a_click_still_does_too(self):
        out = self._investigate({"question": "fix the deploy", "fix": True},
                                operator_confirmed=True)
        assert "OPENED" in str(out.get("error") or ""), out

    def test_a_question_gets_an_offer(self):
        """Writes are withheld on a question, so the turn never sets the marker — and the
        operator is shown the button instead of having a shell opened on their behalf."""
        out = self._investigate({"question": "fix the deploy", "fix": True,
                                 "_writes_allowed": False})
        assert out["ok"] is False and out["requires_confirmation"] is True
        assert out["proposed_args"]["fix"] is True

    def test_a_read_only_investigation_never_needed_either(self):
        out = self._investigate({"question": "why is it broken"})
        assert "OPENED" in str(out.get("error") or ""), out

    def test_the_loop_records_who_authorised_the_turn(self):
        from boss_engine.agent import engine_agent

        body = inspect.getsource(engine_agent)
        assert '"_writes_allowed": bool(allow_writes)' in body

    def test_the_marker_never_reaches_a_button(self):
        from boss_engine.services.intent_router import _offered_args

        assert _offered_args({"proposed_args": {"fix": True, "_writes_allowed": True}}, {}) \
            == {"fix": True}

    def test_what_a_fix_investigation_still_cannot_do_did_not_move(self):
        """Widening WHO may open one must not widen WHAT one may do."""
        from boss_engine.services.debug_session import (
            _NOT_FOR_INVESTIGATIONS, host_command_allowed, sql_is_allowed,
        )

        assert "cancel_job" in _NOT_FOR_INVESTIGATIONS
        assert host_command_allowed("rm -rf /data", allow_write=True)[0] is False
        assert sql_is_allowed("DROP TABLE events", allow_write=True)[0] is False


class TestTheToolbeltMustActuallyWork:
    """Measured on the first fixing investigation. Sixty turns, and the progress trail shows
    what they went on:

        running a BOSS operation: --list 2>&1 | head -60
        reading .boss-bin/boss-do
        editing .boss-bin/boss-do
        running curl -s http://127.0.0.1:8000/openapi.json
        running python3 - <<EOF  import json; d=json.load(open('/tmp/openapi.json')) ...

    `boss-do` came back 401. The three callback paths sit under /v1/, and BOSS_REQUIRE_API_AUTH
    is on in production, so the global middleware rejected the sandbox's own token before the
    endpoint ever saw it. The session then spent its budget reverse-engineering my toolbelt from
    the OpenAPI schema instead of debugging the problem it was opened for — and stopped at the
    ceiling with no verdict.

    Two defects, and the second is the worse one: the run was stored `completed`."""

    def test_the_sandbox_token_is_not_shadowed_by_the_api_key(self):
        import pathlib

        main = pathlib.Path("apps/api/app/main.py").read_text()
        assert "is_investigation_callback" in main
        assert '_parts[4] in {"db", "host", "do"}' in main
        assert "or is_investigation_callback" in main

    def test_the_exemption_is_narrow(self):
        """Only the three callback paths. `/v1/investigations` itself — the console's list —
        stays behind the API key, because that is a human-facing read."""
        import pathlib

        main = pathlib.Path("apps/api/app/main.py").read_text()
        i = main.index("is_investigation_callback = (")
        assert '_parts[2] == "investigations"' in main[i:i + 300]
        assert "len(_parts) >= 5" in main[i:i + 300]

    def test_running_out_of_turns_is_not_completed(self):
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert 'status = "completed" if _finished else ("incomplete" if _ran_out' in body
        assert "RAN OUT OF TURNS IS NOT FINISHED" in body

    def test_and_it_says_so_in_the_error_column(self):
        """The operator had to query the summary column to discover the run had not finished."""
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert "stopped at the {turns}-turn ceiling without reaching a verdict" in body

    def test_a_verdict_is_what_makes_it_finished(self):
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert '_finished = bool(verdict.get("cause"))' in body

    def test_the_event_names_the_real_outcome(self):
        from boss_engine.services.debug_session import run_investigation

        assert 'f"investigation.{status}"' in inspect.getsource(run_investigation)


class TestALongConversationIsNotAnArgument:
    """Measured live, on the Opus chat brain, in a thread that had been going a while:

        I could not answer that: OSError: [Errno 7] Argument list too long: 'nice'

    Linux caps a single argv entry at MAX_ARG_STRLEN — 128 KiB — and this backend passes the
    ENTIRE conversation plus ~48 tool schemas as one `-p <prompt>` argument. So raising the
    chat brain to a model that investigates more thoroughly made the prompt bigger and
    eventually fatal: the command was rejected before the process started, and the operator was
    told BOSS could not answer. The same ceiling sits under the coding path, where a repo
    context pack plus a long spec crosses it just as easily.
    """

    def test_a_large_prompt_goes_on_stdin(self):
        from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

        body = (inspect.getsource(ClaudeCliAdapter._command)
                + inspect.getsource(ClaudeCliAdapter._invoke_once))
        assert 'via_stdin = len(prompt.encode("utf-8", "ignore")) > 96_000' in body
        assert '*(("-p",) if via_stdin else ("-p", prompt))' in body
        assert "input=(prompt if via_stdin else None)" in body

    def test_a_small_one_still_goes_on_the_command_line(self):
        """Unchanged for the common case: this is a ceiling, not a redesign."""
        from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

        body = inspect.getsource(ClaudeCliAdapter._invoke_once)
        assert "stdin=(None if via_stdin else subprocess.DEVNULL)" in body

    def test_the_threshold_is_under_the_kernel_limit(self):
        """MAX_ARG_STRLEN is 128 KiB and the rest of argv shares the budget."""
        from boss_engine.services import claude_cli_adapter

        assert "96_000" in inspect.getsource(claude_cli_adapter)
        assert 96_000 < 128 * 1024

    def test_the_coding_path_has_the_same_ceiling_and_the_same_fix(self):
        from boss_engine.services import code_agent

        body = inspect.getsource(code_agent.ClaudeCodeAgent._invoke)
        assert "return argv, body" in body
        assert "cmd, stdin_prompt = base_cmd(capturing)" in body

    def test_the_pipe_is_closed_before_the_stream_is_drained(self):
        """The CLI reads to EOF; leaving it open deadlocks both sides."""
        from boss_engine.services import code_agent

        body = inspect.getsource(code_agent.ClaudeCodeAgent._invoke)
        i = body.index("child.stdin.write(stdin_prompt)")
        assert "child.stdin.close()" in body[i:i + 200]
        assert body.index("child.stdin.close()") < body.index("_drain(child")


class TestAnInvestigationTheContainerOutlived:
    """Measured: a fixing investigation was running when I deployed, the API container was
    recreated under it, and the row stayed `running` forever. Combined with the one-at-a-time
    guard, that meant the first restart during an investigation permanently disabled the feature
    for that project — every later attempt refused in favour of a session that had stopped
    existing."""

    def test_a_stalled_run_is_reclaimed(self):
        from boss_engine.services.debug_session import open_investigation

        body = inspect.getsource(open_investigation)
        assert "status = 'abandoned'" in body
        assert "updated_at < now() - INTERVAL '25 minutes'" in body

    def test_it_is_marked_rather_than_silently_reused(self):
        """"it stopped when the API restarted" is something the operator should be able to read
        afterwards."""
        from boss_engine.services.debug_session import open_investigation

        body = inspect.getsource(open_investigation)
        assert "the process running it stopped (deploy or restart) before it finished" in body

    def test_the_sweep_runs_before_the_one_at_a_time_check(self):
        from boss_engine.services.debug_session import open_investigation

        body = inspect.getsource(open_investigation)
        assert body.index("status = 'abandoned'") < body.index("live = connection.execute")

    def test_and_its_token_dies_with_it(self):
        from boss_engine.services.debug_session import open_investigation

        i = inspect.getsource(open_investigation).index("status = 'abandoned'")
        assert "token = NULL" in inspect.getsource(open_investigation)[i:i + 200]

    def test_a_live_one_heartbeats_so_stale_means_stalled(self):
        """Without it the window would have to exceed the slowest investigation, and would
        still reclaim a live one eventually."""
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        assert 'beats["n"] % 10 == 0' in body
        assert "UPDATE investigations SET updated_at = now() " in body

    def test_the_heartbeat_cannot_fail_the_work(self):
        from boss_engine.services.debug_session import run_investigation

        body = inspect.getsource(run_investigation)
        i = body.index('beats["n"] % 10 == 0')
        assert "except Exception" in body[i:i + 420]


class TestWhatTheFirstFIXINGInvestigationFound:
    """It worked. 84 turns, 641 seconds, `applied: true`, and it verified its own writes by
    re-reading every row and re-probing the app. It also found three things nobody asked it to.

    THE HEALTH CHECK PROBED THE WRONG PORT, with the line numbers:

        "delivery_pipeline.py:724 reads the deployment target ONCE before apply(), while
         apply()'s _ensure_port (deployment_connectors.py:797) allocates the real port (3013)
         into a new local dict... so observe() at :818 builds its URL from the pre-apply target
         and probes the target's stale port 3000."

    Every first deployment of a new target health-checked a port with nothing on it. Change
    Calendar deployed correctly, answered 200 on 3013, and was recorded unhealthy against 3000.

    AND A DEFECT IN MY OWN GUARD: boss-db rejected any statement containing a semicolon, even
    inside a string literal — so an honest `last_error` could not be written.
    """

    def test_the_health_check_reads_the_target_apply_wrote(self):
        from boss_engine.services.delivery_pipeline import execute_deployment

        body = inspect.getsource(execute_deployment)
        i = body.index('rec.step("observe"')
        block = body[i:i + 1400]
        assert "SELECT * FROM deployment_targets WHERE id = :i" in block
        assert block.index("SELECT * FROM deployment_targets") < block.index("conn.observe(")

    def test_a_stale_port_is_corrected_rather_than_accepted(self):
        from boss_engine.services.deployment_connectors import domain_with_port

        # The case that produced a false red: domain says 3000, the app is on 3013.
        assert domain_with_port("192.0.2.202:3000", "192.0.2.202", 3013) == "192.0.2.202:3013"

    def test_a_correct_port_still_means_nothing_to_change(self):
        from boss_engine.services.deployment_connectors import domain_with_port

        assert domain_with_port("192.0.2.202:3013", "192.0.2.202", 3013) is None

    def test_a_bare_domain_still_gains_the_port(self):
        from boss_engine.services.deployment_connectors import domain_with_port

        assert domain_with_port("192.0.2.202", "192.0.2.202", 3013) == "192.0.2.202:3013"

    def test_something_unparseable_is_left_alone(self):
        from boss_engine.services.deployment_connectors import domain_with_port

        assert domain_with_port("app.example.com:staging", "h", 3013) is None

    @pytest.mark.parametrize("statement", [
        "UPDATE workflow_step_runs SET last_error = 'reconciled; deploy succeeded' WHERE id='x'",
        "SELECT * FROM t WHERE name = 'a;b'",
    ])
    def test_a_semicolon_inside_a_string_is_not_a_second_statement(self, statement):
        from boss_engine.services.debug_session import sql_is_allowed

        assert sql_is_allowed(statement, allow_write=True)[0], statement

    def test_a_real_second_statement_is_still_refused(self):
        from boss_engine.services.debug_session import sql_is_allowed

        assert sql_is_allowed("SELECT 1; DELETE FROM jobs", allow_write=True)[0] is False

    def test_and_a_keyword_quoted_as_data_is_not_a_migration(self):
        from boss_engine.services.debug_session import sql_is_allowed

        assert sql_is_allowed("UPDATE x SET note = 'drop table y' WHERE id = 'z'",
                              allow_write=True)[0] is True

    def test_blanking_preserves_the_shape(self):
        from boss_engine.services.debug_session import _without_string_literals

        original = "UPDATE t SET a = 'x;y' WHERE b = 'q'"
        assert len(_without_string_literals(original)) == len(original)
        assert ";" not in _without_string_literals(original)
