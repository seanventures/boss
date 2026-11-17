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


def _install_login(store, token=TOKEN_A):
    from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from boss_engine.services.secrets_service import ref_key_for

    store.put(ref_key_for(SUBSCRIPTION_SECRET_SCOPE, "CLAUDE_CODE_OAUTH_TOKEN"), token)


def test_signing_in_again_adds_and_the_install_login_is_account_1(eng, project, store):
    _install_login(store)
    with eng.begin() as c:
        second = sp.add_signed_in_account(c, Settings(), project, "claude_code", TOKEN_B,
                                          actor="operator")
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
        sp.add_signed_in_account(c, Settings(), project, "claude_code", "sk-ant-oat01-" + "c" * 48,
                                 actor="operator")
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
        sp.add_signed_in_account(c, Settings(), project, "claude_code", TOKEN_B, actor="o")
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
        sp.add_signed_in_account(c, Settings(), project, "claude_code", TOKEN_B, actor="o")
        a, b = sp.get_pool(c, project, "claude_code")["members"]
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
