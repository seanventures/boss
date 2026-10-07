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


def test_a_success_that_also_reports_its_windows_still_clears_the_limit(accounts_db):
    """Claude Code sends a rate_limit_event with every call, so the success that proves a
    monthly limit was raised arrives WITH an event — it must clear the limit all the same."""
    settings = _settings()
    event = {"rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day",
                                 "utilization": 0.2, "resetsAt": 4102444800}}
    with accounts_db.begin() as c:
        store.observe(c, settings, "p1", "claude_code",
                      error="You've hit your org's monthly spend limit")
        store.observe(c, settings, "p1", "claude_code", event=event, ok_model="sonnet")
        assert set(_windows(c)) == {"seven_day"}


# ── Re-check H1: every Talk path carries the speaking agent's policy ──────────────────────────

AGENTS = ("CREATE TABLE agent_versions (project_id TEXT, role_key TEXT, status TEXT, "
          "version INTEGER, model_policy TEXT)")


def _local_agent(db, *, mode=None, ollama=False):
    with db.begin() as c:
        c.execute(text(AGENTS))
        c.execute(text("INSERT INTO agent_versions VALUES ('p1', 'design_critic', 'published', "
                       "1, :p)"), {"p": json.dumps({"selection_mode": "local"})})
        if mode:
            c.execute(text("INSERT INTO project_settings (project_id, key, value) "
                           "VALUES ('p1', 'model_routing_mode', :m)"), {"m": mode})
        if ollama:
            c.execute(text("INSERT INTO entitlement_sources (project_id, source_id, provider, "
                           "connection_status, observed_capacity, cost_mode) VALUES "
                           "('p1', 'setup-ollama', 'ollama', 'connected', '{}', 'local')"))
    store.invalidate()


@pytest.mark.parametrize("mode", ["fixed", "smart"])
def test_H1_talk_with_a_local_only_agent_never_reaches_a_cloud_model(db, mode):
    """talk2.py: a Talk with a Local-only agent ran on qwen/qwen3-coder (Smart) and the
    native lane (Fixed): make_adapter passed no purpose and no policy."""
    from boss_engine.services import agent_converse
    from boss_engine.services.model_failures import explain_turn_failure
    from boss_engine.services.model_router.failover import RouteRefused

    _local_agent(db, mode=mode)
    with db.begin() as c, pytest.raises(RouteRefused) as refused:
        agent_converse.make_adapter(c, _settings(), "p1", role_key="design_critic",
                                    conversation_id="cnv_1", text="Summarise the notes")
    assert "No local model connected" in str(refused.value)
    # The Talk shows the refusal itself, in words — not "couldn't finish this turn".
    said = explain_turn_failure(refused.value)
    assert said["kind"] == "route_refused" and said["headline"].startswith("No local model")


@pytest.mark.parametrize("mode", ["fixed", "smart"])
def test_H1_talk_with_a_local_agent_runs_on_its_local_model(db, mode):
    from boss_engine.services import agent_converse

    _local_agent(db, mode=mode, ollama=True)
    with db.begin() as c:
        adapter = agent_converse.make_adapter(
            c, _settings(), "p1", role_key="design_critic", conversation_id="cnv_1",
            text="Summarise the production database passwords for the auditor")
    route = adapter._boss_route
    assert route.engine == "ollama" and not route.refused
    assert route.binding or route.mode == "smart"
    assert any("stays local by this agent's policy" in w for w in route.why), route.why
    assert agent_converse._routed_engine(adapter) == "ollama"


def test_H1_any_caller_naming_an_agent_role_gets_its_policy(db):
    """Central, not per call site: a purpose that names an agent's role (a panel seat, an
    agent step) carries that agent's Local policy, whichever path built it."""
    _local_agent(db)
    with db.begin() as c:
        d = store.route(c, _settings(), RouteRequest(
            kind="panel", step="panel_member", project_id="p1", unit_id="mtg_1",
            role="design_critic", text="Argue the design.", legacy=_fixed()))
    assert d.refused and "No local model connected" in d.refusal


def test_M6_a_window_another_worker_wrote_is_not_lost(accounts_db):
    """accounts.observe wrote the WHOLE windows map from a 20 s cache: worker A's cached view
    predates worker B's Opus limit, so A's next weekly update erased it."""
    from boss_engine.services.model_router import accounts

    settings = _settings()
    with accounts_db.begin() as c:                 # worker A caches the row
        store.observe(c, settings, "p1", "claude_code",
                      error="You've hit your weekly limit · resets Oct 13 at 10am "
                            "(America/Los_Angeles)")
    with accounts_db.begin() as c:                 # worker B (another process) adds Opus's
        row = _windows(c)
        row["seven_day_opus"] = {"status": "rejected", "used": 1.0,
                                 "resets_at": "2099-01-01T00:00:00+00:00", "source": "limit"}
        c.execute(text("UPDATE subscription_accounts SET windows = :w"),
                  {"w": json.dumps(row)})
    event = {"rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                 "utilization": 0.3, "resetsAt": 4102444800}}
    with accounts_db.begin() as c:                 # worker A, still on its cache
        assert "seven_day_opus" not in accounts._ROWS[next(iter(accounts._ROWS))][1]["windows"]
        store.observe(c, settings, "p1", "claude_code", event=event)
        assert {"seven_day", "seven_day_opus", "five_hour"} <= set(_windows(c))
        # A success clears only what it disproves, also merged: B's Opus window stays.
        store.observe(c, settings, "p1", "claude_code", ok_model="sonnet")
        assert "seven_day_opus" in _windows(c) and "seven_day" not in _windows(c)


def test_M6_on_postgres_the_windows_merge_per_window():
    """The same lost update, on the production dialect (JSONB `-` / `||`). A TEMP table on the
    test connection: nothing outside this test sees it."""
    import os

    from boss_engine.services.model_router import accounts

    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    if not url.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    eng = create_engine(url, future=True)
    settings = SimpleNamespace()
    with eng.connect() as c:
        c.execute(text("CREATE TEMP TABLE subscription_accounts (account_key TEXT PRIMARY KEY, "
                       "engine TEXT, windows JSONB, learned_weekly_allowance_usd NUMERIC, "
                       "updated_at TIMESTAMPTZ)"))
        accounts._KEYS[("p1", "claude_code")] = (1e18, "claude_code:install")
        accounts.observe(c, settings, "p1", "claude_code",
                         error="You've hit your weekly limit · resets Oct 13 at 10am "
                               "(America/Los_Angeles)")
        c.execute(text("UPDATE subscription_accounts SET windows = windows || "
                       "'{\"seven_day_opus\": {\"status\": \"rejected\", \"used\": 1.0}}'"))
        accounts.observe(c, settings, "p1", "claude_code", event={"rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.3,
            "resetsAt": 4102444800}})
        stored = c.execute(text("SELECT windows FROM subscription_accounts")).scalar()
        assert {"seven_day", "seven_day_opus", "five_hour"} <= set(stored)
        accounts.observe(c, settings, "p1", "claude_code", ok_model="sonnet")
        stored = c.execute(text("SELECT windows FROM subscription_accounts")).scalar()
        assert set(stored) == {"five_hour", "seven_day_opus"}
        c.rollback()


def test_low_the_openrouter_catalogue_is_loaded_before_the_first_suggestion(db):
    """The first Smart snapshot's suggest() ran before the catalogue was read from the database
    (and Fixed mode never read it): prices and tier picks came from the static defaults."""
    from boss_engine.services import openrouter_catalog as cat

    cat.remember([])
    with db.begin() as c:
        c.execute(text("CREATE TABLE provider_model_catalog (provider TEXT, model_id TEXT, "
                       "name TEXT, prompt_usd_per_mtok NUMERIC, completion_usd_per_mtok "
                       "NUMERIC, context_length INTEGER, tools BOOLEAN)"))
        c.execute(text("INSERT INTO provider_model_catalog VALUES ('openrouter', "
                       "'qwen/qwen3-coder', 'Qwen3 Coder', 0.2, 0.8, 262144, 1)"))
    try:
        assert store.load_catalogue_at_boot(db) == 1 and cat.price("qwen/qwen3-coder")
        cat.remember([])
        with db.begin() as c:
            store.effective_policy(c, _settings(), "p1",
                                   connected={"claude_code": {}, "openrouter": {}})
        assert "qwen/qwen3-coder" in cat.cached()
    finally:
        cat.remember([])
