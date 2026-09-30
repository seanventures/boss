"""VAG #109 — budget-variable repair depth + tier escalation. Pure decision logic; one endpoint test."""
from apps.api.app.services import repair_policy as rp


# --- budget math -------------------------------------------------------------
def test_affordable_and_depth():
    assert rp.affordable_attempts(1_000_000, 200_000) == 5
    assert rp.affordable_attempts(150_000, 200_000) == 0        # can't afford one
    assert rp.affordable_attempts(1_000_000, 0) == 0            # guard div-by-zero
    # depth is capped by BOTH budget and the retry cap
    assert rp.budget_variable_depth(1_000_000, 200_000, max_retries=3) == 3   # cap binds
    assert rp.budget_variable_depth(400_000, 200_000, max_retries=10) == 2    # budget binds


def test_dollars_per_verified():
    assert rp.dollars_per_verified(600_000, 3) == 200_000.0
    assert rp.dollars_per_verified(500_000, 0) is None          # nothing verified -> infinite


# --- decide_repair -----------------------------------------------------------
_TIERS = dict(cheap_tier="native", strong_tier="claude_code", max_retries=3,
              escalate_after=1, est_attempt_cost=200_000)


def test_stops_when_gate_passed_or_retries_exhausted():
    passed = rp.decide_repair(attempt=0, spent_usd_micros=0, budget_usd_micros=1_000_000,
                              last_gate_passed=True, **_TIERS)
    assert passed["action"] == "stop" and "verified" in passed["reason"]
    exhausted = rp.decide_repair(attempt=3, spent_usd_micros=0, budget_usd_micros=1_000_000, **_TIERS)
    assert exhausted["action"] == "stop" and "depth exhausted" in exhausted["reason"]


def test_first_attempt_uses_cheap_tier():
    d = rp.decide_repair(attempt=0, spent_usd_micros=0, budget_usd_micros=1_000_000, **_TIERS)
    assert d["action"] == "retry" and d["tier"] == "native" and d["escalated"] is False


def test_escalates_to_strong_after_cheap_failures():
    d = rp.decide_repair(attempt=1, spent_usd_micros=200_000, budget_usd_micros=1_000_000, **_TIERS)
    assert d["action"] == "retry" and d["tier"] == "claude_code" and d["escalated"] is True
    assert "escalate" in d["reason"]


def test_budget_variable_stop_when_cant_afford_next():
    # escalated attempt with a pricey strong tier the remaining budget can't cover -> stop
    d = rp.decide_repair(attempt=1, spent_usd_micros=900_000, budget_usd_micros=1_000_000,
                         strong_attempt_cost=500_000, **_TIERS)
    assert d["action"] == "stop" and "budget can't afford" in d["reason"]


def test_no_strong_tier_stays_cheap():
    d = rp.decide_repair(attempt=2, spent_usd_micros=0, budget_usd_micros=1_000_000,
                         cheap_tier="native", strong_tier="", max_retries=3,
                         escalate_after=1, est_attempt_cost=200_000)
    assert d["tier"] == "native" and d["escalated"] is False    # nothing to escalate to


def test_plan_repair_summarizes():
    plan = rp.plan_repair(remaining_usd_micros=1_000_000, est_attempt_cost=200_000,
                          max_retries=3, escalate_after=1, cheap_tier="native", strong_tier="claude_code")
    assert plan["max_depth"] == 3 and plan["escalate_after"] == 1
    assert "escalating to 'claude_code'" in plan["note"]


# --- endpoint ----------------------------------------------------------------
def test_endpoint_plan_and_decide(client):
    plan = client.post("/v1/repair-policy", json={"budget_usd_micros": 1_000_000,
                                                  "est_attempt_cost": 200_000, "max_retries": 3}).json()
    assert plan["max_depth"] == 3
    decide = client.post("/v1/repair-policy", json={
        "attempt": 1, "spent_usd_micros": 200_000, "budget_usd_micros": 1_000_000,
        "est_attempt_cost": 200_000, "max_retries": 3, "escalate_after": 1,
        "cheap_tier": "native", "strong_tier": "claude_code", "verified_count": 0}).json()
    assert decide["action"] == "retry" and decide["escalated"] is True
    assert decide["dollars_per_verified"] is None
