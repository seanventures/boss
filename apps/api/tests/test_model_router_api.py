"""Setup › Model routing over the real API and Postgres (docs/product/MODEL_ROUTER.md): the
view, saving a policy (merge, version, validation), presets as profiles, the route record on
llm_calls, the scorecard's routing summary, the Smart-vs-Fixed experiment and "Why this model".
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, connect_key, fakes


import pytest


@pytest.fixture(autouse=True)
def _clean_routing_state(engine):
    """project_settings and pipeline_profiles outlive a test here: leave neither a Smart mode
    nor a default profile behind for the next test (or file) on this project."""
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM project_settings WHERE project_id = :p "
                           "AND key = 'model_routing_mode'"), {"p": PROJECT})
            c.execute(text("DELETE FROM pipeline_profiles WHERE project_id = :p"),
                      {"p": PROJECT})
    wipe()
    yield
    wipe()


def _routing(client):
    view = client.get("/v1/setup", headers=AUTH, params={"project_id": PROJECT})
    assert view.status_code == 200, view.text
    return view.json()["routing"]


def _put(client, policy, version=None):
    return client.put("/v1/setup/routing", headers=AUTH, json={
        "project_id": PROJECT, "policy": policy, "version": version})


def test_an_untouched_install_is_fixed_and_seeded(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert connect_key(client).status_code == 200          # an OpenRouter key
    routing = _routing(client)
    assert routing["mode"] == "fixed" and routing["version"] == 0
    assert [t["tier"] for t in routing["tiers"]] == ["fast", "balanced", "frontier"]
    assert any(t["engine"] == "openrouter" for tier in routing["tiers"] for t in tier["targets"])
    # Community's default profile is `lean`: its floors are the base ones only.
    assert routing["floor_preset"] == "lean"
    assert {f["describe"] for f in routing["floors"]} >= {"Panel facilitator"}
    assert "Adversarial review" not in {f["describe"] for f in routing["floors"]}
    assert routing["classifier"]["jev"] in ("absent", "held")
    assert routing["week"]["routes"] == 0


def test_switching_to_smart_is_a_logged_setting_and_a_versioned_policy(client, engine,
                                                                        monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    saved = _put(client, {"mode": "smart"}, version=0)
    assert saved.status_code == 200, saved.text
    assert saved.json()["routing"]["mode"] == "smart"
    assert saved.json()["routing"]["version"] == 1
    with engine.begin() as c:
        mode = c.execute(text("SELECT value FROM project_settings WHERE project_id = :p "
                              "AND key = 'model_routing_mode'"), {"p": PROJECT}).scalar()
        logged = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                                "AND event_type = 'settings.changed' "
                                "AND data->>'key' = 'model_routing_mode'"),
                           {"p": PROJECT}).scalar()
    assert mode == "smart" and logged == 1


def test_a_stale_edit_is_refused_and_a_bad_policy_names_its_field(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert _put(client, {"thresholds": {"near_limit": 0.1}}, version=0).status_code == 200
    stale = _put(client, {"thresholds": {"near_limit": 0.12}}, version=0)
    assert stale.status_code == 409 and stale.json()["detail"]["version"] == 1
    bad = _put(client, {"headroom": [{"source": "claude_code", "when": "near_limit",
                                      "tiers": ["frontier"], "to": "openrouter"}]})
    assert bad.status_code == 422 and "Frontier" in bad.json()["detail"]["message"]


def test_a_preset_is_the_profile_in_both_modes(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    saved = _put(client, {"floor_preset": "strict"})
    assert saved.status_code == 200, saved.text
    floors = saved.json()["routing"]["floors"]
    assert floors[0]["match"] == {"kind": "build_stage"} and floors[0]["tier"] == "frontier"
    with engine.begin() as c:
        default = c.execute(text("SELECT name FROM pipeline_profiles WHERE project_id = :p "
                                 "AND is_default"), {"p": PROJECT}).scalar()
    assert default == "strict"


def test_a_route_is_recorded_and_its_calls_carry_it(engine, monkeypatch):
    from apps.api.app.config import get_settings
    from boss_engine.services.llm_telemetry import (
        clear_llm_context, record_llm_call, set_llm_context,
    )
    from boss_engine.services.model_router import store
    from boss_engine.services.model_router.router import LegacyRoute, RouteRequest

    with engine.begin() as c:
        decision = store.route(c, get_settings(), RouteRequest(
            kind="build_stage", step="implement", project_id=PROJECT, unit_id="crun_mr1",
            legacy=lambda: LegacyRoute("claude_code", "sonnet", "the profile says so",
                                       "low_cost")))
    set_llm_context(db_engine=engine, project_id=PROJECT, run_id="crun_mr1", stage="implement",
                    routes={"implement": decision.id})
    try:
        record_llm_call(engine_name="claude_code", usage={"model": "sonnet",
                                                          "prompt_tokens": 10})
    finally:
        clear_llm_context()
    with engine.begin() as c:
        route_id = c.execute(text("SELECT route_id FROM llm_calls WHERE code_task_run_id = "
                                  "'crun_mr1'")).scalar()
        why = c.execute(text("SELECT why FROM model_routes WHERE id = :i"),
                        {"i": decision.id}).scalar()
    assert route_id == decision.id
    assert why == ["Fixed: the profile says so"]


def test_why_this_model_is_readable_per_run(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    with engine.begin() as c:
        c.execute(text("INSERT INTO model_routes (id, project_id, unit_kind, unit_id, step, mode, "
                       "tier, engine, model, why, reason) VALUES ('route_w1', :p, 'build_stage', "
                       "'crun_why', 'review_code', 'smart', 'frontier', 'claude_code', 'opus', "
                       "CAST(:w AS JSONB), 'Floor: Adversarial review ≥ Frontier')"),
                  {"p": PROJECT, "w": json.dumps(["Floor: Adversarial review ≥ Frontier"])})
    got = client.get("/v1/model-routes", headers=AUTH,
                     params={"project_id": PROJECT, "unit_id": "crun_why"})
    assert got.status_code == 200, got.text
    route = got.json()["routes"][0]
    assert (route["tier"], route["model"], route["why"]) == (
        "frontier", "opus", ["Floor: Adversarial review ≥ Frontier"])


def test_a_scorecard_carries_the_units_routing(engine):
    from boss_engine.services.scorecards import stamp_routing

    with engine.begin() as c:
        c.execute(text("INSERT INTO run_scorecards (unit_kind, unit_id, project_id, "
                       "terminal_state) VALUES ('build', 'crun_sc', :p, 'completed')"),
                  {"p": PROJECT})
        for i, (step, tier, esc) in enumerate([("plan", "balanced", None),
                                               ("implement", "frontier", "tests failed ×2"),
                                               ("review_code", "frontier", None)]):
            c.execute(text("INSERT INTO model_routes (id, project_id, unit_kind, unit_id, step, "
                           "mode, tier, escalation, created_at) VALUES (:i, :p, 'build_stage', "
                           "'crun_sc', :s, 'smart', :t, :e, now() + make_interval(secs => :n))"),
                      {"i": f"route_sc{i}", "p": PROJECT, "s": step, "t": tier, "e": esc, "n": i})
        summary = stamp_routing(c, "build", "crun_sc")
        row = c.execute(text("SELECT route_mode, route_tier, routing FROM run_scorecards "
                             "WHERE unit_id = 'crun_sc'")).mappings().first()
    assert summary["escalations"] == 1 and summary["tiers"] == {"balanced": 1, "frontier": 2}
    assert (row["route_mode"], row["route_tier"]) == ("smart", "frontier")
    assert row["routing"]["routes"] == 3


def test_smart_vs_fixed_is_an_experiment(engine):
    from boss_engine.services import experiments

    with engine.begin() as c:
        exp = experiments.create(c, project_id=PROJECT, actor="ceo", name="routing",
                                 target_kind="role", target_key="software_engineer",
                                 knob="routing", arm_a="fixed", arm_b="smart",
                                 target_units=10)
        arms = set()
        for i in range(12):
            spec: dict = {"role_key": "software_engineer"}
            got = experiments.apply_arm(c, project_id=PROJECT, run_id=f"crun_exp{i}", spec=spec,
                                        finalize_ctx={})
            assert got and spec["routing_mode"] in ("fixed", "smart")
            arms.add(spec["routing_mode"])
        pinned: dict = {"role_key": "software_engineer", "routing_mode": "fixed"}
        experiments.apply_arm(c, project_id=PROJECT, run_id="crun_pinned", spec=pinned,
                              finalize_ctx={})
    assert arms == {"fixed", "smart"} and exp["knob_label"] == "Model routing"
    assert "experiment" not in pinned
