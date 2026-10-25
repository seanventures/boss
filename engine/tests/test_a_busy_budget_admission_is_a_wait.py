"""A busy budget admission is a WAIT, never a cap (brec_6025901347b6445eb9c1).

Two business-rules scans started a second apart both stopped "budget refused" after one file
and no model call: the second hit "Budget admission is busy with another transaction" — a
transient lock collision — and GovernedChatAdapter._open raised it as ModelSpendRefused
"(budget cap …)", terminal. Now: the admission is retried briefly at the source; a transient
refusal that remains is ModelSpendDeferred (a wait the caller retries); the scan waits a few
deferrals out before it stops; and a workflow's declared inputs named at the top level of
start_workflow reach the run.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services import budget, model_routing
from boss_engine.services.knowledge import rule_scan


class _Locks:
    """pg_try_advisory_xact_lock answering from a script: False, False, …, True."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def execute(self, *_a, **_k):
        self.calls += 1
        value = self.answers.pop(0) if self.answers else False
        return SimpleNamespace(scalar_one=lambda: value)


def test_a_busy_admission_is_tried_again_before_it_is_called_busy(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    later = _Locks([False, False, True])
    assert budget._try_admit(later, "p") is True and later.calls == 3
    never = _Locks([])
    assert budget._try_admit(never, "p") is False
    assert never.calls == len(budget.ADMISSION_RETRY_PAUSES) + 1
    assert sum(budget.ADMISSION_RETRY_PAUSES) < 3, "a wait of seconds, never a blocking wait"


def test_a_transient_refusal_is_a_deferral_not_a_cap(monkeypatch):
    busy = {"status": "denied", "refusal": "admission_busy", "transient": True,
            "retry_after_seconds": 1, "message": budget.ADMISSION_BUSY_MESSAGE}
    monkeypatch.setattr(budget.BudgetGovernor, "reserve", lambda self, *a, **k: busy)
    adapter = model_routing.GovernedChatAdapter(
        None, object(), SimpleNamespace(), "p1", skill_id="s", ref="r",
        estimate_usd_micros=1, choice=model_routing.ModelChoice("native", "test"),
        category="knowledge")
    with pytest.raises(model_routing.ModelSpendDeferred) as deferred:
        adapter._open()
    assert "busy" in str(deferred.value) and "budget cap" not in str(deferred.value)
    assert deferred.value.retry_after_seconds == 1
    capped = {"status": "denied", "message": "Budget cap exceeded: today is used."}
    monkeypatch.setattr(budget.BudgetGovernor, "reserve", lambda self, *a, **k: capped)
    with pytest.raises(model_routing.ModelSpendRefused):
        adapter._open()


def _governor(opens):
    gov = rule_scan.Governor.__new__(rule_scan.Governor)
    gov._governed = SimpleNamespace(_open=lambda: opens.pop(0)())
    return gov


def _deferred():
    raise model_routing.ModelSpendDeferred("waiting for the budget: busy",
                                           retry_after_seconds=0.01)


def test_the_scan_waits_a_deferral_out_and_stops_only_on_a_refusal(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    held = ("res_1", "rcpt_1")
    assert _governor([_deferred, _deferred, lambda: held]).reserve() == held

    def refused():
        raise model_routing.ModelSpendRefused("Budget cap exceeded")

    with pytest.raises(rule_scan.BudgetRefused, match="cap"):
        _governor([refused]).reserve()
    with pytest.raises(rule_scan.BudgetRefused, match="busy"):
        _governor([_deferred] * (rule_scan.Governor.DEFER_TRIES + 1)).reserve()
