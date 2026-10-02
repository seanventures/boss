"""Who runs a workflow step: one rule for every kind, recorded on the step, and actually run.

THE INCIDENT. A map of the workflow runtime found four different answers to "who runs this step"
and one of them could never happen: `agent_steps.choose_agent` asked the model through
`adapter.complete()`, a method no chat adapter has. The AttributeError was swallowed by a bare
`except`, every multi-agent pick fell through to word overlap, and the run said the reason was
"no model was available" — on installs whose model was fine. The tests of the day accepted any
`how`, so they passed; the captain tests asserted lines of source text.

So these tests RUN things. The unit half drives `choose_agent` against stub adapters shaped like
the real ones. The database half publishes workflows, ticks the runner, and reads back who the
skill was called as, which tools the agent was offered, and what the step recorded in `ran_as`.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_steps, step_agents
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)


# ── Unit: the routing question is really asked ───────────────────────────────────────────────

POOL = [
    {"role_key": "video_editor", "mission": "cuts video", "holds": ["video.trim"]},
    {"role_key": "media_publisher", "mission": "publishes", "holds": ["publish_youtube"]},
]


class _Answers:
    """A chat adapter with ONLY the method every real one has."""

    def __init__(self, reply):
        self.reply = reply
        self.asked: list[str] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.asked.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return ("sess", self.reply, {})


class _OnlyComplete:
    """The method choose_agent used to call. No adapter has it; this one is here to prove it is
    never the thing being called."""

    def complete(self, **_):  # pragma: no cover - must never be reached
        return json.dumps({"role_key": "media_publisher", "why": "via complete"})


def _route_with(monkeypatch, adapter):
    from boss_engine.services import claude_cli_adapter

    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter", lambda _s: adapter)
    return agent_steps.choose_agent(None, Settings(), "p", instructions="publish the short",
                                    pool=[dict(a) for a in POOL])


def test_the_model_is_actually_asked_and_its_answer_used(monkeypatch):
    adapter = _Answers(json.dumps({"role_key": "media_publisher", "why": "it publishes"}))
    picked = _route_with(monkeypatch, adapter)
    assert picked == {"role_key": "media_publisher", "why": "it publishes", "how": "model"}
    assert adapter.asked and "publish the short" in adapter.asked[0]
    # It was told who it could choose from and what each one holds.
    assert "video.trim" in adapter.asked[0] and "publish_youtube" in adapter.asked[0]


def test_an_adapter_with_only_complete_is_not_how_the_model_is_asked(monkeypatch):
    """The regression, pinned: had choose_agent still called .complete(), this stub would have
    answered and `how` would read "model"."""
    picked = _route_with(monkeypatch, _OnlyComplete())
    assert picked["how"] == "word_overlap"
    assert "AttributeError" in picked["why"]


def test_a_model_failure_is_reported_as_what_it_was(monkeypatch):
    """Not "no model was available" — the sentence that was false on every install it showed."""
    picked = _route_with(monkeypatch, _Answers(RuntimeError("usage limit reached")))
    assert picked["how"] == "word_overlap"
    assert "usage limit reached" in picked["why"]
    assert "no model was available" not in picked["why"]


def test_a_model_naming_somebody_outside_the_pool_is_not_obeyed(monkeypatch):
    picked = _route_with(monkeypatch, _Answers(json.dumps({"role_key": "cto", "why": "x"})))
    assert picked["how"] == "word_overlap"
    assert "named 'cto'" in picked["why"]
    # And the fallback is the agent whose skills share a word with the instruction.
    assert picked["role_key"] == "media_publisher"
    assert "publish" in picked["why"]


def test_the_fallback_says_when_nothing_matched_and_is_stable(monkeypatch):
    from boss_engine.services import claude_cli_adapter

    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter",
                        lambda _s: _Answers(RuntimeError("offline")))
    pool = [{"role_key": "b_agent", "holds": ["x.one", "x.two"]},
            {"role_key": "a_agent", "holds": ["y.one"]}]
    first = agent_steps.choose_agent(None, Settings(), "p", instructions="zzz", pool=pool)
    again = agent_steps.choose_agent(None, Settings(), "p", instructions="zzz",
                                     pool=list(reversed(pool)))
    assert first["role_key"] == again["role_key"] == "a_agent"
    assert "no agent's skills share a word" in first["why"]


def test_every_chat_adapter_can_answer_the_routing_question():
    """ENUMERATED, so a new adapter cannot ship without the one-shot call choose_agent makes.
    Every class in the adapter modules that can hold a conversation must also answer once."""
    import inspect

    from boss_engine.services import claude_cli_adapter, codex_cli_adapter, openai_adapter

    checked = []
    for module in (claude_cli_adapter, codex_cli_adapter, openai_adapter):
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not hasattr(cls, "create_chat_completion"):
                continue
            checked.append(name)
            assert callable(getattr(cls, "create_text_response", None)), (
                f"{name} can chat but cannot answer choose_agent's routing question")
    assert {"OpenAIAdapter", "ClaudeCliAdapter", "FallingBackAdapter"} <= set(checked)


def test_a_version_is_read_under_the_rules_it_was_published_under():
    """REWRITTEN: this asserted the text "check_agents=False" in `_run_spec`'s source, and the
    behaviour it described was wrong — it relaxed the rules for every in-flight run while START
    still refused the same version. `human` was accepted on any step until these rules, and the
    builder offered it everywhere. A stored version without the stamp is now read under the old
    rules wherever it is read (start, run, migrate); a stamped one, under the new."""
    spec = {"entry_step": "s", "steps": [
        {"id": "s", "kind": "capability", "captain": "human",
         "binding": {"capability": "notify", "inputs": {"message": "hi"}}}]}
    with pytest.raises(workflows.WorkflowValidationError, match="only an approval gate"):
        workflows.validate_workflow_spec(spec)

    old = workflows._published_spec(json.dumps(spec))
    assert old["steps"][0]["captain"] == "human"
    tagged = step_agents.tag_steps(old)
    assert step_agents.ran_under_old_rules(tagged["steps"][0])

    stamped = {**spec, step_agents.AGENT_RULES_KEY: step_agents.CURRENT_AGENT_RULES}
    with pytest.raises(workflows.WorkflowValidationError, match="only an approval gate"):
        workflows._published_spec(json.dumps(stamped))


def test_only_the_versions_stamp_decides_which_rules_a_step_runs_under():
    """A caller cannot opt a new step into the old rules by writing the tag on it."""
    spec = {step_agents.AGENT_RULES_KEY: step_agents.CURRENT_AGENT_RULES, "steps": [
        {"id": "s", step_agents.AGENT_RULES_KEY: step_agents.LEGACY_AGENT_RULES}]}
    assert not step_agents.ran_under_old_rules(step_agents.tag_steps(spec)["steps"][0])
    # And a step nobody tagged — built in code, or by a test — is under the current rules.
    assert not step_agents.ran_under_old_rules({"id": "s"})


# ── Database: the runner, end to end ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_step_runs LIMIT 1"))
            if c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar() is None:
                pytest.skip("boss_test has no workspace")
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'who runs a step', :s)"),
                  {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("workflow_step_runs", "workflow_runs", "workflow_custom_steps",
                      "workflow_mappings", "workflow_versions", "workflow_definitions",
                      "project_skills", "mcp_servers", "decisions", "events", "receipts",
                      "agent_runs", "role_skills", "roles", "skill_invocations",
                      "policy_evaluations", "deadletters"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


class Ctx:
    def __init__(self, engine):
        self._engine = engine
        self.settings = Settings()

    def begin(self):
        return self._engine.begin()


def _catalogue(c, project_id, n):
    """Real skill ids from the catalogue, so held_skills offers them as tools."""
    from boss_engine.services.entitlements import EntitlementBrokerService

    ids = sorted(s["id"] for s in EntitlementBrokerService(Settings()).list_skills(
        project_id, connection=c) if s.get("enabled", True))
    if len(ids) < n:
        pytest.skip("the skill catalogue is smaller than this test needs")
    return ids[:n]


def _agent(c, project_id, role_key, *skills, mission=""):
    c.execute(
        text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
             "VALUES (:i, :p, :r, :r, CAST(:s AS JSONB)) "
             "ON CONFLICT (project_id, role_key) DO NOTHING"),
        {"i": f"role_{uuid.uuid4().hex[:10]}", "p": project_id, "r": role_key,
         "s": json.dumps({"mission": mission})},
    )
    for skill_id in skills:
        c.execute(
            text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                 "VALUES (:i, :p, :r, :s, 'builtin') ON CONFLICT DO NOTHING"),
            {"i": f"rs_{uuid.uuid4().hex[:10]}", "p": project_id, "r": role_key, "s": skill_id})


def _registered(c, project_id, skill_id, risk_tier=0):
    """A project_skills row, which is what lets a custom step bind the skill at a known tier."""
    c.execute(
        text("INSERT INTO project_skills (id, project_id, skill_id, enabled, config, provenance) "
             "VALUES (:i, :p, :s, true, CAST(:c AS JSONB), '{\"trusted\": true}'::jsonb) "
             "ON CONFLICT DO NOTHING"),
        {"i": f"ps_{uuid.uuid4().hex[:10]}", "p": project_id, "s": skill_id,
         "c": json.dumps({"risk_tier": risk_tier})})


def _mcp_server(c, project_id, name):
    sid = f"mcp_{uuid.uuid4().hex[:10]}"
    c.execute(
        text("INSERT INTO mcp_servers (id, project_id, name, url, risk_tier, enabled, "
             "provenance, tool_risk_overrides) VALUES (:i, :p, :n, :u, 0, true, "
             "'{\"trusted\": true}'::jsonb, '{}'::jsonb)"),
        {"i": sid, "p": project_id, "n": name, "u": f"http://127.0.0.1:1/{name}"})
    return sid


def _publish(c, project_id, key, steps):
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product")
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"], actor="test",
        spec={"entry_step": steps[0]["id"], "steps": steps})


def _start_and_tick(c, eng, project_id, version, input_data=None):
    run = workflows.start_run(
        c, project_id=project_id, actor="ceo", input_data=input_data or {},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version["id"])
    runner.tick(c, Ctx(eng), project_id=project_id)
    return run


def _step(c, run_id, step_id):
    return c.execute(
        text("SELECT status, output, last_error, decision_id FROM workflow_step_runs "
             "WHERE workflow_run_id=:r AND step_id=:s"),
        {"r": run_id, "s": step_id}).mappings().first()


class _Loop:
    """The agent loop's model. Records which tools the bounded run was offered, then finishes."""

    def __init__(self):
        self.tools: list[str] = []

    def create_chat_completion(self, messages, tools=None, **_):
        self.tools = [t["function"]["name"] for t in (tools or [])]
        reply = "done"
        return ({"content": reply, "tool_calls": None,
                 "raw": {"role": "assistant", "content": reply}},
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


@pytest.fixture()
def loop(monkeypatch):
    from boss_engine.services import agent_loop, claude_cli_adapter

    model = _Loop()
    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _s: model)
    monkeypatch.setattr(agent_loop.MCPClient, "list_tools",
                        lambda self, url: [{"name": "lookup", "description": "look it up",
                                            "inputSchema": {"type": "object"}}])
    # Routing defaults to "the model is down", so a test that wants a model pick says so.
    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter",
                        lambda _s: _Answers(RuntimeError("no routing model in this test")))
    return model


def _agent_step(step_id="work", **binding):
    return {"id": step_id, "kind": "capability", "next": [], "approval": {"mode": "auto"},
            "binding": {"agent_work": True,
                        "inputs": {"instructions": "post the weekly numbers"}, **binding}}


def test_an_agent_step_naming_nobody_is_routed_by_the_model_and_says_so(eng, project, loop,
                                                                        monkeypatch):
    from boss_engine.services import claude_cli_adapter

    with eng.begin() as c:
        first, second = _catalogue(c, project, 2)
        _agent(c, project, "editor", first, mission="edits")
        _agent(c, project, "poster", second, mission="posts")
        router = _Answers(json.dumps({"role_key": "poster", "why": "it posts things"}))
        monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter", lambda _s: router)
        version = _publish(c, project, "route", [_agent_step()])
        run = _start_and_tick(c, eng, project, version)
        row = _step(c, run["id"], "work")

    assert row["status"] == "succeeded", row["last_error"]
    ran_as = row["output"]["ran_as"]
    assert (ran_as["agent"], ran_as["how"], ran_as["why"]) == ("poster", "model", "it posts things")
    assert sorted(ran_as["considered"]) == ["editor", "poster"]
    assert ran_as["skills_offered"] == [second]
    assert loop.tools and all(second.replace(".", "_") in t or second in t for t in loop.tools)


def test_an_agent_step_scoped_to_skills_offers_only_those(eng, project, loop):
    """R3: a step can narrow WHICH of an agent's skills it may use. Before, the step got whatever
    the agent held and `binding.skills` was accepted, stored and ignored."""
    with eng.begin() as c:
        first, second = _catalogue(c, project, 2)
        _agent(c, project, "editor", first, second)
        version = _publish(c, project, "scoped", [_agent_step(skills=[second])])
        run = _start_and_tick(c, eng, project, version)
        row = _step(c, run["id"], "work")

    assert row["status"] == "succeeded", row["last_error"]
    assert row["output"]["ran_as"]["agent"] == "editor"
    assert row["output"]["ran_as"]["how"] == "only_holder"
    assert row["output"]["skills_offered"] == [second]
    assert len(loop.tools) == 1


def test_an_agent_step_with_an_mcp_server_reaches_that_server_and_no_other(eng, project, loop):
    """R3 for MCP: a bounded agent run had no MCP at all. A step naming a server reaches it —
    only it — through the loop's usual risk check."""
    with eng.begin() as c:
        (skill,) = _catalogue(c, project, 1)
        _agent(c, project, "researcher", skill)
        _mcp_server(c, project, "wiki")
        _mcp_server(c, project, "billing")
        version = _publish(c, project, "mcp", [_agent_step(mcp=["wiki"])])
        run = _start_and_tick(c, eng, project, version)
        row = _step(c, run["id"], "work")

    assert row["status"] == "succeeded", row["last_error"]
    assert row["output"]["ran_as"]["mcp_offered"] == ["wiki"]
    mcp_tools = [t for t in loop.tools if t.startswith("mcp_")]
    assert mcp_tools == ["mcp_wiki_lookup"]


def test_a_scoped_skill_the_named_agent_lacks_is_refused_at_publish(eng, project):
    """A step is never a way to give an agent something."""
    with eng.begin() as c:
        first, second = _catalogue(c, project, 2)
        _agent(c, project, "editor", first)
        with pytest.raises(workflows.WorkflowBindingBlockedError) as error:
            _publish(c, project, "lacks", [{**_agent_step(skills=[second]), "captain": "editor"}])
    assert f"which editor does not hold" in str(error.value)
    assert "Agents page" in str(error.value)


def test_an_agent_the_project_made_can_be_named_on_a_custom_step(eng, project, monkeypatch):
    """R4: `docs_writer`, an agent that exists on the live install, was refused as "not a role
    this workforce has" because names were checked against a constant. Any agent of the
    project's own is accepted, and the skill is called AS that agent."""
    seen = {}

    def fake_invoke(connection, settings, project_id, skill_id, caller, args, *, actor,
                    skill_spec=None):
        seen.update(caller=caller, actor=actor, spec=skill_spec)
        return {"status": "succeeded", "result": {"ok": True}}

    from boss_engine.services import agent_loop

    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", fake_invoke)
    with eng.begin() as c:
        (skill,) = _catalogue(c, project, 1)
        _agent(c, project, "brand_new_writer", skill)
        _registered(c, project, skill)
        custom_steps.create(c, project_id=project, label="Write", binding_kind="skill",
                            skill_id=skill, actor="test", step_key="write")
        version = _publish(c, project, "named", [
            {"id": "write", "kind": "capability", "next": [], "captain": "brand_new_writer",
             "approval": {"mode": "manual"}, "binding": {"custom_step": "write", "inputs": {}}}])
        run = _start_and_tick(c, eng, project, version)
        gate = _step(c, run["id"], "write")
        if gate["decision_id"]:
            c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"),
                      {"d": gate["decision_id"]})
            runner.tick(c, Ctx(eng), project_id=project)
            runner.tick(c, Ctx(eng), project_id=project)
        row = _step(c, run["id"], "write")

    assert row["status"] == "succeeded", row["last_error"]
    assert seen["caller"] == seen["actor"] == "brand_new_writer"
    assert seen["spec"] is None, "naming an agent must not widen the skill's callers"
    assert row["output"]["ran_as"]["how"] == "named"


def test_a_custom_step_with_two_holders_is_put_to_the_model(eng, project, monkeypatch):
    from boss_engine.services import agent_loop, claude_cli_adapter

    monkeypatch.setattr(agent_loop, "_invoke_agent_skill",
                        lambda *a, **k: {"status": "succeeded", "result": {}})
    router = _Answers(json.dumps({"role_key": "second_holder", "why": "closer remit"}))
    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter", lambda _s: router)
    with eng.begin() as c:
        (skill,) = _catalogue(c, project, 1)
        _agent(c, project, "first_holder", skill)
        _agent(c, project, "second_holder", skill)
        _registered(c, project, skill)
        custom_steps.create(c, project_id=project, label="Do it", binding_kind="skill",
                            skill_id=skill, actor="test", step_key="do_it",
                            summary="the thing")
        version = _publish(c, project, "two", [
            {"id": "do", "kind": "capability", "next": [], "approval": {"mode": "manual"},
             "binding": {"custom_step": "do_it", "inputs": {}}}])
        run = _start_and_tick(c, eng, project, version)
        gate = _step(c, run["id"], "do")
        if gate["decision_id"]:
            c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"),
                      {"d": gate["decision_id"]})
            runner.tick(c, Ctx(eng), project_id=project)
            runner.tick(c, Ctx(eng), project_id=project)
        row = _step(c, run["id"], "do")

    assert row["status"] == "succeeded", row["last_error"]
    assert row["output"]["ran_as"]["agent"] == "second_holder"
    assert row["output"]["ran_as"]["how"] == "model"
    assert router.asked and "the thing" in router.asked[0]


def test_a_manual_agent_step_waits_for_a_person_before_the_agent_starts(eng, project, loop):
    """The builder offered "manual" on an agent step and the card said "needs you", and the
    runner ignored it: `_gate_subject` returned None for a step with no capability key."""
    with eng.begin() as c:
        (skill,) = _catalogue(c, project, 1)
        _agent(c, project, "editor", skill)
        step = _agent_step()
        step["approval"] = {"mode": "manual"}
        version = _publish(c, project, "manual_agent", [step])
        run = _start_and_tick(c, eng, project, version)
        waiting = _step(c, run["id"], "work")
        assert waiting["status"] == "waiting" and waiting["decision_id"], dict(waiting)
        assert loop.tools == [], "the agent started before anybody approved"
        c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"),
                  {"d": waiting["decision_id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step(c, run["id"], "work")
    assert row["status"] == "succeeded", row["last_error"]


#: EVERY BINDING THE PRODUCT RUNNER EXECUTES. `execute_step` dispatches on these keys; a new one
#: added there without a `ran_as` record shows the operator a step with nobody on it.
EXECUTED_BINDINGS = ("agent_work", "typed_decision", "custom_step", "capability")


def test_every_binding_the_runner_executes_is_listed_here():
    import inspect

    # The dispatch lives in `_execute_step` on the Community tree (execute_step wraps it to write
    # the skipped-input markers); read whichever function holds it.
    source = inspect.getsource(getattr(runner, "_execute_step", runner.execute_step))
    dispatched = {key for key in ("agent_work", "typed_decision", "custom_step", "capability",
                                  "skill_id", "integration_connection_id")
                  if f'binding.get("{key}")' in source}
    assert dispatched <= set(EXECUTED_BINDINGS) | {"capability"}, dispatched
    assert set(EXECUTED_BINDINGS) - {"capability"} <= dispatched


def test_every_executed_step_kind_records_who_ran_it(eng, project, loop, monkeypatch):
    """The enforcing test: one of each kind, run, and each one's output carries a `ran_as` the
    run view can show — agent, how it was chosen, and why."""
    from boss_engine.services import agent_loop, typed_decisions

    monkeypatch.setattr(agent_loop, "_invoke_agent_skill",
                        lambda *a, **k: {"status": "succeeded", "result": {}})
    monkeypatch.setattr(typed_decisions, "run_step",
                        lambda *a, **k: {"answer": "yes"})

    class _Tool:
        name, kind, description, parameters = "get_costs", "read", "costs", {}

        @staticmethod
        def handler(ctx, *, project_id, actor, args):
            return {"spend_usd": 0}

    class _Registry:
        @staticmethod
        def specs():
            return [_Tool()]

    monkeypatch.setattr(runner, "_tool_registry", lambda: _Registry())
    with eng.begin() as c:
        (skill,) = _catalogue(c, project, 1)
        _agent(c, project, "editor", skill)
        _registered(c, project, skill)
        custom_steps.create(c, project_id=project, label="Write", binding_kind="skill",
                            skill_id=skill, actor="test", step_key="write")
        context = {"input": {}, "steps": {}, "run_id": "", "step_index": 0}
        kinds = {
            "agent_work": _agent_step(),
            "custom_step": {"id": "w", "kind": "capability",
                            "binding": {"custom_step": "write", "inputs": {}}},
            "capability": {"id": "c", "kind": "capability",
                           "binding": {"capability": "cost_check", "inputs": {}}},
            "typed_decision": {"id": "t", "kind": "capability",
                               "binding": {"typed_decision": {"question": "q"}, "inputs": {}}},
        }
        assert set(kinds) == set(EXECUTED_BINDINGS)
        for kind, step in kinds.items():
            output = runner.execute_step(c, Ctx(eng), project_id=project, actor="ceo",
                                         step=step, context=dict(context))
            ran_as = output.get("ran_as") or {}
            assert {"agent", "how", "why"} <= set(ran_as), (kind, output)
            assert ran_as["agent"] and ran_as["why"], (kind, ran_as)
