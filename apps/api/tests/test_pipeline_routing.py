"""VAG B2: per-step (engine, model) routing — route_for_step/_parse_route/materialize_route
in pipeline_profiles. Backward-compat invariant: with no model_config, no class-engine
bindings, and only low_cost/strong routing values, resolution is EXACTLY pre-B2 behavior
(lane default engine + strong/low-cost model)."""
from apps.api.app.services.pipeline_profiles import (
    PROFILES,
    REVIEW_LANE_STEPS,
    _parse_route,
    materialize_route,
    model_for_step,
    resolve_profile,
    route_for_step,
)

ALL_STEPS = ("standards", "spec", "plan", "review_plan", "write_tests", "implement",
             "review_code", "security_check", "consistency_check", "check_tests")


def _materialize(route, **over):
    kw = dict(build_engine="claude_code", review_engine="native",
              strong_engine="", low_cost_engine="", strong_model="", low_cost_model="")
    kw.update(over)
    return materialize_route(route, **kw)


# --------------------------------------------------------------------------- #
#  Backward compatibility                                                     #
# --------------------------------------------------------------------------- #
def test_no_config_resolves_to_lane_engine_and_class_model():
    profile = resolve_profile("balanced")
    for step in ALL_STEPS:
        route = route_for_step(profile, step)
        eng, model = _materialize(route, strong_model="opus", low_cost_model="haiku")
        expected_lane = "native" if step in REVIEW_LANE_STEPS else "claude_code"
        assert eng == expected_lane, step
        # model equals today's model_for_step class mapping — on an engine that can run it. The
        # review lane here is OpenAI (`native`), which cannot run a Claude model: it runs its own
        # default instead of `opus` (model_router.compat; boss-1 2026-11-03 launched
        # `claude --model gpt-6-sol` from exactly this kind of pairing).
        expected_model = "opus" if model_for_step(profile, step) == "strong" else "haiku"
        if eng == "native":
            expected_model = None
        assert model == expected_model, step


def test_no_config_empty_class_models_mean_engine_default():
    profile = resolve_profile("strict")
    route = route_for_step(profile, "implement")
    eng, model = _materialize(route)  # no strong/low-cost models bound
    assert eng == "claude_code" and model is None


# --------------------------------------------------------------------------- #
#  _parse_route forms                                                         #
# --------------------------------------------------------------------------- #
def test_parse_route_forms():
    assert _parse_route("low_cost") == {"engine": None, "model": None, "model_class": "low_cost"}
    assert _parse_route("strong") == {"engine": None, "model": None, "model_class": "strong"}
    assert _parse_route("native:deepseek-chat") == {
        "engine": "native", "model": "deepseek-chat", "model_class": None}
    assert _parse_route("claude_code:") == {
        "engine": "claude_code", "model": None, "model_class": None}
    assert _parse_route({"engine": "native", "model": "qwen2.5-coder-32b-instruct"}) == {
        "engine": "native", "model": "qwen2.5-coder-32b-instruct", "model_class": None}
    # bare string = model on the lane engine
    assert _parse_route("deepseek/deepseek-chat") == {
        "engine": None, "model": "deepseek/deepseek-chat", "model_class": None}


def test_parse_route_garbage_falls_back_to_strong():
    for garbage in (123, None, [], {}, "", "  "):
        assert _parse_route(garbage)["model_class"] == "strong", repr(garbage)


# --------------------------------------------------------------------------- #
#  Precedence                                                                 #
# --------------------------------------------------------------------------- #
def test_precedence_spec_step_over_spec_default_over_profile():
    profile = resolve_profile("balanced")  # profile: implement=low_cost, reviews=strong
    mc = {"default": "native:deepseek-chat", "steps": {"implement": "claude_code:opus"}}
    r = route_for_step(profile, "implement", mc)
    assert (r["engine"], r["model"]) == ("claude_code", "opus")  # spec step wins
    r = route_for_step(profile, "write_tests", mc)
    assert (r["engine"], r["model"]) == ("native", "deepseek-chat")  # spec default wins
    r = route_for_step(profile, "write_tests", None)
    assert r["model_class"] == model_for_step(profile, "write_tests")  # profile wins


# --------------------------------------------------------------------------- #
#  Lanes + class-engine binding                                               #
# --------------------------------------------------------------------------- #
def test_lane_assignment():
    profile = resolve_profile("strict")
    assert route_for_step(profile, "security_check")["lane"] == "review"
    assert route_for_step(profile, "check_tests")["lane"] == "review"
    assert route_for_step(profile, "write_tests")["lane"] == "build"
    assert route_for_step(profile, "standards")["lane"] == "build"


def test_class_engine_binding_routes_low_cost_to_other_engine():
    """The VAG junior/senior split: low_cost bound to native/deepseek while strong
    steps stay on the lane engine."""
    profile = resolve_profile("vag")  # implement=low_cost, default=strong
    imp = route_for_step(profile, "implement")
    eng, model = _materialize(imp, build_engine="claude_code", review_engine="claude_code",
                              low_cost_engine="native", low_cost_model="deepseek-chat")
    assert (eng, model) == ("native", "deepseek-chat")
    rev = route_for_step(profile, "review_code")
    eng, model = _materialize(rev, build_engine="claude_code", review_engine="claude_code",
                              low_cost_engine="native", low_cost_model="deepseek-chat")
    assert (eng, model) == ("claude_code", None)  # strong: lane engine, engine-default model


def test_vag_profile_exists_and_is_high_rigor():
    assert PROFILES["vag"]["tier_floor"] >= 2
    assert PROFILES["vag"]["model"]["implement"] == "low_cost"
    assert PROFILES["vag"]["model"]["default"] == "strong"
