"""BOSSAPP-1342: a run's limit reaches the people who govern, at once.

A run's call reports the weekly limit -> the account reads Limit reached (accounts.observe) -> ONE
`subscription.headroom_changed` event, a finding and a card with the choices are raised in the same
moment (no waiting for the doctor's tick) -> Govern's Subscriptions and Setup's headroom strip read
the same numbers -> a later success lifts it. A stale reading says how old it is.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import subscription_headroom as sh
from boss_engine.services import system_doctor as doc
from boss_engine.services.model_router import accounts, headroom, store
from boss_engine.services.model_router.view import routing_view

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", "")
LIMIT = "You've hit your weekly limit · resets Mon 06:56 AM (America/Los_Angeles)"


def _alerts(c):
    return [f for f in doc.open_findings(c) if f["kind"].startswith(sh.KIND_PREFIX)]


@pytest.fixture(scope="module")
def eng():
    if not TEST_DB.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from boss_engine.db import ensure_schema

    engine = create_engine(TEST_DB, future=True)
    ensure_schema(engine)
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'w 1342', :s)"), {"i": pid, "w": wid, "s": pid})
        c.execute(text("DELETE FROM system_findings WHERE kind LIKE 'subscription_limit:%'"))
    accounts.reset_caches()
    store.invalidate()
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM system_findings WHERE kind LIKE 'subscription_limit:%'"))
        c.execute(text("DELETE FROM subscription_accounts WHERE account_key = :k"),
                  {"k": accounts.account_key(c, Settings(), pid, "claude_code")})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        from .test_subscription_headroom_levels_and_pools_1342 import forget_alerts

        forget_alerts(c, pid)
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
    accounts.reset_caches()
    store.invalidate()


@pytest.fixture()
def claude_is_connected(monkeypatch):
    """The project has one connected Claude Code subscription (no machine login needed)."""
    real = store._connected

    def connected(connection, settings, project_id):
        out = dict(real(connection, settings, project_id) or {})
        out["claude_code"] = {**out.get("claude_code", {}), "kind": "subscription", "working": True,
                              "capacity": {}}
        return out

    monkeypatch.setattr(store, "_connected", connected)


def _source(view, engine="claude_code"):
    return next(s for s in view["sources"] if s["engine"] == engine)


def _weekly(used):
    return {"rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day",
                                "utilization": used,
                                "resetsAt": int((datetime.now(timezone.utc)
                                                 + timedelta(days=3)).timestamp())}}


def test_a_runs_limit_flips_the_account_and_raises_the_alert_event_and_card(
        eng, project, claude_is_connected):
    settings = Settings()
    with eng.begin() as c:       # the account has plenty
        store.observe(c, settings, project, "claude_code", event=_weekly(0.2))
        assert _source(sh.project_view(c, settings, project))["state"] == "plenty"
        assert not _alerts(c)
    with eng.begin() as c:       # a run's call comes back with the limit sentence
        store.observe(c, settings, project, "claude_code", error=LIMIT)
    with eng.begin() as c:
        view = sh.govern_payload(c, settings, project)
        src = _source(view)
        # Govern: Limit reached, with the weekly window, the reset and what BOSS will do.
        assert (src["state"], src["state_label"]) == ("exhausted", "Limit reached")
        assert src["left_pct"] == 0 and src["resets_at"]
        assert {w["kind"]: w["state"] for w in src["windows"]}["seven_day"] == "exhausted"
        assert src["at_limit"]["words"]
        # The alert: a finding in Govern's payload, and a CARD with options in the Command Center.
        assert [a["severity"] for a in view["alerts"]] == ["critical"]
        cards = [i for i in doc.card_items(c, project)
                 if i["finding_kind"].startswith(sh.KIND_PREFIX)]
        assert len(cards) == 1 and cards[0]["title"].endswith("limit reached")
        assert {"limit_see_govern", "limit_open_setup"} <= set(cards[0]["actions"])
        # ONE event says it, with what BOSS will do.
        events = c.execute(text("SELECT data FROM events WHERE project_id = :p AND event_type = "
                                "'subscription.headroom_changed'"), {"p": project}).all()
        assert len(events) == 1
        assert events[0][0]["state"] == "exhausted" and events[0][0]["boss_will"]
        # Setup's headroom strip reads the same account: same state, same reset.
        strip = next(h for h in routing_view(c, settings, project)["headroom"]
                     if h["engine"] == "claude_code")
        assert (strip["state"], strip["label"]) == ("exhausted", "Limit reached")
        assert strip["resets_at"] == src["resets_at"]
    # The same limit again (every failing call repeats it) is not a second event.
    with eng.begin() as c:
        store.observe(c, settings, project, "claude_code", error=LIMIT)
        n = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND event_type = "
                           "'subscription.headroom_changed'"), {"p": project}).scalar()
    assert n == 1
    # A success proves the window wrong: the alert lifts at once.
    with eng.begin() as c:
        store.observe(c, settings, project, "claude_code", ok_model="sonnet")
    with eng.begin() as c:
        assert _source(sh.govern_payload(c, settings, project))["state"] == "plenty"
        assert not _alerts(c)


def test_an_account_under_20_percent_is_a_banner_not_a_card(eng, project, claude_is_connected):
    settings = Settings()
    with eng.begin() as c:
        store.observe(c, settings, project, "claude_code", event=_weekly(0.85))
        found = [f for f in sh.findings_for(sh.install_accounts(c))
                 if f["evidence"]["account_key"] == accounts.account_key(c, settings, project,
                                                                         "claude_code")]
        alerts = _alerts(c)
    assert len(found) == 1 and found[0]["severity"] == "info" and not found[0]["needs_choice"]
    assert alerts and alerts[0]["severity"] == "info"


def test_a_stale_reading_says_how_old_it_is_instead_of_plenty():
    now = datetime.now(timezone.utc)
    resets = (now + timedelta(days=2)).isoformat()
    old = headroom.assess("claude_code", {"windows": {"seven_day": {
        "used": 0.1, "status": "allowed", "resets_at": resets,
        "observed_at": (now - timedelta(hours=5)).isoformat()}}}, thresholds={}, now=now)
    fresh = old.freshness(now)
    assert old.state == "plenty" and fresh["stale"] and fresh["age_label"] == "5 h ago"
    assert old.to_dict()["stale"] is True
    recent = headroom.assess("claude_code", {"windows": {"seven_day": {
        "used": 0.1, "status": "allowed", "resets_at": resets,
        "observed_at": (now - timedelta(minutes=3)).isoformat()}}}, thresholds={}, now=now)
    assert not recent.freshness(now)["stale"]
