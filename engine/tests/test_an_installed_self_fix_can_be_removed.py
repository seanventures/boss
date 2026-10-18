"""REMOVE AND REMOVE ALL (BOSSAPP-571 spec §6.5): a person can take an installed self-fix off the
one-box at any time — one fix (BOSS rebuilt without it, the others kept) or every local fix (the
plain release). Same rules as an install: a person's decision, a signed single-use ticket, the
host self-installer, backup → apply → build → start → health → done, and the current build put
back if the new one is not healthy. The fix's record keeps what it was (it was verified) and says
it is off the box."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services import self_fix_install as sfi
from boss_engine.services.decisions import DecisionService, InvalidDecisionTransitionError

from ._self_fix_support import FakeHost, approve, git, harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def _decide(h, decision_id: str, action: str = "approve", actor: str = "ceo") -> None:
    with h.eng.begin() as c:
        DecisionService(h.settings).transition(c, decision_id, action=action, actor=actor,
                                               note="ok")


@pytest.fixture()
def installed(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["status"] == sf.AWAITING_APPROVAL, out
    approve(harness, sfx["id"])
    host = FakeHost(harness)
    done = host.installer("watch", "--once")
    with harness.eng.begin() as c:
        assert sf.get(c, sfx["id"])["status"] == sf.INSTALLED, done.stdout + done.stderr
    harness.mp.setenv("BOSS_STACK_HEAD", host.running_head())
    yield {"sfx": sfx["id"], "host": host}
    host.close()


def test_removing_a_fix_is_a_persons_decision_and_rebuilds_without_it(harness, installed):
    host, sfx_id = installed["host"], installed["sfx"]
    with harness.eng.begin() as c:
        asked = sfi.request_removal(c, harness.settings, project_id=harness.project,
                                    actor="ceo", sfx_id=sfx_id)
        again = sfi.request_removal(c, harness.settings, project_id=harness.project,
                                    actor="ceo", sfx_id=sfx_id)
        row = c.execute(text("SELECT risk_tier, status, recommendation FROM decisions WHERE "
                             "id = :i"), {"i": asked["decision_id"]}).mappings().first()
        tickets = c.execute(text("SELECT count(*) FROM self_fix_tickets WHERE kind = 'remove' "
                                 "AND self_fix_id = :i"), {"i": sfx_id}).scalar()
    assert again["decision_id"] == asked["decision_id"], "one open removal per fix"
    assert row["status"] == "pending" and row["risk_tier"] == 2
    assert row["recommendation"]["action"] == sfi.REMOVE_ACTION and tickets == 0, \
        "nothing is issued before a person approves"
    _decide(harness, asked["decision_id"])
    done = host.installer("remove", sfx_id)
    assert done.returncode == 0, done.stdout + done.stderr
    with harness.eng.begin() as c:
        fix = sf.get(c, sfx_id)
        ticket = c.execute(text("SELECT status, phases FROM self_fix_tickets WHERE kind = "
                                "'remove' AND self_fix_id = :i"), {"i": sfx_id}).mappings().first()
        left = sfi.stack(c)
    assert ticket["status"] == "done"
    assert [p["phase"] for p in ticket["phases"]] == ["backup", "apply", "build", "start",
                                                      "health", "done"]
    assert fix["stack_state"] == "removed" and fix["status"] == sf.INSTALLED
    assert fix["chip"] == "Rolled back" and left == []
    assert host.running_head() == host.release, "the plain release runs again"
    assert git(host.dir, "rev-parse", "refs/boss/stack/v0.0.0-test") == host.release
    assert git(host.dir, "rev-parse", "HEAD") == host.release, "the checkout never moved"


def test_remove_all_puts_the_plain_release_back(harness, installed):
    host = installed["host"]
    with harness.eng.begin() as c:
        asked = sfi.request_removal(c, harness.settings, project_id=harness.project,
                                    actor="ceo")
    assert asked["kind"] == "remove_all"
    _decide(harness, asked["decision_id"])
    done = host.installer("remove-all")
    assert done.returncode == 0, done.stdout + done.stderr
    with harness.eng.begin() as c:
        assert sfi.stack(c) == []
        assert sf.get(c, installed["sfx"])["stack_state"] == "removed"
    assert host.running_head() == host.release


def test_nothing_to_remove_and_no_policy_approval(harness, installed):
    with harness.eng.begin() as c:
        with pytest.raises(sfi.InstallRefused, match="not on the local stack"):
            sfi.request_removal(c, harness.settings, project_id=harness.project, actor="ceo",
                                sfx_id="sfx_0000000000aaaaaaaaaa")
        asked = sfi.request_removal(c, harness.settings, project_id=harness.project,
                                    actor="ceo", sfx_id=installed["sfx"])
    with pytest.raises(InvalidDecisionTransitionError, match="A person approves"):
        _decide(harness, asked["decision_id"], actor="sys_worker")
    from boss_engine.services.decisions import approve_by_policy

    with harness.eng.begin() as c:
        assert approve_by_policy(c, asked["decision_id"], project_id=harness.project,
                                 actor="autonomy_policy", note="policy") is False


def test_no_approved_removal_means_the_command_does_nothing(harness, installed):
    done = installed["host"].installer("remove", installed["sfx"])
    assert done.returncode == 1 and "No approved removal" in done.stdout
    assert installed["host"].running_head() != installed["host"].release
