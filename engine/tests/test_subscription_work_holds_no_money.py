"""Work on subscription capacity reserves its marginal cost, so it cannot turn a budget red.

2026-10-13: each PRD meeting reserved $6.00 for up to two hours, and active reservations count
toward the $10 daily cap (budget.period_spend). Two meetings, or a meeting beside a build, held
work behind a red budget — while every call they made ran on a Claude subscription and settled at
$0 (`included_in_subscription`). Reservations for model work on the project's ROUTED model are
now sized by the rule its calls settle by: $0 on included capacity (a gate, still refused once
real money hits the cap), the full estimate on a metered provider.

Also: the architecture typed decision declared max_usage total_tokens 20,000 and overran it on
every call (21,023–30,009 measured); the ceiling is now a measured value.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from boss_engine.services import (
    code_task_service, included_spend, meeting_service, model_routing, typed_decision_spend,
)

SIX_DOLLARS = 6_000_000


class _Conn:
    """Enough of a connection for the savepoint around the routing lookup."""

    def __init__(self):
        self.savepoints = 0

    def begin_nested(self):
        self.savepoints += 1
        return SimpleNamespace(commit=lambda: None, rollback=lambda: None, is_active=False)


def test_subscription_capacity_reserves_nothing_and_metered_keeps_the_estimate(monkeypatch):
    monkeypatch.setattr(model_routing, "call_is_included", lambda c, s, p, u: True)
    assert included_spend.routed_reservation_estimate(_Conn(), None, "p1", SIX_DOLLARS) == 0
    monkeypatch.setattr(model_routing, "call_is_included", lambda c, s, p, u: False)
    assert included_spend.routed_reservation_estimate(_Conn(), None, "p1", SIX_DOLLARS) == SIX_DOLLARS


def test_unknown_is_money(monkeypatch):
    def broken(*_a):
        raise RuntimeError("routing table unreadable")

    monkeypatch.setattr(model_routing, "call_is_included", broken)
    assert included_spend.routed_reservation_estimate(_Conn(), None, "p1", SIX_DOLLARS) == SIX_DOLLARS


def test_two_meetings_on_a_subscription_leave_the_daily_cap_untouched(monkeypatch):
    """The arithmetic of the incident: what two meetings and a build hold against $10."""
    monkeypatch.setattr(model_routing, "call_is_included", lambda c, s, p, u: True)
    held = sum(included_spend.routed_reservation_estimate(_Conn(), None, "p1", amount)
               for amount in (SIX_DOLLARS, SIX_DOLLARS, 2_000_000))
    assert held == 0
    monkeypatch.setattr(model_routing, "call_is_included", lambda c, s, p, u: False)
    held = sum(included_spend.routed_reservation_estimate(_Conn(), None, "p1", amount)
               for amount in (SIX_DOLLARS, SIX_DOLLARS, 2_000_000))
    assert held > 10_000_000  # metered: the cap still binds, as it should


def test_the_meeting_and_the_code_task_reserve_through_the_marginal_rule():
    meeting = inspect.getsource(meeting_service)
    at = meeting.index('"Multi-agent PRD meeting."')
    assert "routed_reservation_estimate(" in meeting[at - 400:at]
    code = inspect.getsource(code_task_service)
    at = code.index('reason=f"code task: {run[\'title\'][:80]}"')
    # Every build reserves through build_budget.reservation_estimate, which IS the marginal rule.
    assert "_build_budget.reservation_estimate(" in code[at - 400:at]
    from boss_engine.services import build_budget
    assert "routed_reservation_estimate(" in inspect.getsource(build_budget.reservation_estimate)


def test_the_typed_decision_token_ceiling_holds_what_was_measured():
    measured = [24853, 21310, 22471, 21023, 26111, 22942, 23037, 24436, 30009, 21822]
    assert typed_decision_spend.TYPED_DECISION_MAX_TOTAL_TOKENS >= 2 * max(measured)
    src = inspect.getsource(typed_decision_spend)
    assert 'max_usage={"total_tokens": TYPED_DECISION_MAX_TOTAL_TOKENS}' in src
    assert '"total_tokens": 20_000' not in src
