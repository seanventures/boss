"""BOSSAPP-1342 (with 1343's pools): warnings at 20, 10 and 5 percent, through the usual channels,
and every account of a pool in Govern and the Command Center header.

An account's week falls to 20% -> a banner finding (info) and ONE BOSS self-check alert in the
project that uses it (notify's meta route: the console and the owner's email) -> 10% (warn) and
5% (critical) are new messages and the level left behind resolves -> at the limit, a card -> back
above 20% everything resolves. A pool's accounts each show in Govern under the pool, and the one
marked "takes new work" is the one `subscription_pools.assign` gives the next build.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import subscription_headroom as sh
from boss_engine.services import subscription_pools as sp
from boss_engine.services.model_router import store as router_store

from .test_subscription_pools_886 import (  # noqa: F401 — fixtures shared on purpose
    _pool, _windows, eng, project, store)


def _acct(pct, state="plenty"):
    return {"account_key": "claude_code:pool:x", "engine": "claude_code", "label": "Claude Code · seat",
            "left_pct": pct, "state": state, "state_label": state, "resets_at": None}


def test_the_levels_are_20_10_5_and_the_limit_each_its_own_severity():
    assert [sh.alert_level(_acct(p)) for p in (100, 21, 20, 11, 10, 6, 5, 1)] == \
        [None, None, 20, 20, 10, 10, 5, 5]
    assert sh.alert_level(_acct(0, "exhausted")) == sh.LIMIT
    assert sh.alert_level(_acct(None)) is None
    found = {f["evidence"]["state"]: f for f in
             sh.findings_for([_acct(18), _acct(9), _acct(3), _acct(0, "exhausted")])}
    assert {k: (f["severity"], f["needs_choice"]) for k, f in found.items()} == {
        "level_20": ("info", False), "level_10": ("warn", False),
        "level_5": ("critical", False), "level_limit": ("critical", True)}
    # a router "Tight" account with 30% left is not yet a warning
    assert sh.findings_for([_acct(30, "tight")]) == []


def _firing(c, pid):
    return sorted(r[0] for r in c.execute(text(
        "SELECT alertname FROM app_alerts WHERE project_id = :p AND state = 'firing' "
        "AND labels->>'boss_meta' = 'true'"), {"p": pid}))


def _mine(c, member):
    return [a for a in sh.install_accounts(c) if a["account_key"] == member["account_key"]]


def forget_alerts(c, pid):
    """The self-check alerts' rows (alerts, their outbox, the self-check service) before the
    project goes: monitored_services holds a plain foreign key to it."""
    for table in ("notifications", "notification_groups", "alert_transitions", "app_alerts",
                  "monitored_services", "contact_points"):
        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})


_cleanup = forget_alerts


def test_each_level_is_told_once_through_the_usual_channels(eng, project, store):
    try:
        with eng.begin() as c:
            _pool_, (seat, _other) = _pool(c, project)
            _windows(c, seat, used=0.85)                       # 15% left: the 20% level
            assert sh.notify_channels(c, _mine(c, seat)) == 1
            assert _firing(c, project) == ["Claude Code · seat 0: 20% or less of the week left"]
            assert sh.notify_channels(c, _mine(c, seat)) == 0, "the same level is told once"
            # the usual channels: the outbox gets a message for the project's console
            from boss_engine.services import notify

            assert notify.plan(c, Settings(), project)["enqueued"] >= 1
            sent = c.execute(text(
                "SELECT n.subject FROM notifications n JOIN contact_points p ON "
                "p.id = n.contact_point_id WHERE n.project_id = :p AND p.kind = 'console'"),
                {"p": project}).scalars().all()
            assert sent and any("20%" in s for s in sent)

            _windows(c, seat, used=0.92)                       # 8% left: 10% replaces 20%
            assert sh.notify_channels(c, _mine(c, seat)) == 1
            assert _firing(c, project) == ["Claude Code · seat 0: 10% or less of the week left"]
            _windows(c, seat, used=0.97)                       # 3% left
            sh.notify_channels(c, _mine(c, seat))
            assert _firing(c, project) == ["Claude Code · seat 0: 5% or less of the week left"]
            _windows(c, seat, used=0.4)                        # room again: all resolve
            assert sh.notify_channels(c, _mine(c, seat)) == 0
            assert _firing(c, project) == []
    finally:
        with eng.begin() as c:
            _cleanup(c, project)


def test_a_pools_accounts_show_in_govern_and_the_one_taking_new_work_is_the_routers(
        eng, project, store, monkeypatch):
    real = router_store._connected

    def connected(connection, settings, project_id):
        out = dict(real(connection, settings, project_id) or {})
        out["claude_code"] = {**out.get("claude_code", {}), "kind": "subscription",
                              "working": True, "capacity": {}}
        return out

    monkeypatch.setattr(router_store, "_connected", connected)
    router_store.invalidate()
    with eng.begin() as c:
        pool, (a, b) = _pool(c, project)
        _windows(c, a, used=0.9)                              # seat 0 is low, seat 1 has room
        _windows(c, b, used=0.2)
        view = sh.project_view(c, Settings(), project)
        seats = [s for s in view["sources"] if s["engine"] == "claude_code"]
        assert [s["key"] for s in seats] == [a["account_key"], b["account_key"]]
        assert all(s["pool"]["id"] == pool["id"] and s["pool"]["size"] == 2 for s in seats)
        assert [s["left_pct"] for s in seats] == [10, 80]
        assert seats[0]["windows"] and seats[0]["windows"][0]["kind"] == "seven_day"
        given = sp.assign(c, project, "claude_code", "wfr_gov")
        assert [s["taking_new_work"] for s in seats] == [s["key"] == given.account_key for s in seats]
        assert seats[1]["taking_new_work"]
        # seat 0's limit moves work to seat 1, in words
        assert "seat 1" in seats[0]["at_limit"]["words"]
        # the Command Center header: the same accounts, worst first
        chip = sh.header_chip(c, Settings(), project)
        keys = [x["key"] for x in chip["accounts"]]
        assert keys.index(a["account_key"]) < keys.index(b["account_key"])
        first = next(x for x in chip["accounts"] if x["key"] == a["account_key"])
        assert first["left_pct"] == 10 and first["pool"] == pool["name"]
        # a paused pool hands back to the single login: Govern shows no seats
        sp.set_pool_status(c, project, pool["id"], status="disabled", actor="o")
        after = sh.project_view(c, Settings(), project)
        assert not any(s.get("pool") for s in after["sources"])
    router_store.invalidate()
