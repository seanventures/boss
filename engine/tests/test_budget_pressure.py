"""BOSS-280 — auto-lite mode, actually enforced: the pure budget-pressure clamp."""
from boss_engine.services.pipeline_profiles import (
    PROFILES,
    apply_budget_pressure,
    route_for_step,
)


def _route(profile_name, step, model_config=None):
    return route_for_step(PROFILES[profile_name], step, model_config)


def test_green_and_yellow_change_nothing():
    r = _route("balanced", "implement")
    for state in ("green", "yellow", "unknown", ""):
        out, lite = apply_budget_pressure(r, "implement", state)
        assert out == r and lite is False


def test_orange_degrades_strong_build_steps():
    # Balanced intentionally uses the economical builder and strong verification.
    # Strict is the profile that exercises a class-routed strong build step.
    r = _route("strict", "implement")
    assert r["model_class"] == "strong"
    out, lite = apply_budget_pressure(r, "implement", "orange")
    assert lite is True and out["model_class"] == "low_cost"
    # red behaves the same
    out2, lite2 = apply_budget_pressure(r, "implement", "red")
    assert lite2 is True and out2["model_class"] == "low_cost"


def test_verification_never_degrades():
    for step in ("review_code", "security_check", "consistency_check", "check_tests"):
        r = _route("balanced", step)
        out, lite = apply_budget_pressure(r, step, "red")
        assert lite is False and out == r      # the checker is never cheapened


def test_explicit_operator_pin_never_overridden():
    r = _route("balanced", "implement",
               {"steps": {"implement": {"engine": "claude_code", "model": "opus"}}})
    out, lite = apply_budget_pressure(r, "implement", "red")
    assert lite is False and out.get("model") == "opus"


def test_already_cheap_steps_untouched():
    r = _route("balanced", "standards")             # balanced default is low_cost
    assert r["model_class"] == "low_cost"
    out, lite = apply_budget_pressure(r, "standards", "red")
    assert lite is False and out == r
