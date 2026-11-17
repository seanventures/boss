"""BOSSAPP-1343: more than one Claude subscription.

Signing in again ADDS an account (the install's login becomes account 1, nothing is replaced) ->
each account has its own label, room and primary mark -> the first account hitting its limit moves
the task onto the next with headroom and the move is said (reason + event) -> both stay listed ->
removing one removes only it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import subscription_pools as sp
from boss_engine.services.llm_telemetry import set_llm_context

from .test_subscription_pools_886 import (  # noqa: F401 — fixtures shared on purpose
    TOKEN_A, TOKEN_B, _windows, eng, project, store)


def _runner(db, settings, project_id, engine, actor, *, prove, **_):
    return prove()


def _prove(eng, project, member_id, ok=True, error="refused"):
    return sp.prove_member(eng, Settings(), project, member_id, "o",
                           prove=lambda e, c: {"ok": ok, "error": None if ok else error},
                           proof_runner=_runner)


def _add(eng, c, project, token, **kw):
    """Sign-in adds a member (out of routing), then its Connect proof lets it in."""
    m = sp.add_signed_in_account(c, Settings(), project, "claude_code", token, actor="o", **kw)
    return m


def _install_login(store, token=TOKEN_A):
    from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from boss_engine.services.secrets_service import ref_key_for

    store.put(ref_key_for(SUBSCRIPTION_SECRET_SCOPE, "CLAUDE_CODE_OAUTH_TOKEN"), token)


def test_signing_in_again_adds_and_the_install_login_is_account_1(eng, project, store):
    _install_login(store)
    with eng.begin() as c:
        second = _add(eng, c, project, TOKEN_B)
        pool = sp.get_pool(c, project, "claude_code")
    labels = [m["label"] for m in pool["members"]]
    assert labels == ["Account 1", "Account 2"] and second["label"] == "Account 2"
    one, two = pool["members"]
    assert one["primary"] and not two["primary"]
    assert one["account_key"].endswith(":install") or ":ref:" in one["account_key"] \
        or ":tok:" in one["account_key"]
    assert one["secret_name"] == "CLAUDE_CODE_OAUTH_TOKEN"      # the SAME store key, not a copy
    assert TOKEN_A in store.held.values() and TOKEN_B in store.held.values()
    # a third sign-in still adds
    with eng.begin() as c:
        _add(eng, c, project, "sk-ant-oat01-" + "c" * 48)
        assert len(sp.get_pool(c, project, "claude_code")["members"]) == 3


def test_adoption_is_idempotent_and_needs_a_stored_login(eng, project, store):
    with eng.begin() as c:
        sp.create_pool(c, project, engine="claude_code", name="", actor="o")
        assert sp.adopt_install_login(c, Settings(), project, "claude_code") is None
    _install_login(store)
    with eng.begin() as c:
        assert sp.adopt_install_login(c, Settings(), project, "claude_code")["label"] == "Account 1"
        assert sp.adopt_install_login(c, Settings(), project, "claude_code") is None


def test_first_hits_its_limit_work_moves_to_the_second_both_stay_listed(eng, project, store):
    _install_login(store)
    with eng.begin() as c:
        _add(eng, c, project, TOKEN_B)
    with eng.begin() as c:
        pool = sp.get_pool(c, project, "claude_code")
    assert _prove(eng, project, pool["members"][1]["id"])["ok"]
    with eng.begin() as c:
        pool = sp.get_pool(c, project, "claude_code")
        a, b = pool["members"]
        first = sp.assign(c, project, "claude_code", "wfr_1")
        assert first.member_id == a["id"] and first.reason == "", "the primary leads"
        soon = datetime.now(timezone.utc) + timedelta(hours=3)
        _windows(c, a, rejected_until=soon)                   # 429 / limit on account 1
        moved = sp.assign(c, project, "claude_code", "wfr_1")
        assert moved.member_id == b["id"]
        assert "moved off Account 1" in moved.reason and "Account 2" in moved.reason
        ev = c.execute(text("SELECT 1 FROM events WHERE project_id = :p AND "
                            "event_type = 'subscription_pool.moved'"), {"p": project}).all()
        assert len(ev) == 1
        again = sp.assign(c, project, "claude_code", "wfr_1")
        assert again.member_id == b["id"] and again.pinned and again.reason == ""
        listed = sp.describe(c, project)[0]["members"]
        assert [m["label"] for m in listed] == ["Account 1", "Account 2"]
        assert listed[0]["headroom"]["state"] == "exhausted"
        assert listed[1]["headroom"]["state"] == "plenty"
    # a run's context carries the reason, and its token is the second account's
    set_llm_context(project_id=project, run_id="wfr_1", workflow_run_id="wfr_1", db_engine=eng)
    from boss_engine.services.llm_telemetry import get_llm_context

    assert sp.assignment_for("claude_code", Settings()).member_id == b["id"]
    assert get_llm_context().get("pool_account") == b["account_key"]


def test_primary_moves_rename_and_remove_only_that_one(eng, project, store):
    _install_login(store)
    with eng.begin() as c:
        _add(eng, c, project, TOKEN_B)
    with eng.begin() as c:
        a, b = sp.get_pool(c, project, "claude_code")["members"]
    assert _prove(eng, project, b["id"])["ok"]
    with eng.begin() as c:
        out = sp.update_member(c, Settings(), project, b["id"], actor="o", primary=True,
                               label="Bridge $100")
        assert out["primary"] and out["label"] == "Bridge $100"
        a2, b2 = sp.get_pool(c, project, "claude_code")["members"]
        assert not a2["primary"] and b2["primary"]
        assert sp.assign(c, project, "claude_code", "wfr_9").member_id == b["id"]
        with pytest.raises(sp.PoolError):
            sp.update_member(c, Settings(), project, b["id"], actor="o", label="account 1")
        sp.remove_member(c, Settings(), project, b["id"], actor="o")
        left = sp.get_pool(c, project, "claude_code")["members"]
    assert [m["id"] for m in left] == [a["id"]]
    assert TOKEN_A in store.held.values() and TOKEN_B not in store.held.values()
    assert "secret" not in json.dumps(sp.describe.__doc__ or "")


def test_a_failed_proof_shows_on_the_row_and_never_joins_routing(eng, project, store):
    _install_login(store)
    with eng.begin() as c:
        b = _add(eng, c, project, TOKEN_B)
        assert b["status"] == "needs_reauth", "a new account waits for its proof"
    out = _prove(eng, project, b["id"], ok=False, error="the provider refused this account")
    assert not out["ok"]
    with eng.begin() as c:
        listed = sp.describe(c, project)[0]["members"]
        assert listed[1]["status"] == "needs_reauth"
        assert listed[1]["proof"]["ok"] is False and "refused" in listed[1]["proof"]["error"]
        for ref in ("t1", "t2", "t3"):
            assert sp.assign(c, project, "claude_code", ref).member_id == listed[0]["id"]
    assert _prove(eng, project, b["id"])["ok"]                 # a later pass lets it in
    with eng.begin() as c:
        assert sp.get_pool(c, project, "claude_code")["members"][1]["status"] == "active"


def test_a_refused_member_leaves_rotation_for_every_launcher(eng, project, store):
    _install_login(store)
    with eng.begin() as c:
        b = _add(eng, c, project, TOKEN_B)
    assert _prove(eng, project, b["id"])["ok"]
    with eng.begin() as c:
        a, b = sp.get_pool(c, project, "claude_code")["members"]
    set_llm_context(project_id=project, run_id="crun_r", db_engine=eng, pool_account=b["account_key"])
    from boss_engine.services import harness_failures as hf

    hf.trip(hf.HarnessVerdict(engine="claude_code", kind="auth_refused", message="401",
                              fix="sign in", where="engine"))
    with eng.begin() as c:
        status = {m["label"]: m["status"] for m in sp.get_pool(c, project, "claude_code")["members"]}
    assert status == {"Account 1": "active", "Account 2": "needs_reauth"}


def _two_projects(eng):
    import uuid

    ids = [f"p_{uuid.uuid4().hex[:10]}" for _ in range(2)]
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        for pid in ids:
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:i, :w, 'org pool', :i)"), {"i": pid, "w": wid})
    return ids


def _drop(eng, ids):
    with eng.begin() as c:
        c.execute(text("DELETE FROM subscription_accounts WHERE account_key LIKE '%:pool:%'"
                       " AND account_key IN (SELECT m.account_key FROM subscription_pool_members m "
                       "JOIN subscription_pools s ON s.id = m.pool_id WHERE s.project_id = ANY(:p))"),
                  {"p": ids})
        c.execute(text("DELETE FROM subscription_pools WHERE project_id = ANY(:p)"), {"p": ids})
        c.execute(text("DELETE FROM events WHERE project_id = ANY(:p)"), {"p": ids})
        c.execute(text("DELETE FROM projects WHERE id = ANY(:p)"), {"p": ids})


def test_an_org_pool_serves_every_project_and_a_project_pool_overrides_it(eng, store):
    one, two = _two_projects(eng)
    try:
        _install_login(store)
        with eng.begin() as c:
            _add(eng, c, one, TOKEN_B)                       # org pool by default
        with eng.begin() as c:
            pool = sp.get_pool(c, one, "claude_code")
            assert pool["scope"] == "org"
            b = pool["members"][1]
            seen_from_two = sp.get_pool(c, two, "claude_code")
            assert seen_from_two["id"] == pool["id"], "the org's pool is the other project's too"
        assert _prove(eng, one, b["id"])["ok"]
        with eng.begin() as c:
            a, b = sp.get_pool(c, two, "claude_code")["members"]
            soon = datetime.now(timezone.utc) + timedelta(hours=3)
            _windows(c, a, rejected_until=soon)
            # a run of EACH project routes across the one pool, off the spent account
            assert sp.assign(c, one, "claude_code", "wfr_a").member_id == b["id"]
            assert sp.assign(c, two, "claude_code", "wfr_b").member_id == b["id"]
            # a member can be managed from either project of the org
            sp.update_member(c, Settings(), two, b["id"], actor="o", label="Bridge")
            # a project's OWN pool overrides the org's for that project only
            own = sp.create_pool(c, two, engine="claude_code", name="own", actor="o")
            m = sp.add_member(c, Settings(), two, own["id"], label="Mine", credential=TOKEN_A,
                              actor="o")
            assert sp.assign(c, two, "claude_code", "wfr_c").member_id == m["id"]
            assert sp.assign(c, one, "claude_code", "wfr_d").member_id == b["id"]
            names = sorted((p["scope"], p["id"] == own["id"]) for p in sp.describe(c, two))
            assert names == [("org", False), ("project", True)]
    finally:
        _drop(eng, [one, two])
