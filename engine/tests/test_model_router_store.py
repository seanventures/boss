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


ACCOUNTS = ("CREATE TABLE subscription_accounts (account_key TEXT PRIMARY KEY, engine TEXT, "
            "windows TEXT, learned_weekly_allowance_usd NUMERIC, updated_at TIMESTAMP)")


@pytest.fixture()
def accounts_db(db):
    with db.begin() as c:
        c.execute(text(ACCOUNTS))
    return db


def _windows(c) -> dict:
    raw = c.execute(text("SELECT windows FROM subscription_accounts")).scalar()
    return json.loads(raw) if raw else {}


def test_the_clis_window_numbers_become_headroom(accounts_db):
    settings = _settings()
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.9,
        "resetsAt": 4102444800}}
    with accounts_db.begin() as c:
        patch = store.observe(c, settings, "p1", "claude_code", event=event)
    assert patch["seven_day"]["used"] == 0.9
    with accounts_db.begin() as c:
        assert _windows(c)["seven_day"]["status"] == "allowed_warning"
        snap = store.snapshot(c, settings, "p1", fresh=True)
    assert snap.sources["claude_code"].headroom.state == "near_limit"


def test_a_weekly_limit_teaches_the_allowance(accounts_db):
    settings = _settings()
    from datetime import datetime, timezone

    with accounts_db.begin() as c:
        c.execute(text("INSERT INTO llm_calls VALUES ('claude_code', 'p1', 412000000, 1, :t)"),
                  {"t": datetime.now(timezone.utc)})
        store.observe(c, settings, "p1", "claude_code",
                      error="You've hit your weekly limit · resets Mon 12:00am")
        row = c.execute(text("SELECT learned_weekly_allowance_usd FROM subscription_accounts")
                        ).scalar()
        assert float(row) == 412.0
        assert _windows(c)["seven_day"]["status"] == "rejected"


def test_one_projects_limit_is_every_projects_limit(accounts_db):
    """Two projects on one Claude login share one week (Decision 14)."""
    settings = _settings()
    with accounts_db.begin() as c:
        c.execute(text("INSERT INTO entitlement_sources (project_id, source_id, provider, "
                       "connection_status, observed_capacity, cost_mode) VALUES ('p2', "
                       "'anthropic_claude_code', 'anthropic', 'connected', '{}', 'subscription')"))
        store.observe(c, settings, "p1", "claude_code",
                      error="Weekly limit reached ∙ resets Oct 13, 10am")
    with accounts_db.begin() as c:
        p2 = store.snapshot(c, settings, "p2", fresh=True)
    assert p2.sources["claude_code"].headroom.state == "exhausted"


def test_a_later_success_clears_the_rejected_window(accounts_db):
    """Live, not sticky: the monthly limit was lifted (the operator raised it); the next call
    succeeds, and the source is usable again at once."""
    settings = _settings()
    with accounts_db.begin() as c:
        store.observe(c, settings, "p1", "claude_code",
                      error="You've hit your org's monthly spend limit")
        store.observe(c, settings, "p1", "claude_code",
                      error="You've hit your Opus limit · resets 9pm")
        assert {"monthly", "seven_day_opus"} <= set(_windows(c))
        cleared = store.observe(c, settings, "p1", "claude_code", ok_model="sonnet")
        assert set(cleared) == {"monthly"}            # a Sonnet success says nothing of Opus
        assert set(_windows(c)) == {"seven_day_opus"}
        snap = store.snapshot(c, settings, "p1", fresh=True)
    assert snap.sources["claude_code"].headroom.state != "exhausted"


def test_an_unchanged_rate_limit_event_is_not_written_again(accounts_db):
    settings = _settings()
    event = {"rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day",
                                 "utilization": 0.50, "resetsAt": 4102444800}}
    nudge = {"rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day",
                                 "utilization": 0.52, "resetsAt": 4102444800}}
    writes = []
    from sqlalchemy import event as sa_event

    sa_event.listen(accounts_db, "before_cursor_execute",
                    lambda *a: writes.append(a[2]) if "subscription_accounts (" in a[2] else None)
    with accounts_db.begin() as c:
        store.observe(c, settings, "p1", "claude_code", event=event)
        for _ in range(5):
            assert store.observe(c, settings, "p1", "claude_code", event=nudge) == {}
    assert len([w for w in writes if w.lstrip().upper().startswith("INSERT")]) == 1


def test_a_resumed_session_keeps_its_model(accounts_db):
    db = accounts_db
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
