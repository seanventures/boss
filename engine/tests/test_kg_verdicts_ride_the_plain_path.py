"""KG and consultant verdicts ride the plain completion path (BOSSAPP-1005, audit 997 #5).

A single-turn JSON judgement — a KG role verdict, a rulebook extraction, the regulatory-trace
judge, a rule scan — used to run a FULL agentic CLI session (fresh process, ~34k tokens of
coding preamble and tool schemas, an empty temp dir) to return one JSON object. The guard here:

  * on a plain-capable engine these calls use `run_plain` (engine_contract.run_verdict) and
    never start an agentic session;
  * an engine with no plain path — including every test fake that only implements run_role —
    keeps the old route verbatim: a read-only run_role in an empty temporary directory;
  * a plain answer that failed only because the model reached for a tool falls back to the
    old route instead of failing the step;
  * the result schema the call sites parse (summary/usage/cost) is the same on both routes.

No real model is ever called: fakes and the scripted conformance engine only.
"""
from __future__ import annotations

from pathlib import Path

import pytest


JUDGEMENT = '=== JUDGEMENT ===\n{"finding": "ok", "confidence": 0.9}\n=== END JUDGEMENT ==='


class PlainCapableAgent:
    """A fake engine with a plain completion path; an agentic session here is the regression."""

    engine = "fake-plain"

    def __init__(self, summary: str = JUDGEMENT):
        self.summary = summary
        self.plain_calls: list[str] = []

    @property
    def capabilities(self):
        from boss_engine.services.engine_contract import EngineCapabilities

        return EngineCapabilities(reports_cost=True, plain_completion=True)

    def run_plain(self, prompt: str, *, model=None):
        self.plain_calls.append(prompt)
        return {"summary": self.summary, "usage": {"model": "fake-1", "prompt_tokens": 10,
                                                   "completion_tokens": 5},
                "cost_usd_micros": 7, "turns": 1, "model": "fake-1", "stop_reason": "done"}

    def run_role(self, prompt, cwd, **kw):  # pragma: no cover — reaching it IS the failure
        raise AssertionError("a single-turn verdict started an agentic session "
                             "on a plain-capable engine")


class RoleOnlyAgent:
    """A fake engine with no plain path: the old route must still work, unchanged."""

    engine = "fake-role-only"

    def __init__(self, summary: str = JUDGEMENT):
        self.summary = summary
        self.role_calls: list[dict] = []

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        self.role_calls.append({"prompt": prompt, "cwd": Path(cwd), "read_only": read_only,
                                "max_turns": max_turns,
                                "cwd_entries": sorted(p.name for p in Path(cwd).iterdir())})
        return {"summary": self.summary, "usage": {"model": "fake-2"}, "cost_usd_micros": 3}


def test_a_verdict_on_a_plain_capable_engine_never_starts_an_agentic_session():
    from boss_engine.services.engine_contract import contracted, run_verdict

    agent = contracted(PlainCapableAgent())
    out = run_verdict(agent, "judge this", max_turns=4)
    assert out["transport"] == "plain"
    assert out["summary"] == JUDGEMENT
    assert out["usage"]["model"] == "fake-1"
    assert out["cost_usd_micros"] == 7
    assert not out["cost_unreported"]


def test_an_engine_without_a_plain_path_keeps_the_old_route_verbatim():
    from boss_engine.services.engine_contract import contracted, run_verdict

    inner = RoleOnlyAgent()
    out = run_verdict(contracted(inner), "judge this", max_turns=4, temp_prefix="boss-t1005-")
    assert out["transport"] == "agentic"
    assert out["summary"] == JUDGEMENT
    call = inner.role_calls[0]
    assert call["read_only"] is True and call["max_turns"] == 4
    assert call["cwd_entries"] == []                       # the empty temp dir, as before
    assert call["cwd"].name.startswith("boss-t1005-")
    assert not call["cwd"].exists()                        # and it is cleaned up


def test_a_bare_fake_with_only_run_role_still_works():
    """Every existing pipeline test fake implements run_role alone; the verdict path must
    treat it exactly like an engine with no plain completion."""
    from boss_engine.services.engine_contract import run_verdict

    inner = RoleOnlyAgent()
    out = run_verdict(inner, "judge this", max_turns=2)
    assert out["transport"] == "agentic" and inner.role_calls


def test_a_plain_answer_that_reached_for_a_tool_falls_back_to_the_agentic_route():
    from boss_engine.services.engine_contract import run_verdict
    from boss_engine.services.model_failures import NO_DIRECT_ANSWER

    class ToolReacher(PlainCapableAgent):
        def run_plain(self, prompt, *, model=None):
            raise RuntimeError(f"the model {NO_DIRECT_ANSWER} (took 2 turns)")

        def run_role(self, prompt, cwd, **kw):
            return {"summary": JUDGEMENT, "usage": {"model": "fake-1"}}

    out = run_verdict(ToolReacher(), "judge this")
    assert out["transport"] == "agentic" and out["summary"] == JUDGEMENT


def test_any_other_plain_failure_still_raises():
    from boss_engine.services.engine_contract import run_verdict

    class Broken(PlainCapableAgent):
        def run_plain(self, prompt, *, model=None):
            raise RuntimeError("claude CLI exited 1: boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_verdict(Broken(), "judge this")


def test_the_role_judgements_parse_the_plain_answer_exactly_as_before():
    """The shared KG role ask (`banking_regulatory_pipeline._ask_role`, reused verbatim by the
    AML and DQ-incident pipelines) and the rulebook extractor both ride the plain path and
    return the same parsed judgement they always did."""
    from boss_engine.services.knowledge import banking_regulatory_pipeline as shared
    from boss_engine.services.knowledge import rulebook_extract

    agent = PlainCapableAgent()
    parsed = shared._ask_role(agent, "ROLE PROMPT + SITUATION")
    assert parsed == {"finding": "ok", "confidence": 0.9}
    assert agent.plain_calls == ["ROLE PROMPT + SITUATION"]

    agent = PlainCapableAgent()
    parsed, cost = rulebook_extract._ask(agent, "CLAUSES")
    assert parsed == {"finding": "ok", "confidence": 0.9}
    assert cost["usage"]["model"] == "fake-1"
    assert cost["cost_unreported"] is False


def test_an_unreadable_plain_answer_still_gets_the_one_followup_ask():
    from boss_engine.services.knowledge import banking_regulatory_pipeline as shared

    class SlowToAnswer(PlainCapableAgent):
        def run_plain(self, prompt, *, model=None):
            out = super().run_plain(prompt, model=model)
            if len(self.plain_calls) == 1:
                out["summary"] = "I read the sections carefully and found three rules."
            return out

    agent = SlowToAnswer()
    parsed = shared._ask_role(agent, "ROLE PROMPT")
    assert parsed == {"finding": "ok", "confidence": 0.9}
    assert len(agent.plain_calls) == 2
    assert "Answer NOW" in agent.plain_calls[1]


def test_the_trace_judge_rides_the_plain_path_and_keeps_its_contract():
    from boss_engine.services.knowledge import regulatory_trace

    agent = PlainCapableAgent(
        '=== JUDGEMENT ===\n{"choice": 0, "rationale": "implements it", "confidence": 0.8}\n'
        "=== END JUDGEMENT ===")
    judged = regulatory_trace._judge(agent, "the clause",
                                     [{"kind": "rule", "name": "candidate A", "detail": ""}])
    assert judged["index"] == 0 and judged["confidence"] == 0.8
    assert judged["usage"]["model"] == "fake-1"
    assert agent.plain_calls and "candidate A" in agent.plain_calls[0]


def test_the_scripted_conformance_engine_answers_a_verdict_plainly_when_capable():
    """The same assertion on a real registered engine, no model behind it: with the
    plain_completion capability the verdict call reaches run_plain (the run state records
    transport="plain"); without it, the old agentic run_role route."""
    from boss_engine.services import scripted_engine
    from boss_engine.services.engine_contract import contracted, run_verdict

    scripted_engine.reset()
    plain = contracted(scripted_engine.ScriptedEngine(caps={"plain_completion",
                                                            "reports_cost"}))
    out = run_verdict(plain, "judge this inlined context")
    assert out["transport"] == "plain"
    assert out["summary"].strip()
    calls = scripted_engine.run_state(None).calls
    assert calls and calls[-1]["transport"] == "plain"

    scripted_engine.reset()
    poorest = contracted(scripted_engine.ScriptedEngine(caps=set()))
    out = run_verdict(poorest, "judge this inlined context")
    assert out["transport"] == "agentic"
    calls = scripted_engine.run_state(None).calls
    assert calls and "transport" not in calls[-1]
    scripted_engine.reset()


def test_the_rule_scan_ask_goes_through_the_verdict_path():
    """rule_scan's model call sits inside run_scan (DB-bound), so the guard here is on the
    seam itself: the module asks engine_contract.run_verdict, not agent.run_role directly."""
    import inspect

    from boss_engine.services.knowledge import rule_scan

    src = inspect.getsource(rule_scan)
    assert "run_verdict(agent, prompt" in src
    assert "agent.run_role(" not in src
