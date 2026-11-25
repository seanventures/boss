"""BOSSAPP-1343, the independent review's findings, each pinned.

S1 removing (or re-logging) the adopted account 1 never deletes or overwrites the install's own
   login. M1 every seat at its limit is a capacity limit with its reset (the at-limit path), and
   the router reads the pool's seat. M4 "Add another account" never cancels another sign-in nor
   takes the "already signed in" shortcut. M5 a deleted project's org pool moves to another
   project of the org; a pool that goes deletes its seats' logins; one primary per pool.
M6 an adopted install login whose login is refused takes no new work.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from boss_engine.config import Settings
from boss_engine.services import subscription_pools as sp
from boss_engine.services import subscription_sign_in as si

from .test_several_claude_accounts_1343 import _add, _drop, _install_login, _prove, _two_projects
from .test_subscription_pools_886 import (  # noqa: F401 — fixtures shared on purpose
    TOKEN_A, TOKEN_B, _windows, eng, project, store)


def _pool_of_two(eng, project_id, store):
    _install_login(store)
    with eng.begin() as c:
        _add(eng, c, project_id, TOKEN_B)
    with eng.begin() as c:
        a, b = sp.get_pool(c, project_id, "claude_code")["members"]
    assert _prove(eng, project_id, b["id"])["ok"]
    return a, b


def test_removing_the_adopted_account_1_keeps_the_install_login(eng, project, store):
    a, b = _pool_of_two(eng, project, store)
    assert a["secret_name"] == sp.BASE_SECRET["claude_code"]
    with eng.begin() as c:
        with pytest.raises(sp.PoolError, match="install's own login"):
            sp.update_member(c, Settings(), project, a["id"], actor="o",
                             credential="sk-ant-oat01-" + "z" * 48)
    assert TOKEN_A in store.held.values(), "a pool row never overwrites the install login"
    with eng.begin() as c:
        sp.remove_member(c, Settings(), project, a["id"], actor="o")
        sp.remove_member(c, Settings(), project, b["id"], actor="o")
    assert TOKEN_A in store.held.values(), "the install-wide login stays"
    assert TOKEN_B not in store.held.values(), "a seat's own login goes with it"


def test_every_seat_at_its_limit_is_a_capacity_limit_with_its_reset(eng, project, store):
    from boss_engine.services import harness_failures, provider_limits

    a, b = _pool_of_two(eng, project, store)
    soon = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=3)
    with eng.begin() as c:
        _windows(c, a, rejected_until=soon)
        # the router reads the pool's seat with room, not the install login's windows
        assert sp.router_account(c, Settings(), project, "claude_code") == b["account_key"]
        _windows(c, b, rejected_until=soon + timedelta(hours=1))
        assert sp.router_account(c, Settings(), project, "claude_code") == a["account_key"], \
            "all at the limit: the seat that resets first"
        with pytest.raises(sp.PoolExhausted) as full:
            sp.assign(c, project, "claude_code", "wfr_full")
    signal = provider_limits.classify_limit(str(full.value))
    assert signal is not None and signal.exhausted
    assert signal.retry_at == soon == full.value.retry_at
    # not a harness failure that stops the build for a person
    assert harness_failures.classify(str(full.value), engine="claude_code") is None


def test_the_router_reads_the_pool_seat_in_its_snapshot(eng, project, store):
    from boss_engine.services.model_router import store as router_store

    a, b = _pool_of_two(eng, project, store)
    with eng.begin() as c:
        _windows(c, a, used=0.9)
        _windows(c, b, used=0.1)
        got = router_store._subscription_windows(c, Settings(), project, ["claude_code"])
    assert got["claude_code"][0] == b["account_key"]


def test_add_another_account_never_cancels_a_sign_in_nor_shortcuts(monkeypatch):
    monkeypatch.setattr(si, "_SESSIONS", {})
    missing = ("boss-no-such-cli-1343",)
    # with the CLI "signed in", a plain sign-in answers signed_in; adding still starts a login
    plain = si.start("claude_code", project_id="p1", actor="o", org_id="org_a",
                     signed_in=lambda: True, argv=missing)
    assert plain["state"] == "signed_in"
    added = si.start("claude_code", project_id="p1", actor="o", org_id="org_a",
                     signed_in=lambda: True, argv=missing, add_account=True, fresh=True)
    assert added["state"] == "failed", ("no shortcut: it tried to start a login", added)
    # a plain sign-in in progress is neither cancelled nor handed to "add"
    live = si._Session(sign_in_id="si_live", provider="claude_code", project_id="p1",
                       actor="o", started=__import__("time").time(), expires=None,
                       state="waiting", org_id="org_a")
    monkeypatch.setattr(si, "_SESSIONS", {"si_live": live})
    with pytest.raises(si.SignInError) as busy:
        si.start("claude_code", project_id="p1", actor="o", org_id="org_a",
                 signed_in=lambda: True, argv=missing, add_account=True)
    assert busy.value.status == 409
    assert live.state == "waiting"


def test_a_deleted_projects_org_pool_moves_and_a_dropped_pool_deletes_its_logins(eng, store):
    one, two = _two_projects(eng)
    try:
        _install_login(store)
        with eng.begin() as c:
            _add(eng, c, one, TOKEN_B)                      # the org's pool, anchored on one
            org_pool = sp.get_pool(c, one, "claude_code", scope="org")
            own = sp.create_pool(c, two, engine="claude_code", name="own", actor="o")
            sp.add_member(c, Settings(), two, own["id"], label="Mine",
                          credential="sk-ant-oat01-" + "m" * 48, actor="o")
            anchored = c.execute(text("SELECT project_id FROM subscription_pools WHERE id = :i"),
                                 {"i": org_pool["id"]}).scalar()
            # the org's pool lives on the org's model home (whichever project that is)
            out = sp.before_project_purge(c, Settings(), anchored)
            assert out["moved"] == 1
            moved_to = c.execute(text("SELECT project_id FROM subscription_pools WHERE id = :i"),
                                 {"i": org_pool["id"]}).scalar()
            assert moved_to and moved_to != anchored
            assert sp.get_pool(c, one, "claude_code", scope="org")["id"] == org_pool["id"]
            # back where it was, so the shared database is left as found
            c.execute(text("UPDATE subscription_pools SET project_id = :p WHERE id = :i"),
                      {"p": anchored, "i": org_pool["id"]})
        # the project-scoped pool of a deleted project takes its seat's login with it
        with eng.begin() as c:
            out = sp.before_project_purge(c, Settings(), two)
        assert out["logins_deleted"] >= 1
        assert "sk-ant-oat01-" + "m" * 48 not in store.held.values()
        assert TOKEN_A in store.held.values(), "the adopted install login is never deleted"
    finally:
        _drop(eng, [one, two])


def test_one_primary_per_pool_is_enforced_by_the_database(eng, project, store):
    a, b = _pool_of_two(eng, project, store)
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text("UPDATE subscription_pool_members SET is_primary = TRUE "
                           "WHERE id = :i"), {"i": b["id"]})


def test_a_refused_install_login_takes_no_new_work(eng, project, store, monkeypatch):
    a, b = _pool_of_two(eng, project, store)
    with eng.begin() as c:
        assert sp.assign(c, project, "claude_code", "wfr_m6", settings=Settings()).member_id \
            == a["id"], "the primary leads while its login works"
    monkeypatch.setattr(sp, "install_login_refused", lambda *a_, **k: True)
    with eng.begin() as c:
        assert sp.assign(c, project, "claude_code", "wfr_m6b", settings=Settings()).member_id \
            == b["id"]
        assert sp.router_account(c, Settings(), project, "claude_code") == b["account_key"]
