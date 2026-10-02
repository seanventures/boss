"""A chat turn ends inside its time — with what it found and what to do next — never at 240 s.

THE COMPLAINT (the owner, 2026-10-02): "I don't know why it takes so long to find the issue and
come up with the recommendation to fix … it really times out after spending 240s there? very
annoying". What was true on milestone/integration:

* the turn's 180 s budget (turn_budget.py) was consulted only BETWEEN model calls; the call itself
  ran on the CLI's own 240 s timeout, so a call started late ran far past the turn;
* a CLI timeout read as "timed out" — so `_with_transient_retry` ran the call again, up to three
  times, and FallingBackAdapter read "timed out after" as "unavailable" and asked a second brain
  the same question from the start;
* the console's stall line is 240 s, so any slow turn ended in "Still working — past 240s", and
  everything the turn had found by then was lost with it.

These pin the repair (services/call_deadline.py): every model call is bounded by the turn's
remaining time; a call the TURN cut off is neither retried nor handed to a fallback; the turn's
hard end sits inside the console's stall line; and a turn that runs out answers from its trace —
the findings, what it checked, and the fix as a button.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time

import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.agent.transient import _with_transient_retry
from boss_engine.config import Settings
from boss_engine.services import call_deadline
from boss_engine.services.call_deadline import TurnDeadlineExceeded, bounded_timeout, until
from boss_engine.services.claude_cli_adapter import FallingBackAdapter
from boss_engine.services.turn_budget import TurnBudget
from boss_engine.services.turn_subject import SubjectRef, subject_named_in

REPO = pathlib.Path(__file__).resolve().parents[2]
CONSOLE_STALL_LINES = {
    "apps/web/app/agent-ui.tsx": r"const AGENT_STALL_SECONDS = (\d+);",
    "apps/web/app/ask-boss-hero.tsx": r"const STALL_SECONDS = (\d+);",
}
#: Time the API and the console's poll need around the turn itself.
MARGIN_SECONDS = 10


def test_the_turns_hard_end_is_inside_every_console_stall_line():
    """The configured turn plus its answer window ends before any console door gives up."""
    hard_end = Settings().chat_turn_seconds + TurnBudget().answer_seconds
    for path, pattern in CONSOLE_STALL_LINES.items():
        match = re.search(pattern, (REPO / path).read_text(encoding="utf-8"))
        assert match, f"{path} no longer declares its stall line; this test would pass vacuously"
        stall = int(match.group(1))
        assert hard_end + MARGIN_SECONDS <= stall, (
            f"a turn may run {hard_end}s but {path} gives up at {stall}s")


def test_a_hung_cli_call_is_cut_at_the_turns_deadline_not_its_own_240s(tmp_path, monkeypatch):
    """The real adapter, a CLI that never answers: cut at the turn's time, as a deadline."""
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

    hung = tmp_path / "claude"
    hung.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    hung.chmod(0o755)
    adapter = ClaudeCliAdapter.__new__(ClaudeCliAdapter)
    adapter.settings = Settings().model_copy(update={"claude_cli_timeout": 240,
                                                     "repo_root": tmp_path})
    adapter.model_override = None
    monkeypatch.setattr(adapter, "_claude_bin", lambda: str(hung), raising=False)
    monkeypatch.setattr(adapter, "_child_env", lambda: {}, raising=False)
    monkeypatch.setattr(call_deadline, "MIN_CALL_SECONDS", 0.5)
    started = time.monotonic()
    with until(time.monotonic() + 2.0):
        with pytest.raises(TurnDeadlineExceeded):
            adapter._invoke("why is it stuck?")
    assert time.monotonic() - started < 10, "the call ran on the CLI's own timeout"


def test_a_call_the_turn_cut_off_is_neither_retried_nor_handed_to_a_fallback():
    asked: list[str] = []

    class Cut:
        def create_chat_completion(self, messages, tools=None, **kw):
            asked.append("primary")
            # Worded like the CLI's own timeout on purpose: the TYPE decides, not the words
            # both retry and fallback used to match on.
            raise TurnDeadlineExceeded("claude CLI timed out after 2s: the turn's time ran out")

    class Fallback:
        def create_chat_completion(self, messages, tools=None, **kw):
            asked.append("fallback")
            return ({"content": "late"}, {})

    with pytest.raises(TurnDeadlineExceeded):
        _with_transient_retry(
            lambda: FallingBackAdapter(Cut(), Fallback()).create_chat_completion([]))
    assert asked == ["primary"], asked


def test_a_backend_that_is_really_down_still_falls_back():
    """Keep what worked: the CLI's OWN timeout outside any turn is still 'unavailable'."""
    class Down:
        def create_chat_completion(self, messages, tools=None, **kw):
            raise RuntimeError("claude CLI timed out after 240s")

    class Fine:
        def create_chat_completion(self, messages, tools=None, **kw):
            return ({"content": "answered"}, {})

    assert FallingBackAdapter(Down(), Fine()).create_chat_completion([])[0]["content"] \
        == "answered"


def test_no_call_starts_with_too_little_time_left(monkeypatch):
    monkeypatch.setattr(call_deadline, "MIN_CALL_SECONDS", 5.0)
    with until(time.monotonic() + 1.0):
        with pytest.raises(TurnDeadlineExceeded):
            bounded_timeout(240)
    with until(time.monotonic() + 100.0):
        assert 90 < bounded_timeout(240) <= 100
        with until(time.monotonic() + 500.0):
            assert bounded_timeout(240) <= 100, "an inner pass widened the turn's deadline"
    assert bounded_timeout(240) == 240


# ------------------------------------------------------------------------------------------- #
# The stuck-run diagnosis, end to end through the agent, against a model that never finishes. #
# ------------------------------------------------------------------------------------------- #
DIAGNOSIS = {
    "ok": True, "run_id": "crun_stuck1", "status": "running",
    "findings": ["NO TURN RECORDED FOR 14 MINUTES while the run is still 'running'. This is "
                 "not slow work — it has gone quiet.",
                 "1 tool call(s) never produced a result. The last one is where it stopped."],
    "resolution": {"tool": "cancel_code_task", "args": {"run_id": "crun_stuck1"},
                   "label": "Stop Build the inbox",
                   "why": "it has produced nothing for 14 minutes; stopping it frees the slot"},
}


class _ThinksForever:
    """Asks for the diagnosis, then never finishes thinking — every later call runs until the
    turn's deadline cuts it, exactly as a hung CLI call does."""

    def __init__(self):
        self.calls = 0

    def create_chat_completion(self, messages, tools=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return ({"raw": {"role": "assistant", "content": ""}, "content": "",
                     "tool_calls": [{"id": "c1", "name": "diagnose_run",
                                     "arguments": json.dumps({"run_id": "crun_stuck1"})}]}, {})
        time.sleep(bounded_timeout(240))
        raise TurnDeadlineExceeded("the turn's time ran out while the model was still working")


def _agent(adapter, ran: list):
    agent = EngineAgent.__new__(EngineAgent)
    agent._adapter = adapter
    agent._notes = []
    agent.ambient = {}
    agent.registry = type("R", (), {"openai_schema": lambda self: [
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("diagnose_run", "read_logs", "propose_action")]})()

    def run_tool(name, args, **kw):
        ran.append(name)
        if name == "diagnose_run":
            return DIAGNOSIS
        if name == "propose_action":
            return {"ok": True, "proposed": True, "tool": args["tool"], "args": args["args"],
                    "label": args["label"]}
        return {"ok": True}

    agent._run_tool = run_tool
    return agent


def test_a_stuck_run_diagnosis_returns_within_budget_with_its_recommendation(monkeypatch):
    """The whole complaint in one test: a turn about a stuck build, a model that will not finish,
    and a budget. The turn ends within its seconds plus its answer window, and the operator gets
    what diagnose_run found, what was checked, and the fix as a button."""
    monkeypatch.setattr(call_deadline, "MIN_CALL_SECONDS", 0.2)
    adapter = _ThinksForever()
    ran: list[str] = []
    # The reserve scaled to the test's seconds, as 20 s is to a real turn's 180.
    budget = TurnBudget(seconds=1.5, answer_seconds=1.0, answer_reserve=0.1, model_calls=16)
    started = time.monotonic()
    turn = _agent(adapter, ran).chat(project_id="p", message="why is crun_stuck1 stuck?",
                                     max_steps=14, budget=budget)
    took = time.monotonic() - started
    assert took <= budget.seconds + budget.answer_seconds + 0.5, f"the turn ran {took:.1f}s"
    assert adapter.calls == 3, "a cut-off call was retried, or the answer step never came"
    assert "NO TURN RECORDED FOR 14 MINUTES" in turn.reply
    assert "What I checked" in turn.reply and "diagnose_run" in turn.reply
    assert "Next: Stop Build the inbox" in turn.reply
    offered = [s for s in turn.trace if s["tool"] == "propose_action"]
    assert offered and offered[0]["args"]["tool"] == "cancel_code_task"
    assert ran == ["diagnose_run", "propose_action"]


# ------------------------------------------------------------------------------------------- #
# A question that names its run is answered from its record in the first call.               #
# ------------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("question,ref", [
    ("why did crun_3e370c114b95425d8a7c fail?", SubjectRef("code_task", "crun_3e370c114b95425d8a7c")),
    ("what happened to wfr_9c78b289633348c68246, it's stuck",
     SubjectRef("workflow_run", "wfr_9c78b289633348c68246")),
    ("is crun_3e370c114b95425d8a7c slower than crun_aaaaaaaa1111?", None),
    ("why is the build stuck?", None),
])
def test_an_id_in_the_question_is_the_subject(question, ref):
    assert subject_named_in(question) == ref
