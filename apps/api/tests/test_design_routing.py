"""APP-6 (#96) — hybrid frontier+OSS routing for the design axis: tier classification, engine
resolution (with override + fallback), and the metered/capped design-pass budget. Pure."""
from apps.api.app.services import design_routing as dr


# --- tier classification -----------------------------------------------------
def test_design_axis_vs_fillin_vs_default():
    for s in ("spec", "plan", "product_design", "scaffold_theme", "visual_judge"):
        assert dr.resolve_tier(s) == "frontier" and dr.is_design_axis(s)
    for s in ("implement", "repair", "write_tests"):
        assert dr.resolve_tier(s) == "oss" and not dr.is_design_axis(s)
    for s in ("review_code", "security_check"):
        assert dr.resolve_tier(s) == "default"


# --- engine resolution -------------------------------------------------------
def test_resolve_engine_by_tier():
    kw = dict(frontier_engine="claude_code", oss_engine="native", default_engine="claude_code")
    assert dr.resolve_engine("product_design", **kw) == "claude_code"     # frontier
    assert dr.resolve_engine("implement", **kw) == "native"               # oss
    assert dr.resolve_engine("security_check", **kw) == "claude_code"     # default


def test_override_wins_and_empty_tier_falls_back():
    assert dr.resolve_engine("implement", frontier_engine="f", oss_engine="o", default_engine="d",
                             overrides={"implement": "custom"}) == "custom"
    # empty frontier engine -> default (safe no-op when unconfigured)
    assert dr.resolve_engine("spec", frontier_engine="", oss_engine="o", default_engine="d") == "d"


# --- metered design-pass budget ----------------------------------------------
def test_design_pass_plan_meters_and_caps():
    plan = dr.design_pass_plan(2_000_000, design_fraction=0.4, est_design_pass_cost=300_000,
                               max_design_passes=2)
    assert plan["design_envelope_usd_micros"] == 800_000       # 40% of 2M
    assert plan["max_design_passes"] == 2                       # min(cap=2, affordable=800k/300k=2)


def test_design_pass_plan_budget_binds_below_cap():
    plan = dr.design_pass_plan(1_000_000, design_fraction=0.3, est_design_pass_cost=300_000,
                               max_design_passes=5)
    assert plan["design_envelope_usd_micros"] == 300_000
    assert plan["max_design_passes"] == 1                       # envelope only affords 1 (< cap 5)


def test_should_run_design_pass_governor():
    plan = dr.design_pass_plan(2_000_000, design_fraction=0.4, est_design_pass_cost=300_000,
                               max_design_passes=2)
    ok = dr.should_run_design_pass(0, 0, plan)
    assert ok["allow"] is True
    capped = dr.should_run_design_pass(2, 0, plan)
    assert capped["allow"] is False and capped["fallback"] == "oss" and "cap" in capped["reason"]
    broke = dr.should_run_design_pass(1, 700_000, plan)          # 800k envelope, 700k spent -> <300k left
    assert broke["allow"] is False and "exhausted" in broke["reason"]


# --- endpoint ----------------------------------------------------------------
def test_endpoint_step_and_plan(client):
    step = client.post("/v1/design-routing", json={"step": "product_design"}).json()
    assert step["tier"] == "frontier" and step["engine"]
    fill = client.post("/v1/design-routing", json={"step": "implement"}).json()
    assert fill["tier"] == "oss"
    plan = client.post("/v1/design-routing", json={"budget_usd_micros": 2_000_000}).json()
    assert "max_design_passes" in plan and plan["design_envelope_usd_micros"] > 0
