"""K2 (BOSSAPP-961): a decision's READ PHASE and final choice run on the harness kernel.

Behind BOSS_KERNEL_DECISIONS (default ON since AP-P1b; "0" turns it off): `agent_decision.
decide` with a ReadPhase runs `kernel.run` with presets.decision, a ReadOnlyToolSet over the caller's read tools, and the
evidence/citation acceptance (`_accept` + the caller's check) as the VerifyPolicy — one re-ask,
exactly as today. Flag off is byte-identical to `_ask_reading`; flag on is held equal by
test_kernel_conformance.py plus the checks here at the `decide` seam:

  * the same script decides the same way on both paths, asking the model the same things;
  * a check that refuses uncited evidence draws the one re-ask, in the same words;
  * NO_MODEL stays an answer, never a crash (a fatal model error is a result);
  * a budget deferral is an error result in `_ask_reading`'s own words, not a wait;
  * the read-phase turn cap answers on the last turn, without tools;
  * when `decide` routed the model itself, every kernel call is reserved and settled through
    the SAME project gate (`_routed`'s GovernedChatAdapter, as kernel/governance.AdapterGate).

Jev, candidates, the confidence bar and the no-model reply stay caller-side (unchanged by the
flag): test_an_agent_decides_with_a_model.py and test_jev_decides_structured_and_lifted.py run
green with the flag on and off.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from boss_engine.kernel.scripted import Call, Fail, Say, ScriptedEngine
from boss_engine.services import agent_decision as ad

SETTINGS = SimpleNamespace()

READ_SCHEMA = {"type": "object", "properties": {"run": {"type": "string"}},
               "required": ["run"], "additionalProperties": False}
READ_TOOLS = [{"type": "function", "function": {"name": "read_logs",
                                                "description": "Read a run's logs.",
                                                "parameters": READ_SCHEMA}}]
CANDIDATES = [ad.Candidate("fix", "fix the failing step"),
              ad.Candidate("ask", "ask the person", acts=False)]

FIX = json.dumps({"action": "fix", "confidence": 0.9, "reason": "the logs say so",
                  "evidence": ["run r1: 3 errors"]})
MADE_UP = json.dumps({"action": "fix", "confidence": 0.9, "reason": "trust me",
                      "evidence": ["a log line nobody was shown"]})


def read_logs(name, args):
    return {"ok": True, "tool": "read_logs", "lines": [f"run {args.get('run')}: 3 errors"]}


def read_phase(max_turns=4, **kw):
    return ad.ReadPhase(tools=list(READ_TOOLS), run=read_logs, max_turns=max_turns, **kw)


def flag(monkeypatch, on: bool) -> None:
    monkeypatch.setenv(ad.KERNEL_FLAG, "1" if on else "0")


def decide(adapter, *, read=None, check=None, **kw):
    return ad.decide(None, SETTINGS, "p", situation="a failed step", message="why did it fail?",
                     candidates=CANDIDATES, adapter=adapter, read=read, check=check, **kw)


def same_requests(old: ScriptedEngine, new: ScriptedEngine) -> None:
    assert len(old.requests) == len(new.requests), (len(old.requests), len(new.requests))
    for n, (a, b) in enumerate(zip(old.requests, new.requests)):
        assert a["tools"] == b["tools"], (n, a["tools"], b["tools"])
        assert [m.get("role") for m in a["messages"]] == \
            [m.get("role") for m in b["messages"]], n
        for i, (ma, mb) in enumerate(zip(a["messages"], b["messages"])):
            assert ma.get("content") == mb.get("content"), (n, i)


class TestTheFlag:
    def test_off_is_the_old_loop_and_on_is_the_kernel_deciding_the_same(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Say(FIX)]
        flag(monkeypatch, False)
        old = ScriptedEngine(list(script))
        before = decide(old, read=read_phase())

        flag(monkeypatch, True)
        monkeypatch.setattr(ad, "_ask_reading", _never_the_old_loop)
        new = ScriptedEngine(list(script))
        after = decide(new, read=read_phase())

        same_requests(old, new)
        assert before.decided and after.decided
        assert before.to_dict() == after.to_dict()
        assert after.action == "fix" and after.turns == 2
        assert after.evidence == ["run r1: 3 errors"]

    def test_the_default_is_on_and_an_off_word_turns_it_off(self, monkeypatch):
        # AP-P1b (D3): decisions flipped on after parity; "0" is the escape hatch, and a blank
        # value in an .env is the default, never a silent switch back to the old loop.
        monkeypatch.delenv(ad.KERNEL_FLAG, raising=False)
        assert ad.kernel_decisions()
        monkeypatch.setenv(ad.KERNEL_FLAG, "")
        assert ad.kernel_decisions()
        for word in ("0", "false", "no", "off", "OFF"):
            monkeypatch.setenv(ad.KERNEL_FLAG, word)
            assert not ad.kernel_decisions(), word
        monkeypatch.setenv(ad.KERNEL_FLAG, "1")
        assert ad.kernel_decisions()


def _never_the_old_loop(*_a, **_k):
    raise AssertionError("BOSS_KERNEL_DECISIONS is on: _ask_reading must not run")


class TestTheEvidenceReask:
    def check(self, answer, corpus):
        missing = [e for e in (answer.get("evidence") or []) if str(e) not in corpus]
        return f"evidence not shown to you: {missing[0]}" if missing else ""

    def test_uncited_evidence_is_refused_once_and_the_reask_is_the_same_words(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Say(MADE_UP), Say(FIX)]
        flag(monkeypatch, False)
        old = ScriptedEngine(list(script))
        before = decide(old, read=read_phase(), check=self.check)
        flag(monkeypatch, True)
        new = ScriptedEngine(list(script))
        after = decide(new, read=read_phase(), check=self.check)
        same_requests(old, new)
        assert before.to_dict() == after.to_dict() and after.decided
        reask = new.requests[-1]["messages"][-1]["content"]
        assert reask.startswith("Refused: evidence not shown to you: ")
        assert reask.endswith("Answer again with ONLY the JSON object.")

    def test_twice_refused_decides_nothing_and_says_why(self, monkeypatch):
        script = [Say(MADE_UP), Say(MADE_UP), Say(FIX)]
        flag(monkeypatch, True)
        got = decide(ScriptedEngine(list(script)), read=read_phase(), check=self.check)
        assert got.status == ad.NO_MODEL and not got.decided
        assert "evidence not shown to you" in got.refused


class TestNoModel:
    def test_a_fatal_model_error_is_an_answer_not_a_crash(self, monkeypatch):
        flag(monkeypatch, True)
        got = decide(ScriptedEngine([Fail(RuntimeError("boom: nothing connected"))]),
                     read=read_phase())
        assert got.status == ad.NO_MODEL and not got.decided
        assert "boom" in got.error

    def test_routing_that_fails_is_still_no_model(self, monkeypatch):
        flag(monkeypatch, True)

        def no_route(*_a, **_k):
            raise RuntimeError("no model is connected")

        monkeypatch.setattr(ad, "_routed", no_route)
        got = ad.decide(None, SETTINGS, "p", situation="s", message="m",
                        candidates=CANDIDATES, read=read_phase())
        assert got.status == ad.NO_MODEL and "no model is connected" in got.error


class TestTheBudgetDeferral:
    def test_a_deferral_is_an_error_result_in_the_old_loops_words(self, monkeypatch):
        from boss_engine.services.model_routing import ModelSpendDeferred

        script = [Fail(ModelSpendDeferred("the per-minute budget is busy",
                                          retry_after_seconds=3))]
        flag(monkeypatch, False)
        before = decide(ScriptedEngine(list(script)), read=read_phase())
        flag(monkeypatch, True)
        after = decide(ScriptedEngine(list(script)), read=read_phase())
        assert before.status == after.status == ad.NO_MODEL
        assert before.error == after.error
        assert after.error.startswith("the model budget deferred the turn: ")
        assert "busy" in after.error


class TestTheTurnCap:
    def test_the_last_turn_answers_without_tools(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Say(FIX)]
        flag(monkeypatch, True)
        new = ScriptedEngine(list(script))
        got = decide(new, read=read_phase(max_turns=2))
        assert got.decided and got.turns == 2
        assert new.requests[0]["tools"] == ["read_logs"]
        assert new.requests[1]["tools"] is None
        assert new.requests[1]["messages"][-1]["content"] == ad.ANSWER_NOW

    def test_still_reading_at_the_cap_decides_nothing(self, monkeypatch):
        script = [Call("read_logs", {"run": "r1"}), Call("read_logs", {"run": "r2"}),
                  Say(FIX)]
        flag(monkeypatch, False)
        before = decide(ScriptedEngine(list(script)), read=read_phase(max_turns=2))
        flag(monkeypatch, True)
        after = decide(ScriptedEngine(list(script)), read=read_phase(max_turns=2))
        # The cap's last turn offers no tools; a scripted model that answers a tool call
        # anyway has not answered the question, on either path.
        assert before.status == after.status
        assert before.decided == after.decided


class _Gate:
    """A GovernedChatAdapter stood in for: the reserve/settle/release seam the kernel uses."""

    def __init__(self, inner):
        self._inner = inner
        self.opened = 0
        self.settled = 0
        self.released = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def reserve_turn(self):
        self.opened += 1
        return ("resv_1", "rcpt_1")

    def settle_turn(self, held, usage):
        self.settled += 1

    def release_turn(self, held, error):
        self.released += 1


class TestGovernance:
    def test_a_routed_decision_is_reserved_and_settled_through_the_project_gate(self,
                                                                                monkeypatch):
        flag(monkeypatch, True)
        raw = ScriptedEngine([Call("read_logs", {"run": "r1"}), Say(FIX)])
        governed = _Gate(raw)
        monkeypatch.setattr(ad, "_routed", lambda *_a, **_k: (governed, raw))
        got = ad.decide(None, SETTINGS, "p", situation="s", message="m",
                        candidates=CANDIDATES, read=read_phase())
        assert got.decided
        assert governed.opened == governed.settled == 2 and governed.released == 0

    def test_a_failed_call_releases_its_reservation(self, monkeypatch):
        flag(monkeypatch, True)
        raw = ScriptedEngine([Fail(RuntimeError("down for good"))])
        governed = _Gate(raw)
        monkeypatch.setattr(ad, "_routed", lambda *_a, **_k: (governed, raw))
        got = ad.decide(None, SETTINGS, "p", situation="s", message="m",
                        candidates=CANDIDATES, read=read_phase())
        assert got.status == ad.NO_MODEL
        assert governed.opened == governed.released and governed.released >= 1

    def test_a_callers_own_adapter_is_not_wrapped_in_a_second_gate(self, monkeypatch):
        """An adapter the caller passes keeps whatever governance it came with — the kernel adds
        no gate of its own (the caller's envelope rule, and today's behaviour)."""
        flag(monkeypatch, True)
        raw = ScriptedEngine([Say(FIX)])
        governed = _Gate(raw)
        got = decide(governed, read=read_phase())
        assert got.decided
        assert governed.opened == 0, "no AdapterGate was built around a caller's adapter"


@pytest.mark.parametrize("on", [False, True])
def test_the_whole_shape_survives_the_flag(monkeypatch, on):
    """One richer run, both ways: a read, a refused answer, the re-ask, the final choice —
    the AgentDecision a caller reads is the same object either way."""
    script = [Call("read_logs", {"run": "r1"}),
              Say(json.dumps({"action": "panic", "confidence": 0.9})), Say(FIX)]
    flag(monkeypatch, on)
    engine = ScriptedEngine(list(script))
    got = decide(engine, read=read_phase())
    assert got.decided and got.action == "fix"
    assert got.confidence == 0.9 and got.turns == 3
    assert got.evidence == ["run r1: 3 errors"]
    reask = engine.requests[-1]["messages"][-1]["content"]
    assert reask.startswith("Refused: 'panic' is not one of the candidate actions")
