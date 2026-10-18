"""P0: a self-fix that breaks BOSS's health is rolled back by itself (BOSSAPP-571 R7, spec §6.5,
F6): the previous images (:prev) are put back and started, checked, the stack ref moves back,
and the self-fix says why — never a rebuild, never code from the fix. And the kill switch never
stops an install half-way: it finishes or rolls back.

The REAL host self-installer against a real checkout, a fake Docker and a fake API over the real
engine functions (see _self_fix_support.FakeHost)."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build

from ._self_fix_support import FakeHost, approve, git, harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


@pytest.fixture()
def approved(harness):
    harness.script("breaks_health")
    sfx = harness.propose()["self_fix"]
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["status"] == sf.AWAITING_APPROVAL, out
    approve(harness, sfx["id"])
    return sfx["id"]


def test_broken_health_puts_the_previous_build_back(harness, approved):
    host = FakeHost(harness, health="broken")
    try:
        done = host.installer("watch", "--once")
    finally:
        host.close()
    assert "stopped at health" in done.stdout, done.stdout + done.stderr
    with harness.eng.begin() as c:
        row = sf.get(c, approved)
        phases = c.execute(text("SELECT phases, status FROM self_fix_tickets WHERE "
                                "self_fix_id = :i"), {"i": approved}).mappings().first()
        rec = boss_recommendations.list_recommendations(c, harness.project)[0]
    assert row["status"] == sf.ROLLED_BACK and row["rolled_back_at"]
    said = [(p["phase"], p["ok"]) for p in phases["phases"]]
    assert ("health", False) in said and said[-1] == ("rollback", True)
    assert phases["status"] == "rolled_back"
    # The previous build is what runs, from its kept images — no rebuild after the failure.
    assert host.running_head() == host.release
    builds = [c for c in host.docker_state()["calls"] if "build" in c]
    assert len(builds) == 1, "rolled back by re-tag, not by building the old source again"
    assert host.stack_ref() == "", "the stack ref moved back"
    assert git(host.dir, "rev-parse", "HEAD") == host.release
    assert f"BOSS_STACK_HEAD={host.release}" in (host.dir / ".env").read_text()
    assert rec["status"] == "open" and rec["attempts"][-1]["outcome"] == sf.ROLLED_BACK


def test_switching_off_before_the_host_takes_it_cancels_the_install(harness, approved):
    host = FakeHost(harness)
    try:
        with harness.eng.begin() as c:
            sf.set_switch(c, on=False, actor="ceo", why="stop everything")
        done = host.installer("watch", "--once")
    finally:
        host.close()
    with harness.eng.begin() as c:
        row = sf.get(c, approved)
        statuses = [r[0] for r in c.execute(text(
            "SELECT status FROM self_fix_tickets WHERE self_fix_id = :i"), {"i": approved})]
    assert row["status"] == sf.CANCELLED and statuses == ["revoked"]
    assert host.stack_ref() == "" and host.running_head() == host.release
    assert done.returncode == 0


def test_switching_off_mid_install_finishes_or_rolls_back_never_half_way(harness, approved):
    """Taken, then switched off: the install already on the box finishes; cancel refuses it."""
    host = FakeHost(harness)
    try:
        with harness.eng.begin() as c:
            ticket = c.execute(text("SELECT id FROM self_fix_tickets WHERE self_fix_id = :i"),
                               {"i": approved}).scalar()
            sf.transition(c, approved, sf.INSTALLING, actor="sys_self_installer",
                          sentence="installing")
            sf.set_switch(c, on=False, actor="ceo", why="stop")
            assert sf.get(c, approved)["status"] == sf.INSTALLING, "never stopped half-way"
            c.execute(text("UPDATE self_fix_tickets SET status = 'taken' WHERE id = :i"),
                      {"i": ticket})
        from boss_engine.services import self_fix_install as sfi

        with harness.eng.begin() as c:
            for phase in ("start", "done"):
                sfi.report(c, harness.settings, ticket, phase=phase, ok=True,
                           stack_head=sf.get(c, approved)["fix_commit_sha"])
            assert sf.get(c, approved)["status"] == sf.INSTALLED, "it finished its install"
    finally:
        host.close()
