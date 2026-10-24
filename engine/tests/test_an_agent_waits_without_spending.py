"""An agent waits for BOSS's own work in one call, without a model call per look; a turn is sized
by what it was decided to be; a turn that stops short ends with one press that resumes it.

THE EXAM (Metatron, 2026-10-24). Asked to make sure three code-graph index jobs (about a minute
each) had finished and then answer a graph question, every turn read code_graph_status over and
over — eighteen times in one — ran out of its 14 round-trips, and handed back "Next action: poll
code_graph_status again". Earlier replies were carried forward as facts ("kg_cypher returned
forbidden" after it was fixed), and a guessed `attempts, started_at` on jobs failed without the
real column names in the error the next turn could see.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.config import Settings
from boss_engine.services import call_deadline, turn_budget
from boss_engine.services import wait_for as waiting


# ── the wait ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def jobs_db():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE jobs (id TEXT, project_id TEXT, kind TEXT, status TEXT, "
                       "attempt INTEGER, max_attempts INTEGER, last_error TEXT, run_after TEXT, "
                       "updated_at TEXT)"))
        c.execute(text("INSERT INTO jobs VALUES ('job_1','p1','knowledge.repository.index',"
                       "'running',1,2,NULL,NULL,'2026-10-24'),"
                       "('job_2','p2','x','running',1,2,NULL,NULL,'2026-10-24')"))
    return engine


class _Clock:
    """A fake clock the fake sleep advances, so a 10-minute wait runs in milliseconds."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []
        self.on_sleep = None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.slept))


@pytest.fixture()
def fake_time(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(waiting, "_clock", clock.clock)
    monkeypatch.setattr(waiting, "_sleep", clock.sleep)
    return clock


def _settings(**over):
    return Settings().model_copy(update=over)


def test_a_wait_returns_the_final_state_when_the_job_settles(jobs_db, fake_time):
    def worker_finishes(n):
        if n == 3:
            with jobs_db.begin() as c:
                c.execute(text("UPDATE jobs SET status = 'succeeded' WHERE id = 'job_1'"))

    fake_time.on_sleep = worker_finishes
    out = waiting.wait(jobs_db.begin, _settings(), project_id="p1", what="job", ref="job_1")
    assert out["done"] is True and out["outcome"] == "succeeded"
    assert out["state"]["status"] == "succeeded"
    assert out["reads"] == 4, "it re-read the record until it settled, and then stopped"
    # Short first, then longer: a job about to finish is reported promptly, a long one cheaply.
    assert fake_time.slept[0] == waiting.FIRST_POLL_SECONDS
    assert fake_time.slept[-1] > fake_time.slept[0]


def test_a_wait_that_times_out_says_still_running_and_how_to_resume(jobs_db, fake_time):
    out = waiting.wait(jobs_db.begin, _settings(), project_id="p1", what="job", ref="job_1",
                       timeout_seconds=60)
    assert out["done"] is False and out["state"]["status"] == "running"
    assert 59 <= out["waited_seconds"] <= 61
    assert "Keep waiting" == out["resume"]["label"] and "job_1" in out["resume"]["message"]
    assert max(fake_time.slept) <= waiting.MAX_POLL_SECONDS


def test_the_wait_is_capped_by_the_install_and_by_the_turn(jobs_db, fake_time):
    out = waiting.wait(jobs_db.begin, _settings(agent_wait_max_seconds=30), project_id="p1",
                       what="job", ref="job_1", timeout_seconds=10_000)
    assert out["waited_seconds"] <= 30.5
    # Inside a turn: never past the turn's tool time, less the margin the answer needs.
    with call_deadline.until(time.monotonic() + 25):
        out = waiting.wait(jobs_db.begin, _settings(), project_id="p1", what="job",
                           ref="job_1", timeout_seconds=600)
    assert out["waited_seconds"] <= 25 - waiting.TURN_MARGIN_SECONDS + 0.5
    assert "this turn's time" in out["note"]


def test_a_wait_cannot_reach_another_projects_record_or_a_wrong_kind(jobs_db, fake_time):
    with pytest.raises(waiting.WaitRefused):
        waiting.wait(jobs_db.begin, _settings(), project_id="p1", what="job", ref="job_2")
    with pytest.raises(waiting.WaitRefused):
        waiting.wait(jobs_db.begin, _settings(), project_id="p1", what="code_task", ref="job_1")
    with pytest.raises(waiting.WaitRefused):
        waiting.wait(jobs_db.begin, _settings(), project_id="p1", what="nonsense", ref="x")


def test_a_wait_spends_no_model_call_while_it_waits(jobs_db, fake_time):
    """Through the real tool loop: the model asks to wait once and answers once. However many
    times the record is re-read in between, that is two model calls."""
    def worker_finishes(n):
        if n == 6:
            with jobs_db.begin() as c:
                c.execute(text("UPDATE jobs SET status = 'succeeded' WHERE id = 'job_1'"))

    fake_time.on_sleep = worker_finishes

    class Model:
        def __init__(self):
            self.calls = 0

        def create_chat_completion(self, messages, tools=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return ({"raw": {"role": "assistant", "content": ""}, "content": "",
                         "tool_calls": [{"id": "c1", "name": "wait_for", "arguments": json.dumps(
                             {"what": "job", "id": "job_1"})}]}, {})
            return ({"raw": {"role": "assistant", "content": "It finished."},
                     "content": "It finished."}, {})

    model = Model()
    agent = EngineAgent.__new__(EngineAgent)
    agent._adapter, agent._notes, agent.ambient = model, [], {}
    agent.registry = type("R", (), {"openai_schema": lambda self: [
        {"type": "function", "function": {"name": "wait_for", "parameters": {}}}]})()

    def run_tool(name, args, **kw):
        return waiting.wait(jobs_db.begin, _settings(), project_id="p1", what=args["what"],
                            ref=args["id"])

    agent._run_tool = run_tool
    turn = agent.chat(project_id="p1", message="wait for job_1", max_steps=14)
    result = turn.trace[0]["result"]
    assert result["done"] is True and result["reads"] == 7
    assert model.calls == 2, "a model call was spent while waiting"
    assert turn.exhausted == ""


def test_wait_for_is_one_read_tool_every_agent_holds_and_each_kind_keeps_its_permission():
    from boss_engine.services.agent_baseline import FUNDAMENTALS_TOOLS
    from boss_engine.tools.builtin import build_discuss_read_registry
    from boss_engine.tools.object_args import CONFINED

    spec = build_discuss_read_registry().get("wait_for")
    assert spec.kind == "read" and "wait_for" in FUNDAMENTALS_TOOLS
    assert spec.permission_for({"what": "job"}) == "job:read"
    assert spec.permission_for({"what": "code_task"}) == "code_task:read"
    assert spec.permission_for({"what": "workflow_run"}) == "workflow_run:read"
    assert spec.permission_for({"what": "code_graph"}) == "knowledge:read"
    assert set(spec.parameters["properties"]["what"]["enum"]) == set(waiting.READERS)
    assert "id" in CONFINED["wait_for"]
    # One way to wait in a turn, one way to keep an eye on something across turns.
    watch = build_discuss_read_registry().get("watch")
    assert "wait_for" in watch.description and "watch" in spec.description


def test_the_code_graph_is_settled_only_when_no_repository_is_still_indexing(monkeypatch):
    from boss_engine.services.knowledge import repo_index

    rows = [{"repo": "o/a", "status": "indexed", "job_id": "j1", "binding_id": "b1"},
            {"repo": "o/b", "status": "running", "job_id": "j2", "binding_id": "b2"}]
    live = {"j2": True}
    monkeypatch.setattr(repo_index, "status", lambda c, p: {"repositories": rows})
    monkeypatch.setattr(repo_index, "_in_flight", lambda c, r: live.get(r["job_id"], False))
    assert waiting._code_graph(None, "p", "", "").settled is False
    assert waiting._code_graph(None, "p", "", "o/a").settled is True
    live["j2"] = False   # its job died: waiting longer would not move it
    assert waiting._code_graph(None, "p", "", "").settled is True
    rows.append({"repo": "o/c", "status": "pending", "job_id": None, "binding_id": "b3"})
    assert waiting._code_graph(None, "p", "", "").settled is False


# ── the turn's size ─────────────────────────────────────────────────────────────────────────
def test_a_turn_is_sized_by_what_it_was_decided_to_be_under_one_ceiling():
    s = Settings()
    light, light_steps, kind = turn_budget.for_turn(s, action="answer", depth=None)
    assert kind == turn_budget.LIGHT and light.model_calls == s.chat_turn_model_calls
    assert light_steps == s.chat_max_steps
    for action, depth in (("investigate", None), ("operate", None), ("answer", "investigate")):
        deep, deep_steps, kind = turn_budget.for_turn(s, action=action, depth=depth)
        assert kind == turn_budget.DEEP, (action, depth)
        assert deep.model_calls > light.model_calls and deep_steps > light_steps
    # The ceiling holds whatever the settings say.
    huge = s.model_copy(update={"chat_deep_turn_model_calls": 500, "chat_deep_max_steps": 500})
    capped, capped_steps, _ = turn_budget.for_turn(huge, action="operate", depth=None)
    assert capped.model_calls == capped_steps == s.chat_turn_model_calls_ceiling
    # Time is the same for every turn: the console's stall line does not move with depth.
    assert capped.seconds == light.seconds == s.chat_turn_seconds


# ── what a turn that stopped short offers ─────────────────────────────────────────────────────
class _Turn:
    def __init__(self, trace=(), exhausted=""):
        self.trace, self.exhausted = list(trace), exhausted


def test_an_exhausted_turn_offers_continue_and_a_finished_one_offers_nothing():
    assert turn_budget.continue_steps([_Turn()]) == []
    steps = turn_budget.continue_steps([_Turn(exhausted="steps")])
    assert [s["id"] for s in steps] == ["continue"] and steps[0]["depth"] == "investigate"
    assert turn_budget.continue_steps([_Turn()], budget_ran_out=True)[0]["id"] == "continue"


def test_a_cut_short_wait_offers_keep_waiting_unless_a_later_wait_settled_it():
    cut = {"tool": "wait_for", "args": {"what": "job", "id": "job_1"},
           "result": {"done": False, "resume": {"label": "Keep waiting",
                                                "message": "Keep waiting for job job_1"}}}
    done = {"tool": "wait_for", "args": {"what": "job", "id": "job_1", "timeout_seconds": 9},
            "result": {"done": True}}
    steps = turn_budget.continue_steps([_Turn([cut], exhausted="budget")])
    assert steps == [{"id": "keep_waiting", "label": "Keep waiting", "depth": "investigate",
                      "message": "Keep waiting for job job_1"}]
    assert turn_budget.continue_steps([_Turn([cut]), _Turn([done])]) == []


def test_the_loop_marks_a_turn_it_made_answer_early():
    class Busy:
        def create_chat_completion(self, messages, tools=None, **kw):
            if tools:
                return ({"raw": {"role": "assistant", "content": ""}, "content": "",
                         "tool_calls": [{"id": "c", "name": "code_graph_status",
                                         "arguments": "{}"}]}, {})
            self.prompt = messages[-1]["content"]
            return ({"raw": {"role": "assistant", "content": "partial"}, "content": "partial"},
                    {})

    model = Busy()
    agent = EngineAgent.__new__(EngineAgent)
    agent._adapter, agent._notes, agent.ambient = model, [], {}
    agent.registry = type("R", (), {"openai_schema": lambda self: [
        {"type": "function", "function": {"name": "code_graph_status", "parameters": {}}}]})()
    agent._run_tool = lambda name, args, **kw: {"ok": True, "status": "running"}
    turn = agent.chat(project_id="p", message="is it done?", max_steps=3)
    assert turn.exhausted == "steps"
    assert "Continue" in model.prompt and "ask them to ask again" in model.prompt
    assert "What is left" in model.prompt


# ── fresh state over recalled state ─────────────────────────────────────────────────────────
def test_earlier_replies_are_said_to_be_history_not_state():
    from boss_engine.services.agent_baseline import PRIMER
    from boss_engine.services.intent_router import history_is_not_state

    assert "NOW, NOT THEN" in PRIMER and "wait_for" in PRIMER
    assert history_is_not_state([{"role": "user", "content": "hi"}]) == ""
    note = history_is_not_state([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "kg_cypher returned forbidden",
         "created_at": "2026-10-24T08:52:09+00:00"}])
    assert "HISTORY, NOT STATE" in note and "1 earlier reply" in note and "minutes old" in note


def test_kg_schema_reaches_every_chat_turn():
    from boss_engine.services.agent_baseline import KNOWLEDGE_TOOLS
    from boss_engine.services.knowledge.scopes import DEFAULTS, offers_tool
    from boss_engine.tools.builtin import build_discuss_read_registry

    registry = build_discuss_read_registry(edition="community")
    assert {"kg_schema", "kg_cypher"} <= set(registry.names())
    assert registry.get("kg_schema").kind == "read" and "kg_schema" in KNOWLEDGE_TOOLS
    assert "kg_schema" in registry.get("kg_cypher").description
    # A chat turn reads unscoped (no stage), and every stage that offers Cypher offers the schema.
    assert offers_tool(None, "kg_schema")
    for policy in DEFAULTS.values():
        assert offers_tool(policy, "kg_schema") or not offers_tool(policy, "kg_cypher")


def test_an_unknown_column_error_names_the_real_columns():
    from boss_engine.tools import builtin

    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE jobs (id TEXT, project_id TEXT, status TEXT, attempt INTEGER, "
                       "created_at TEXT)"))

    class Ctx:
        settings = None

        @contextmanager
        def begin(self):
            with engine.begin() as c:
                yield c

    real = builtin._text
    try:
        builtin._text = (lambda sql: real("SELECT name FROM pragma_table_info(:t)")
                         if "information_schema.columns" in sql else real(sql))
        out = builtin._query_project_data(Ctx(), "p1", {
            "table": "jobs", "columns": ["id", "attempts", "started_at"]})
    finally:
        builtin._text = real
    assert out["ok"] is False
    # In the error itself: the next turn sees only the error string (tool_calls keep it alone).
    assert "['attempts', 'started_at']" in out["error"]
    assert "id, project_id, status, attempt, created_at" in out["error"]
