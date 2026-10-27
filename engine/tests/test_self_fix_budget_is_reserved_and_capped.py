"""P0: a self-fix's budget is reserved before it starts and capped while it runs (BOSSAPP-571
R12, spec §8.4). BOSS's spend on itself is its own category, never the step's recovery budget;
the month has a ceiling; spent → refused: budget, and the recommendation stays."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build

from ._self_fix_support import harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_the_budget_is_reserved_under_its_own_category(harness):
    # A PAID engine (the Agent SDK runs on the API key only), so the hold is the whole budget
    # whatever login this host has; a subscription engine holds $0 (test_a_self_fix_on_a_
    # subscription_fits_a_small_daily_cap).
    harness.mp.setenv("BOSS_SELF_FIX_ENGINE", "claude_agent_sdk")
    out = harness.propose()
    sfx = out["self_fix"]
    with harness.eng.begin() as c:
        reservation = c.execute(text(
            "SELECT category, amount_usd_micros, status FROM budget_reservations WHERE id = :i"),
            {"i": sfx["budget_reservation_id"]}).mappings().first()
    assert reservation["category"] == "self_fix" and reservation["status"] == "active"
    assert reservation["amount_usd_micros"] == 1000 * 10_000 == sfx["budget_usd_micros"]


def test_a_refused_budget_means_no_self_fix(harness):
    harness.mp.setattr(sf, "_reserve", lambda *a, **k: (None, "over the project's cap"))
    out = harness.propose()
    assert out["outcome"] == "skipped" and "cap" in out["why"]


def test_the_month_has_a_ceiling(harness):
    with harness.eng.begin() as c:
        sf.set_switch(c, on=True, actor="test", settings_patch={"self_fix_month_cents": 500})
    out = harness.propose()
    assert out["outcome"] == "skipped" and "this month" in out["why"]


def test_spending_the_budget_mid_build_refuses_it(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET budget_usd_micros = 1 WHERE id = :i"),
                  {"i": sfx["id"]})
    build = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                                 source=harness.mirror)
    build.cost_micros = 5
    out = build.run()
    assert out["refusal"] == "budget"
    with harness.eng.begin() as c:
        row = sf.get(c, sfx["id"])
        rec = c.execute(text("SELECT status FROM boss_recommendations WHERE id = :i"),
                        {"i": row["recommendation_id"]}).scalar()
    assert row["status"] == sf.REFUSED and rec == "open"


def test_the_time_cap_refuses_it(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET started_at = now() - interval '2 hours' "
                       "WHERE id = :i"), {"i": sfx["id"]})
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["refusal"] == "time"


def test_a_finished_fix_holds_what_it_cost_not_its_reservation(harness):
    """Live install, 2026-10-24: three refused fixes that cost $15.47 held $30 of a $50 month,
    so a fourth fix found 'no room'. A finished fix spends nothing more: it counts its cost."""
    sfx = harness.propose()["self_fix"]
    reserved = int(sfx["budget_usd_micros"])
    with harness.eng.begin() as c:
        assert sf.month_spent(c) == reserved  # still able to spend: the whole reservation
        c.execute(text("UPDATE self_fixes SET status = 'refused', cost_micros = 1000000 "
                       "WHERE id = :i"), {"i": sfx["id"]})
        assert sf.month_spent(c) == 1_000_000
        c.execute(text("UPDATE self_fixes SET status = 'not_resolved' WHERE id = :i"),
                  {"i": sfx["id"]})
        assert sf.month_spent(c) == 1_000_000
        c.execute(text("UPDATE self_fixes SET status = 'awaiting_approval' WHERE id = :i"),
                  {"i": sfx["id"]})
        assert sf.month_spent(c) == reserved  # repair rounds may still spend


def test_a_fix_every_reviewer_passed_is_kept_when_its_last_review_overran(harness):
    """sfx_5fa667f3 (brec_c15618): every gate and all four reviewers passed, then the build was
    refused 'budget' at $11.70 of $10 — the review round's own spend, already sunk. The caps
    stop new spending; delivering spends nothing. The overrun is recorded, the card is put up,
    and a person still approves."""
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    real_review = self_fix_build.Build.review

    def review_that_overruns(self, *a, **k):
        findings = real_review(self, *a, **k)
        self.cost_micros = int(self.sfx()["budget_usd_micros"]) + 1_700_000
        return findings

    harness.mp.setattr(self_fix_build.Build, "review", review_that_overruns)
    self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                         source=harness.mirror).run()
    with harness.eng.begin() as c:
        row = sf.get(c, sfx["id"])
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    assert row["decision_id"], "a person still approves it"
    assert row["review"]["overrun"]["budget"]["spent_micros"] > row["budget_usd_micros"]


def test_an_overrun_before_a_stage_that_spends_still_refuses(harness):
    """The other half: once over the cap, no new stage starts (a review round's findings need
    a fix round, which spends)."""
    harness.script("review_blocker")
    sfx = harness.propose()["self_fix"]
    real_review = self_fix_build.Build.review

    def review_that_overruns(self, *a, **k):
        findings = real_review(self, *a, **k)
        self.cost_micros = int(self.sfx()["budget_usd_micros"]) + 1
        return findings

    harness.mp.setattr(self_fix_build.Build, "review", review_that_overruns)
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["refusal"] == "budget"


# THE CAP BOUNDS EACH TURN WHILE IT RUNS (operator, 2026-10-25): sfx_3f2fb90a spent $15.64 of a
# $10 cap inside ONE diagnosis turn — the cap was checked only at stage boundaries.

class _CeilingAgent:
    """An engine that can stop itself at a spend ceiling: records what it was given."""

    def __init__(self, *, can_stop: bool):
        from boss_engine.services.engine_contract import EngineCapabilities

        self.capabilities = EngineCapabilities(budget_limit=can_stop)
        self.calls = []

    def run_role(self, prompt, cwd, **kwargs):
        self.calls.append(kwargs)
        return {"summary": "done", "cost_usd_micros": 0}


def test_a_turn_is_given_what_is_left_of_the_cap(harness, tmp_path):
    sfx = harness.propose()["self_fix"]
    build = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                                 source=harness.mirror)
    build.cost_micros = int(sfx["budget_usd_micros"]) - 2_500_000
    agent = _CeilingAgent(can_stop=True)
    build.turn(agent, "self_fix_diagnose", "p", tmp_path, read_only=True)
    assert agent.calls[-1]["max_budget_usd"] == 2.5, "the turn's ceiling is the cap left"
    build.cost_micros = int(sfx["budget_usd_micros"]) + 1
    build.turn(agent, "self_fix_diagnose", "p", tmp_path, read_only=True)
    assert agent.calls[-1]["max_budget_usd"] == 0.0
    plain = _CeilingAgent(can_stop=False)
    build.turn(plain, "self_fix_diagnose", "p", tmp_path, read_only=True)
    assert "max_budget_usd" not in plain.calls[-1], "only an engine that can stop itself"


def test_the_claude_engine_stops_itself_at_the_ceiling(monkeypatch, tmp_path):
    """ClaudeCodeAgent passes the ceiling to the CLI (--max-budget-usd), which stops there."""
    from boss_engine.config import get_settings
    from boss_engine.services import code_agent

    agent = code_agent.ClaudeCodeAgent(get_settings())
    assert agent.capabilities.budget_limit
    seen = {}

    def fake_invoke(prompt, cwd, **kwargs):
        seen.update(kwargs)
        return {"result": "ok", "num_turns": 1, "total_cost_usd": 0.01, "usage": {}}

    monkeypatch.setattr(agent, "_invoke", fake_invoke)
    agent.run_role("p", tmp_path, read_only=True, max_budget_usd=2.5)
    assert seen["max_budget_usd"] == 2.5
    src = open(code_agent.__file__, encoding="utf-8").read()
    assert '"--max-budget-usd"' in src


def test_every_engine_that_claims_a_ceiling_takes_it_all_the_way_down(tmp_path, monkeypatch):
    """budget_limit is a promise: run_role AND the session it starts accept max_budget_usd.
    (The Agent SDK engine inherited the claim from claude_code without the parameter.)"""
    import inspect

    from boss_engine.config import get_settings
    from boss_engine.services import agent_sdk_engine, code_agent

    for cls in (code_agent.ClaudeCodeAgent, agent_sdk_engine.build_agent_class()):
        assert cls.capabilities.budget_limit, cls.__name__
        for method in ("run_role", "_invoke"):
            assert "max_budget_usd" in inspect.signature(getattr(cls, method)).parameters, (
                f"{cls.__name__}.{method} claims budget_limit but cannot take max_budget_usd")
    sdk = agent_sdk_engine.build_agent_class()(get_settings())
    monkeypatch.setattr(sdk, "_child_env", lambda: {})   # the option, not the credential
    try:
        options = sdk._options(cwd=tmp_path, read_only=True, allowed="Read", permission="default",
                               max_turns=4, model="m", resume=None, shared_ctx="", fan_out=0,
                               effort=None, max_budget_usd=2.5)
    except ImportError:  # pragma: no cover — the SDK ships in the API image
        return
    assert options.max_budget_usd == 2.5
