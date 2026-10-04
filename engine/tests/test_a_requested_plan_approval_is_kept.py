"""An operator who asks for tier 3 asks to sign the plan (factory test D7).

Community's default `lean` profile caps inferred rigor at tier 1. An explicit `risk_tier: 3`
was clamped to 2 with it, so the build wrote code without the plan approval it was asked to
wait for — the `Approve the plan?` card could never appear. A cost profile limits what BOSS
infers; it never removes a human gate a person requested.
"""
from __future__ import annotations

from boss_engine.services.governed_code_task_service import _requires_plan_approval
from boss_engine.services.pipeline_profiles import PROFILES, infer_risk_tier

LEAN = PROFILES["lean"]


def test_an_explicit_tier_3_survives_the_lean_cap():
    tier = infer_risk_tier({"title": "Rotate keys", "risk_tier": 3}, LEAN)
    assert tier == 3 and _requires_plan_approval(tier)


def test_what_boss_infers_is_still_capped():
    # Sensitive but not requested: the cost profile's cap and the safety floor, as before.
    assert infer_risk_tier({"title": "Rotate the API secret keys"}, LEAN) == 2
    assert infer_risk_tier({"title": "Fix a typo", "risk_tier": 2}, LEAN) == 1
