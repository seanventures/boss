"""A self-fix on a subscription engine can start in a project whose daily cap is below the per-fix
budget; a paid self-fix that cannot fit is still refused.

boss-platform, 2026-10-26: requesting a self-fix failed with "Budget cap exceeded: this needs $20
reserved, more than today's whole budget of $10". The per-fix budget (self_fix_budget_cents=2000)
was held against the project's daily MONEY cap, while every call ran on the Claude subscription
(included_in_subscription) and would settle at $0. A project whose cap is below the per-fix budget
could never run any self-fix. Money caps govern money (included_spend.py): on included capacity
the hold is $0 — a gate, still refused once the cap's real money is gone — and the fix stays
bounded by its own budget.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import claude_credential
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services.budget import BudgetGovernor

from ._self_fix_support import harness, self_fix_on  # noqa: F401
from .test_self_fix_budget_is_reserved_and_capped import _CeilingAgent
from .test_workflow_capability_runner import eng, project  # noqa: F401

TEN_DOLLARS = 10_000_000
TWENTY_DOLLARS = 20_000_000


@pytest.fixture()
def small_cap(harness):
    """A $10 daily cap and a $20 per-fix budget (the boss-platform settings)."""
    with harness.eng.begin() as c:
        c.execute(text("DELETE FROM budget_policies WHERE scope_id = :p"), {"p": harness.project})
        c.execute(text(
            "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
            "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
            "VALUES (:p, 'project', :p, :cap, 0, 0)"), {"p": harness.project, "cap": TEN_DOLLARS})
        sf.set_switch(c, on=True, actor="test", settings_patch={"self_fix_budget_cents": 2000})
    yield harness
    with harness.eng.begin() as c:
        c.execute(text("DELETE FROM budget_policies WHERE scope_id = :p"), {"p": harness.project})


def _runs_on(harness, kind: str, *, engine: str = "claude_code", reviewer: str = "") -> None:
    harness.mp.setenv("BOSS_SELF_FIX_ENGINE", engine)
    if reviewer:
        harness.mp.setenv("BOSS_SELF_FIX_REVIEW_ENGINE", reviewer)
    else:
        harness.mp.delenv("BOSS_SELF_FIX_REVIEW_ENGINE", raising=False)
    harness.mp.setattr(claude_credential, "resolve_claude_credential",
                       lambda *a, **k: claude_credential.ClaudeCredential(kind))


def _reservation(harness, reservation_id: str) -> dict:
    with harness.eng.begin() as c:
        return dict(c.execute(text(
            "SELECT category, amount_usd_micros, status FROM budget_reservations WHERE id = :i"),
            {"i": reservation_id}).mappings().first())


@pytest.mark.parametrize("kind", [claude_credential.KIND_SUBSCRIPTION,
                                  claude_credential.KIND_CLI_LOGIN])
def test_a_self_fix_on_a_subscription_fits_a_small_daily_cap(small_cap, tmp_path, kind):
    _runs_on(small_cap, kind)
    out = small_cap.propose()
    assert out["outcome"] == "started", out.get("why")
    sfx = out["self_fix"]
    held = _reservation(small_cap, sfx["budget_reservation_id"])
    assert held["category"] == "self_fix" and held["status"] == "active"
    assert held["amount_usd_micros"] == 0, "no money is spent on a subscription: a gate, no hold"
    # STILL BOUNDED: its own $20 budget, which each turn's ceiling and the build's budget stop use.
    assert sfx["budget_usd_micros"] == TWENTY_DOLLARS
    build = self_fix_build.Build(engine=small_cap.eng, settings=small_cap.settings,
                                 sfx_id=sfx["id"], source=small_cap.mirror)
    agent = _CeilingAgent(can_stop=True)
    build.turn(agent, "self_fix_diagnose", "p", tmp_path, read_only=True)
    assert agent.calls[-1]["max_budget_usd"] == 20.0


def test_a_paid_self_fix_that_cannot_fit_is_still_refused(small_cap):
    _runs_on(small_cap, claude_credential.KIND_STORED_KEY)  # claude_code on an API key is money
    out = small_cap.propose()
    assert out["outcome"] == "skipped"
    assert "more than today's whole budget" in out["why"], out["why"]


@pytest.mark.parametrize("engine,reviewer", [("claude_agent_sdk", ""), ("codex", ""),
                                             ("claude_code", "claude_agent_sdk")])
def test_any_paid_engine_in_the_fix_holds_the_whole_budget(small_cap, engine, reviewer):
    """A paid reviewer next to a subscription Mender is still money; codex may be on a key."""
    _runs_on(small_cap, claude_credential.KIND_SUBSCRIPTION, engine=engine, reviewer=reviewer)
    out = small_cap.propose()
    assert out["outcome"] == "skipped" and "more than today's whole budget" in out["why"]


def test_an_unknown_credential_is_money(small_cap):
    _runs_on(small_cap, claude_credential.KIND_NONE)
    with small_cap.eng.begin() as c:
        assert not sf.runs_on_included_capacity(c, small_cap.settings, small_cap.project)

    def broken(*a, **k):
        raise RuntimeError("store down")

    small_cap.mp.setattr(claude_credential, "resolve_claude_credential", broken)
    with small_cap.eng.begin() as c:
        assert not sf.runs_on_included_capacity(c, small_cap.settings, small_cap.project)


def test_the_gate_still_refuses_once_the_cap_is_used(small_cap):
    """$0 is a gate, not a free pass: a day whose cap is already committed refuses it."""
    _runs_on(small_cap, claude_credential.KIND_SUBSCRIPTION)
    with small_cap.eng.begin() as c:
        got = BudgetGovernor(small_cap.settings).reserve(
            c, small_cap.project, "model", TEN_DOLLARS, "other paid work", 60)
        assert got["status"] == "reserved"
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = :cap "
                       "WHERE scope_id = :p"), {"p": small_cap.project, "cap": TEN_DOLLARS // 2})
    out = small_cap.propose()
    assert out["outcome"] == "skipped", out
