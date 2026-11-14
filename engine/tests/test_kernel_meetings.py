"""MEETING SEATS THROUGH THE GOVERNED PIPELINE (AP-P1b; design §2.3 "Meetings bypass the
pipeline", §4.2 the `meeting_seat` preset).

Each panel seat's turn, the facilitator's synthesis and the PRD draft called
`adapter.create_text_response` raw (meeting_service._ask): no budget reservation, no receipt, no
retry. With BOSS_KERNEL_MEETINGS on (the default) each is one `kernel.run` with
presets.meeting_seat over a TextEngine — the SAME call on the same adapter — so the kernel's
mandatory governance is around it. The shadow comparison holds the two paths equal on what the
adapter is asked and what the seat says and costs; the intended differences (a provider hiccup
retried; the budget gate reserving, settling and refusing) are pinned.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import meeting_service as ms

SYSTEM = "You are the QA lead on this panel."
PROMPT = "DIRECTIVE: build a status page.\nAs the QA lead, give your initial perspective."


class Adapter:
    """A seat's model: records every call, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers) or ["I would test the outage banner first."]
        self.calls: list = []

    def create_text_response(self, user_prompt, system_prompt=None):
        self.calls.append((user_prompt, system_prompt))
        answer = self.answers.pop(0) if self.answers else "(no more)"
        if isinstance(answer, BaseException):
            raise answer
        return "ref_1", answer, {"model": "gpt-test", "prompt_tokens": 1200,
                                 "completion_tokens": 300, "total_tokens": 1500}


class Gate:
    def __init__(self, refuse=None):
        self.log: list = []
        self.refuse = refuse

    def open(self):
        if self.refuse is not None:
            raise self.refuse
        self.log.append("open")
        return "held"

    def settle(self, held, usage):
        self.log.append(("settle", usage.get("prompt_tokens")))

    def fail(self, held, error):
        self.log.append(("fail", type(error).__name__))


def test_the_flag_is_on_by_default(monkeypatch):
    monkeypatch.delenv("BOSS_KERNEL_MEETINGS", raising=False)
    assert ms.kernel_meetings()
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "0")
    assert not ms.kernel_meetings()


def test_shadow_the_seat_is_asked_the_same_and_says_and_costs_the_same(monkeypatch):
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "0")
    old = Adapter("Ship the banner with a test.")
    before = ms._ask(old, SYSTEM, PROMPT)
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    new = Adapter("Ship the banner with a test.")
    after = ms._ask(new, SYSTEM, PROMPT)
    assert new.calls == old.calls == [(PROMPT, SYSTEM)], "the adapter was asked differently"
    assert after == before
    text, cost, model = after
    assert text == "Ship the banner with a test." and model == "gpt-test" and cost > 0


def test_on_the_kernel_the_seat_is_reserved_and_settled_by_the_budget_gate(monkeypatch):
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    gate = Gate()
    text, _cost, _model = ms._ask(ms.Seat(Adapter("ok"), gate=gate, project_id="p"),
                                  SYSTEM, PROMPT)
    assert text == "ok" and gate.log == ["open", ("settle", 1200)]


def test_a_refused_budget_ends_the_seat_saying_so_and_the_model_is_never_asked(monkeypatch):
    from boss_engine.services.model_routing import ModelSpendRefused

    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    adapter = Adapter("never")
    with pytest.raises(ModelSpendRefused):
        ms._ask(ms.Seat(adapter, gate=Gate(refuse=ModelSpendRefused("cap reached")),
                        project_id="p"), SYSTEM, PROMPT)
    assert adapter.calls == []


def test_a_provider_hiccup_is_retried_where_it_used_to_end_the_phase(monkeypatch):
    from boss_engine.agent import transient

    monkeypatch.setattr(transient.time, "sleep", lambda s: None)
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    gate = Gate()
    adapter = Adapter(RuntimeError("529 overloaded_error"), "Second time lucky.")
    text, _cost, _model = ms._ask(ms.Seat(adapter, gate=gate), SYSTEM, PROMPT)
    assert text == "Second time lucky." and len(adapter.calls) == 2
    assert gate.log == ["open", ("fail", "RuntimeError"), "open", ("settle", 1200)]


def test_the_adapters_own_failure_is_raised_as_it_was(monkeypatch):
    from boss_engine.services.openai_adapter import IntegrationConfigurationError

    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    adapter = Adapter(IntegrationConfigurationError("OPENAI_API_KEY is not configured."))
    with pytest.raises(IntegrationConfigurationError, match="OPENAI_API_KEY"):
        ms._ask(adapter, SYSTEM, PROMPT)


def test_a_seat_the_router_refused_still_says_why_in_the_minutes(monkeypatch):
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    refused = ms._RefusedSeat("QA lead", "its agent is Local-only and no local model runs")
    seat = ms._seat(None, None, "p", "mrun_1", refused)
    assert seat.gate is None  # nothing to reserve: it never calls a model
    text, cost, _model = ms._ask(seat, SYSTEM, PROMPT)
    assert "did not speak" in text and cost == 0


def test_a_panel_speaks_seat_by_seat_through_the_kernel(monkeypatch):
    monkeypatch.setenv("BOSS_KERNEL_MEETINGS", "1")
    shared, own = Adapter("shared view", "shared view"), Adapter("my own model's view")
    gates = {"shared": Gate(), "own": Gate()}
    personas = [{"key": "pm", "title": "Product", "lens": "value"},
                {"key": "qa", "title": "QA", "lens": "risk"},
                {"key": "arch", "title": "Architect", "lens": "structure"}]
    out, cost, _model = ms._ask_panel(
        ms.Seat(shared, gate=gates["shared"]), personas,
        make_prompt=lambda p: f"As the {p['title']}...",
        seats={"qa": ms.Seat(own, gate=gates["own"])})
    assert out == {"Product": "shared view", "QA": "my own model's view",
                   "Architect": "shared view"}
    assert len(gates["shared"].log) == 4 and len(gates["own"].log) == 2
    assert cost > 0


def test_every_seat_of_a_phase_is_wrapped_in_its_governance():
    source = inspect.getsource(ms.advance_meeting_run)
    assert "adapter = _seat(c, settings, run[\"project_id\"], run_id, adapter)" in source
    assert "_seat(c, settings, run[\"project_id\"], run_id, seat_adapter)" in source
    assert "presets.meeting_seat()" in inspect.getsource(ms._ask)
