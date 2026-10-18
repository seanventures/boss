"""BOSS's own view of an upgrade's stack plan is the installer's plan (one implementation), and
what the host did is recorded on every fix — nothing dropped silently (BOSSAPP-571 R8, §6.4)."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services import boss_recommendations
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_install as sfi
from boss_engine.services import self_fix_source

from ._self_fix_support import git, harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def _installed(harness, status: str = "verified") -> tuple[str, str]:
    """A self-fix on the stack: its commit in the mirror, its row in self_fix_stack."""
    repo = harness.repo
    git(repo, "checkout", "-q", "-b", "boss/self-fix/x")
    path = repo / "engine/boss_engine/services/widget.py"
    path.write_text(path.read_text().replace("return x + 2", "return x * 2"))
    git(repo, "commit", "-qam", "fix(engine): double doubles")
    fix = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    sfx = harness.propose()["self_fix"]
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET status = :s, fix_commit_sha = :c WHERE id = :i"),
                  {"s": status, "c": fix, "i": sfx["id"]})
        c.execute(text("INSERT INTO self_fix_stack (release_ref, position, self_fix_id, "
                       "commit_sha) VALUES ('v0.0.0-test', 1, :i, :c)"),
                  {"i": sfx["id"], "c": fix})
    return sfx["id"], fix


def test_the_plan_reads_the_mirror_and_a_release_with_the_fix_drops_it(harness):
    sfx_id, fix = _installed(harness)
    git(harness.repo, "cherry-pick", fix)
    git(harness.repo, "tag", "v0.0.1-test")
    self_fix_source.seed(harness.repo, harness.mirror)
    with harness.eng.begin() as c:
        plan = sfi.upgrade_plan(c, harness.settings, to_ref="v0.0.1-test")
        assert [f["outcome"] for f in plan["fixes"]] == ["dropped_upstream"]
        sfi.record_upgrade(c, plan["fixes"], release="v0.0.1-test")
        row = sf.get(c, sfx_id)
        rec = c.execute(text("SELECT status FROM boss_recommendations WHERE id = :i"),
                        {"i": row["recommendation_id"]}).scalar()
        stacked = c.execute(text("SELECT count(*) FROM self_fix_stack WHERE self_fix_id = :i"),
                            {"i": sfx_id}).scalar()
    assert row["status"] == sf.SUPERSEDED and row["stack_state"] == "dropped_upstream"
    assert rec == "done" and stacked == 0


def test_a_fix_left_out_of_an_upgrade_reopens_its_recommendation(harness):
    sfx_id, fix = _installed(harness, status="installed")
    with harness.eng.begin() as c:
        sfi.record_upgrade(c, [{"self_fix_id": sfx_id, "commit": fix, "outcome": "removed"}],
                           release="v0.0.2-test")
        row = sf.get(c, sfx_id)
        recs = boss_recommendations.list_recommendations(c, harness.project)
    assert row["stack_state"] == "removed"
    assert recs[0]["status"] == "open" and recs[0]["attempts"][-1]["outcome"] == "removed"


def test_a_carried_fix_moves_to_the_new_release_stack(harness):
    sfx_id, fix = _installed(harness, status="installed")
    with harness.eng.begin() as c:
        sfi.record_upgrade(c, [{"self_fix_id": sfx_id, "commit": fix, "outcome": "carried",
                                "new_commit": "c" * 40}], release="v0.0.2-test")
        rows = c.execute(text("SELECT release_ref, commit_sha FROM self_fix_stack WHERE "
                              "self_fix_id = :i"), {"i": sfx_id}).all()
    assert [tuple(r) for r in rows] == [("v0.0.2-test", "c" * 40)]
