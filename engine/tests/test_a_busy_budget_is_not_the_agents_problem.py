"""A transient budget refusal is never the agent's problem.

Found live: five "Video edit and publish" runs started together, the per-minute burst cap was
busy, and inside the 'edit' agent step every media.fetch_link call came back to the AGENT as
"budget refused: Burst cap exceeded ($/min). Retry shortly." The agent read it as a broken tool
and declared "Edit is blocked: skill_media_fetch_link repeatedly denied ..." — the step stopped
on something that clears by itself and that no person had to decide.

Now the call waits and is made again where agent tool calls reach skills
(agent_loop.BudgetWaiter); past the bound a workflow's agent step ends `budget_wait` and the
WORKFLOW step waits (wait_step) and runs again on a later tick. A refusal a person must lift
still reaches the agent as before. Nothing sleeps for real here: the clock and the sleeper are
injected.
"""
from __future__ import annotations

import inspect
import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_loop, agent_steps

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"

BURST = {"refusal": "burst", "transient": True, "retry_after_seconds": 20,
         "message": "Burst cap exceeded ($/min). Retry shortly.", "decision_id": None}
CAP = {"refusal": "cap_exceeded", "transient": False, "retry_after_seconds": None,
       "message": "Budget cap exceeded. Decision created.", "decision_id": "dec_x"}


def _refused(budget):
    return {"status": "denied", "skill_id": "media.fetch_link",
            "reason": f"budget refused: {budget['message']}", "budget": dict(budget)}


SERVED = {"status": "succeeded", "skill_id": "media.fetch_link",
          "result": {"media_artifact_id": "art_fetched"}}


class FakeTime:
    """A clock that only moves when somebody sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _calls(*results):
    queue = list(results)
    made: list[int] = []

    def call():
        made.append(1)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return call, made


# --------------------------------------------------------------------------- #
# The waiter
# --------------------------------------------------------------------------- #
def test_a_transient_refusal_is_waited_out_and_the_call_is_made_again():
    t = FakeTime()
    call, made = _calls(_refused(BURST), _refused(BURST), SERVED)
    waiter = agent_loop.BudgetWaiter(sleep=t.sleep, clock=t.clock)

    result, exhausted = waiter.invoke(call)

    assert result == SERVED and exhausted is None
    assert len(made) == 3, "every attempt goes back through the governor"
    assert sum(t.slept) == 40, "waited retry_after_seconds before each retry"
    assert max(t.slept) <= agent_loop.BUDGET_WAIT_SLICE_SECONDS, "slept in short slices"


def test_past_the_bound_the_call_says_the_budget_is_busy():
    t = FakeTime()
    call, made = _calls(_refused(BURST))
    waiter = agent_loop.BudgetWaiter(sleep=t.sleep, clock=t.clock)

    result, exhausted = waiter.invoke(call)

    assert exhausted is not None and exhausted["refusal"] == "burst"
    assert result["status"] == agent_loop.BUDGET_WAIT
    assert result["message"] == agent_loop.BUDGET_BUSY_MESSAGE
    assert sum(t.slept) <= agent_loop.TOOL_BUDGET_WAIT_SECONDS
    assert len(made) > 1


def test_the_run_wide_bound_and_a_deadline_are_respected():
    t = FakeTime()
    waiter = agent_loop.BudgetWaiter(sleep=t.sleep, clock=t.clock, per_run=30)
    _, exhausted = waiter.invoke(_calls(_refused(BURST))[0])
    assert exhausted is not None and waiter.waited <= 30
    # A second call on the same run has no room left: it does not wait at all.
    before = sum(t.slept)
    _, exhausted = waiter.invoke(_calls(_refused(BURST))[0])
    assert exhausted is not None and sum(t.slept) == before

    t = FakeTime()
    waiter = agent_loop.BudgetWaiter(sleep=t.sleep, clock=t.clock, deadline=t.now + 10)
    _, exhausted = waiter.invoke(_calls(_refused(BURST))[0])
    assert exhausted is not None and t.slept == [], "a wait past the deadline is not started"


def test_a_stop_request_ends_the_wait():
    t = FakeTime()
    waiter = agent_loop.BudgetWaiter(sleep=t.sleep, clock=t.clock,
                                     should_stop=lambda: bool(t.slept))
    _, exhausted = waiter.invoke(_calls(_refused(BURST), SERVED)[0])
    assert exhausted is not None and len(t.slept) == 1


def test_a_refusal_a_person_must_lift_is_returned_untouched():
    t = FakeTime()
    call, made = _calls(_refused(CAP))
    result, exhausted = agent_loop.BudgetWaiter(sleep=t.sleep, clock=t.clock).invoke(call)
    assert exhausted is None and result == _refused(CAP)
    assert len(made) == 1 and t.slept == []


def test_the_loop_and_the_step_agree_on_the_word():
    assert agent_loop.BUDGET_WAIT == agent_steps.BUDGET_WAIT


def test_the_burst_window_is_measured_on_the_wall_clock():
    """A tick runs inside one transaction, whose `now()` stands still: measured with it, the
    minute never moved for a caller that waited and asked again."""
    from boss_engine.services import budget

    # The window lives in burst_window() since the cap measures actual spend (settled receipts).
    source = inspect.getsource(budget)
    window = source[source.index("def burst_window"):]
    window = window[:window.index("\ndef ", 1) if "\ndef " in window[1:] else len(window)]
    assert "clock_timestamp()" in window
    assert "now()" not in window


# --------------------------------------------------------------------------- #
# The loop, exercised
# --------------------------------------------------------------------------- #
class _CallsTheSkillThenStops:
    """A model that calls its one skill, then reports what it saw."""

    def __init__(self):
        self.turns = 0
        self.tool_results: list[str] = []

    def create_chat_completion(self, messages, tools=None, **kwargs):
        self.turns += 1
        self.tool_results = [m.get("content") or "" for m in messages if m.get("role") == "tool"]
        if self.turns == 1:
            name = (tools[0].get("function") or {}).get("name")
            raw = {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "call_1", "type": "function",
                                   "function": {"name": name, "arguments": "{}"}}]}
            return ({"role": "assistant", "content": "", "raw": raw,
                     "tool_calls": [{"id": "call_1", "name": name, "arguments": "{}"}]},
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        said = '{"outcome": "completed", "reason": "fetched"}'
        return ({"role": "assistant", "content": said,
                 "raw": {"role": "assistant", "content": said}, "tool_calls": []},
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        yield connection


def _one_skill_agent(db):
    from boss_engine.services.skill_definitions import assign_skill_to_role, assignable_catalogue

    db.execute(text("DELETE FROM role_skills WHERE project_id=:p AND role_key='qa'"),
               {"p": PROJECT})
    one = assignable_catalogue(db, PROJECT)[0]["skill_id"]
    assign_skill_to_role(db, project_id=PROJECT, actor="test", role_key="qa", skill_id=one)


def _scripted(monkeypatch, *results):
    t = FakeTime()
    monkeypatch.setattr(agent_loop, "_sleep", t.sleep)
    monkeypatch.setattr(agent_loop, "_clock", t.clock)
    call, made = _calls(*results)
    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", lambda *_a, **_k: call())
    model = _CallsTheSkillThenStops()
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: model)
    return model, made, t


@needs_db
def test_the_agent_never_sees_a_refusal_that_cleared(db, monkeypatch):
    _one_skill_agent(db)
    model, made, t = _scripted(monkeypatch, _refused(BURST), SERVED)

    out = agent_steps.run(db, Settings(), PROJECT, instructions="Fetch it.", role_key="qa",
                          actor="test")

    assert out["status"] == "completed"
    assert len(made) == 2 and sum(t.slept) == 20
    assert len(model.tool_results) == 1
    seen = json.loads(model.tool_results[0])
    assert seen["status"] == "succeeded", seen
    assert "budget" not in model.tool_results[0].lower()


@needs_db
def test_past_the_bound_the_step_stops_to_wait_and_the_agent_does_not_judge_it(db, monkeypatch):
    _one_skill_agent(db)
    model, made, _ = _scripted(monkeypatch, _refused(BURST))

    out = agent_steps.run(db, Settings(), PROJECT, instructions="Fetch it.", role_key="qa",
                          actor="test")

    assert out["status"] == agent_steps.BUDGET_WAIT
    assert out["retry_after_seconds"] == 20 and out["budget"]["refusal"] == "burst"
    assert model.turns == 1, "the agent was handed the refusal to judge"
    status = db.execute(text("SELECT status FROM agent_runs WHERE id=:i"),
                        {"i": out["agent_run_id"]}).scalar()
    assert status == agent_loop.BUDGET_WAIT


@needs_db
def test_a_refusal_a_person_must_lift_reaches_the_agent_as_before(db, monkeypatch):
    _one_skill_agent(db)
    model, made, t = _scripted(monkeypatch, _refused(CAP))

    out = agent_steps.run(db, Settings(), PROJECT, instructions="Fetch it.", role_key="qa",
                          actor="test")

    assert out["status"] == "completed", "the loop ran on; the agent decides"
    assert len(made) == 1 and t.slept == []
    seen = json.loads(model.tool_results[0])
    assert seen["status"] == "denied" and seen["budget"]["refusal"] == "cap_exceeded"
