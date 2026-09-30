"""VAG #110 — generator/verifier independence contract: family taxonomy, separation ranking,
the fail-closed gate, shared-trace detection, and the config audit. All pure (no DB/models);
one endpoint test via the client."""
from types import SimpleNamespace

from apps.api.app.services import independence as ind


# --- family taxonomy ---------------------------------------------------------
def test_family_of_derives_lineage_from_engine_and_model():
    assert ind.family_of("claude_code") == "claude"
    assert ind.family_of("claude_cli") == "claude"
    assert ind.family_of("native", "gpt-4o") == "gpt"
    assert ind.family_of("azure:", "gpt-4o") == "gpt"
    assert ind.family_of("openrouter:deepseek/deepseek-chat") == "deepseek"
    assert ind.family_of("openrouter:qwen/qwen-2.5-coder") == "qwen"
    assert ind.family_of("native", "gemini-1.5-pro") == "gemini"


# --- separation ranking ------------------------------------------------------
def _d(engine, model=None):
    return ind._descriptor(engine, model)


def test_separation_ranks():
    claude = _d("claude_code")
    assert ind.separation(claude, _d("claude_code")) == ind._SAME_MODEL          # self
    assert ind.separation(_d("native", "gpt-4o"), _d("native", "gpt-3.5")) == ind._SAME_FAMILY
    assert ind.separation(_d("openrouter:deepseek/x"), _d("openrouter:qwen/y")) == ind._SAME_PROVIDER
    assert ind.separation(claude, _d("native", "gpt-4o")) == ind._INDEPENDENT   # diff family+provider


# --- assess + gate -----------------------------------------------------------
def test_assess_takes_weakest_verifier():
    v = ind.assess_independence(("claude_code", None),
                                [("native", "gpt-4o"), ("claude_cli", None)])
    # the independent gpt reviewer is rank 3, but the same-family claude reviewer drags the
    # contract down to its level — the panel is only as independent as its weakest member.
    assert v["min_level"] == "same_family"
    assert v["n_verifiers"] == 2


def test_gate_passes_independent_panel():
    v = ind.assess_independence(("claude_code", None), [("native", "gpt-4o")])
    g = ind.independence_gate(v, required_level="family")
    assert g["passed"] and g["reasons"] == []


def test_gate_blocks_same_family_and_self_review():
    # same family, different model -> fails a family requirement
    v = ind.assess_independence(("native", "gpt-4o"), [("native", "gpt-3.5")])
    g = ind.independence_gate(v, required_level="family")
    assert not g["passed"] and any("same_family" in r for r in g["reasons"])
    # but passes a weaker "model" requirement (models do differ)
    assert ind.independence_gate(v, required_level="model")["passed"]
    # no verifiers at all -> builder judging itself
    empty = ind.assess_independence(("claude_code", None), [])
    assert not ind.independence_gate(empty)["passed"]


def test_off_level_disables_the_contract():
    v = ind.assess_independence(("claude_code", None), [("claude_code", None)])
    assert ind.independence_gate(v, required_level="off")["passed"]


# --- shared trace ------------------------------------------------------------
def test_shares_trace_detects_leaked_reasoning():
    trace = "I chose a greedy loop because the input is small and sorting would be overkill here."
    assert ind.shares_trace(f"Review this code.\n{trace}\nIs it correct?", trace) is True
    assert ind.shares_trace("Review this code on its own merits. VERDICT?", trace) is False
    assert ind.shares_trace("short", "short") is False   # too short to conclude


# --- config audit ------------------------------------------------------------
def test_configured_contract_flags_self_review():
    # reviewer defaults to the builder engine -> NOT independent -> gate fails
    bad = SimpleNamespace(code_agent_engine="claude_code", code_review_engine="",
                          code_agent_strong_model="", independence_required_level="family")
    assert ind.configured_contract(bad)["gate"]["passed"] is False
    # a distinct reviewer family -> independent -> passes
    good = SimpleNamespace(code_agent_engine="claude_code", code_review_engine="native",
                           code_agent_strong_model="gpt-4o", independence_required_level="family")
    assert ind.configured_contract(good)["gate"]["passed"] is True


# --- endpoint ----------------------------------------------------------------
def test_endpoint_audit_and_explicit(client):
    # explicit builder/verifiers
    resp = client.post("/v1/independence-check", json={
        "builder": {"engine": "claude_code"},
        "verifiers": [{"engine": "native", "model": "gpt-4o"}],
        "required_level": "family"})
    assert resp.status_code == 200 and resp.json()["gate"]["passed"] is True
    # no payload -> audits current config, returns a gate verdict
    audit = client.post("/v1/independence-check", json={}).json()
    assert "gate" in audit and "min_level" in audit
