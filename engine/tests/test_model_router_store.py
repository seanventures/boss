"""The router's database side on a stub store: a route is recorded once with its reasons,
llm_calls rows carry their route, the CLI's window numbers become headroom, a resumed session
keeps its model, and a snapshot that cannot be read routes as Fixed (docs/product/MODEL_ROUTER.md).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.model_router import store
from boss_engine.services.model_router.router import LegacyRoute, RouteRequest

ROUTES = (
    "CREATE TABLE model_routes (id TEXT PRIMARY KEY, project_id TEXT, unit_kind TEXT, "
    "unit_id TEXT, step TEXT, ref TEXT, role TEXT, mode TEXT, tier TEXT, classified_tier TEXT, "
    "classifier TEXT, confidence NUMERIC, engine TEXT, model TEXT, source_id TEXT, "
    "source_kind TEXT, rules TEXT, why TEXT, reason TEXT, escalation TEXT, "
    "escalation_signal TEXT, fallback BOOLEAN, moved_by_headroom BOOLEAN, budget_lite BOOLEAN, "
    "latency_ms NUMERIC, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")


@pytest.fixture()
def db(entitlement_tables):
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        entitlement_tables(c)
        c.execute(text(ROUTES))
        c.execute(text("CREATE TABLE llm_calls (engine TEXT, project_id TEXT, "
                       "cost_usd_micros INTEGER, included_in_subscription BOOLEAN, "
                       "created_at TIMESTAMP)"))
        c.execute(text("INSERT INTO entitlement_sources (project_id, source_id, provider, "
                       "connection_status, observed_capacity, cost_mode) VALUES "
                       "('p1', 'anthropic_claude_code', 'anthropic', 'connected', '{}', "
                       "'subscription')"))
    return eng


def _settings(**kw):
    from boss_engine.config import Settings

    return Settings(**kw) if kw else Settings()


def _fixed(engine="claude_code", model="sonnet"):
    return lambda: LegacyRoute(engine, model, "the balanced profile puts implement on the "
                                              "low-cost lane", "low_cost")


def test_fixed_mode_records_the_legacy_route_once(db):
    settings = _settings()
    request = RouteRequest(kind="build_stage", step="implement", project_id="p1",
                           unit_id="crun_1", legacy=_fixed())
    with db.begin() as c:
        first = store.route(c, settings, request)
        again = store.route(c, settings, request)
        rows = store.routes_for(c, "crun_1")
    assert first.mode == "fixed" and (first.engine, first.model) == ("claude_code", "sonnet")
    assert again.id == first.id and len(rows) == 1
    assert rows[0]["why"] == ["Fixed: the balanced profile puts implement on the low-cost lane"]
    assert rows[0]["tier"] == "balanced"


def test_smart_mode_is_the_project_setting(db):
    settings = _settings()
    with db.begin() as c:
        c.execute(text("INSERT INTO project_settings (project_id, key, value) "
                       "VALUES ('p1', 'model_routing_mode', 'smart')"))
    with db.begin() as c:
        d = store.route(c, settings, RouteRequest(
            kind="build_stage", step="review_code", project_id="p1", unit_id="crun_2",
            spec={"title": "", "instructions": "Build a new app."}, legacy=_fixed()))
    assert d.mode == "smart" and d.tier == "frontier" and d.model == "opus"


def test_the_clis_window_numbers_become_headroom(db):
    settings = _settings()
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.9,
        "resetsAt": 4102444800}}
    with db.begin() as c:
        patch = store.observe(c, settings, "p1", "claude_code", event=event)
    assert patch["seven_day"]["used"] == 0.9
    with db.begin() as c:
        observed = json.loads(c.execute(text(
            "SELECT observed_capacity FROM entitlement_sources")).scalar())
        snap = store.snapshot(c, settings, "p1", fresh=True)
    assert observed["windows"]["seven_day"]["status"] == "allowed_warning"
    assert snap.sources["claude_code"].headroom.state == "near_limit"


def test_a_weekly_limit_teaches_the_allowance(db):
    settings = _settings()
    from datetime import datetime, timezone

    with db.begin() as c:
        c.execute(text("INSERT INTO llm_calls VALUES ('claude_code', 'p1', 412000000, 1, :t)"),
                  {"t": datetime.now(timezone.utc)})
        store.observe(c, settings, "p1", "claude_code",
                      error="You've hit your weekly limit · resets Mon 12:00am")
        observed = json.loads(c.execute(text(
            "SELECT observed_capacity FROM entitlement_sources")).scalar())
    assert observed["learned_weekly_allowance_usd"] == 412.0
    assert observed["windows"]["seven_day"]["status"] == "rejected"


def test_a_resumed_session_keeps_its_model(db):
    settings = _settings()
    with db.begin() as c:
        c.execute(text("INSERT INTO project_settings (project_id, key, value) "
                       "VALUES ('p1', 'model_routing_mode', 'smart')"))
        c.execute(text("INSERT INTO model_routes (id, project_id, unit_kind, unit_id, step, "
                       "mode, tier, engine, model, rules, why) VALUES ('route_old', 'p1', "
                       "'agent', 'arun_1', 'agent', 'smart', 'fast', 'claude_code', 'haiku', "
                       "'[]', '[]')"))
    with db.begin() as c:
        d = store.route(c, settings, RouteRequest(
            kind="agent", step="agent", project_id="p1", unit_id="arun_1", sticky=True,
            text="Design the architecture of a distributed consensus service."))
    assert (d.engine, d.model) == ("claude_code", "haiku") and "pin:session" in d.rules


def test_an_unreadable_store_routes_as_fixed():
    broken = SimpleNamespace(execute=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
                             begin_nested=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    snap = store.snapshot(broken, _settings(), "p9", fresh=True)
    assert snap.policy.mode == "fixed"


def test_an_environment_subscription_reads_its_catalogue_rows_windows(db, monkeypatch):
    """A Claude subscription connected through CLAUDE_CODE_OAUTH_TOKEN has no connection row;
    telemetry records its windows on the catalogue row, and the snapshot must read them there."""
    from boss_engine.services.model_router.store import sources_from, _observed
    from boss_engine.services.model_router.policy import RoutingPolicy

    settings = _settings()
    event = {"rate_limit_info": {"status": "allowed_warning", "rateLimitType": "seven_day",
                                 "utilization": 0.92, "resetsAt": 4102444800}}
    with db.begin() as c:
        c.execute(text("UPDATE entitlement_sources SET connection_status = 'not_connected'"))
        store.observe(c, settings, "p1", "claude_code", event=event)
        observed = _observed(c, settings, "p1", ["claude_code"])
    env_only = {"claude_code": {"kind": "env", "env_attr": "claude_code_oauth_token",
                                "working": True}}
    sources = sources_from(env_only, RoutingPolicy(), {}, observed_by_engine=observed)
    assert sources["claude_code"].kind == "subscription"
    assert sources["claude_code"].headroom.state == "near_limit"
