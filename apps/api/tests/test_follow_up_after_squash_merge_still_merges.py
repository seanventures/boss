"""A FOLLOW-UP AFTER A SQUASH MERGE STILL MERGES (BOSSAPP-980, -981, -982).

THE CASE (helpdesk-laptop, run wfr_8e29…, 2026-11-10). The Standard workflow squash-merged
change #1 (the install default, BOSS_SANDBOX_MERGE_METHOD="squash"); the workflow's follow-up
code tasks (launch readiness, more tests) kept committing to the same boss/code/crun_… lineage
branch; and change #2 could NEVER merge: with #1 squashed, the branch shares no history with
main beyond the initial commit, so every file both sides hold is an add/add conflict —
"boss/code/crun_… does not merge cleanly into main". A fix run could not repair it (the
governed pipeline only appends commits, BOSSAPP-981), and the approved merge decision slid
back to pending while its job dead-lettered (BOSSAPP-982).

THE DESIGN UNDER TEST is the one GitHub repositories already had (pr_follow_up, PR #236/#237,
2026-11-08), now answered by the LOCAL host too: `open_pull_request_for`,
`last_merged_pull_request` and `restack_after_merge` on LocalGitAdapter, so

  * at the PR-open seam every host shares (software_factory.create_pull_request →
    pr_follow_up.prepare_branch), a follow-up branch is put back on its base BEFORE its change
    set opens, proposing only what came after the merge; and
  * at the merge seam, a change set that already conflicted (the live run's change #2) recovers
    through tier 1b (software_factory._merge_after_restack) — merge_failures.classify now reads
    the local conflict wording as NOT_MERGEABLE, which is what arms that recovery.

Real git in temporary bare repositories, the real database.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.config import Settings, get_settings
from boss_engine.services import local_repo, merge_failures, pr_follow_up
from boss_engine.services.local_repo_host import LocalGitAdapter, change_set_url
from boss_engine.services.software_factory import SoftwareFactoryService

BRANCH = "boss/code/crun_sqm1"


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _project(engine) -> str:
    pid = f"sqm-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO orgs (id, name) VALUES ('org_sqm', 'SQM') ON CONFLICT DO NOTHING"))
        db.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_sqm', 'org_sqm', 'SQM') "
            "ON CONFLICT DO NOTHING"))
        db.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
            "(:id, 'ws_sqm', :n, :id)"), {"id": pid, "n": f"Squash {pid[-4:]}"})
    return pid


def _squash_merged_lineage(engine) -> tuple[str, LocalGitAdapter]:
    """The live run's exact shape: change #1 squash-merged, and the lineage branch then took
    the workflow's follow-up commits (launch readiness, more tests)."""
    pid = _project(engine)
    settings = get_settings().model_copy(update={"sandbox_merge_method": "squash"})
    repo = local_repo.create(pid, "app", settings=settings, title="App")
    adapter = LocalGitAdapter(settings, owner=repo["owner"], repo=repo["repo"],
                              project_id=pid, engine=engine)
    git_dir = adapter.git_dir
    local_repo.commit_files(git_dir, "main", {"app.py": "print('v0')\n"}, "base")
    local_repo.ensure_branch(git_dir, BRANCH)
    local_repo.commit_files(git_dir, BRANCH,
                            {"app.py": "print('v1')\n", "api.py": "API = 1\n"}, "change 1")
    pr1 = adapter.create_pull_request(BRANCH, "Change 1", "the feature")
    assert pr1.number == 1
    assert adapter.merge_pull_request(pr1.number, "Change 1").merged
    # The squash divergence that killed the live run: main took ONE new commit, so the branch
    # is not an ancestor of main and their merge-base is the pre-change history.
    assert local_repo.merge_base(git_dir, BRANCH, "main") != local_repo.branch_sha(git_dir, "main")
    # The follow-up tasks commit to the SAME lineage branch (code_task_service.work_branch_for).
    local_repo.commit_files(git_dir, BRANCH,
                            {"api.py": "API = 2\n", "tests/test_api.py": "def test(): pass\n"},
                            "more tests")
    return pid, adapter


def test_the_install_default_merge_method_is_squash():
    """The field default this whole class of failure rides on — documented, not assumed."""
    assert Settings.model_fields["sandbox_merge_method"].default == "squash"


def test_a_run_id_containing_409_does_not_disguise_the_conflict():
    """Branch names embed hex run ids, and hex happily contains "409"/"405": the local
    conflict wording must classify as the conflict it is, never as a moved head."""
    msg = "boss/code/crun_a4409fe2b405 does not merge cleanly into main: app.py conflict."
    assert merge_failures.classify(msg) == merge_failures.NOT_MERGEABLE


def test_a_follow_up_after_the_squash_merge_restacks_and_change_2_merges(engine, repo_root):
    _, adapter = _squash_merged_lineage(engine)
    # The PR-open seam every host shares (software_factory.create_pull_request).
    follow = pr_follow_up.prepare_branch(adapter, BRANCH, base="main")
    assert follow is not None
    assert follow["after"] == 1
    assert follow["restacked"] is True
    assert follow["paths"] == ["api.py", "tests/test_api.py"]
    pr2 = adapter.create_pull_request(
        BRANCH, pr_follow_up.follow_up_title("Change 1", follow["after"]), "the follow-up")
    assert pr2.number == 2
    # Change #2 proposes ONLY what came after the merge…
    assert {f["path"] for f in adapter.pull_request_files(pr2.number)} == \
        {"api.py", "tests/test_api.py"}
    # …and it MERGES — the exact step the live run could never pass.
    assert adapter.merge_pull_request(pr2.number, "Fix: Change 1 after #1").merged
    assert adapter.read_text_file("main", "api.py") == "API = 2\n"
    assert adapter.read_text_file("main", "tests/test_api.py") == "def test(): pass\n"
    assert adapter.read_text_file("main", "app.py") == "print('v1')\n"


def test_a_change_set_opened_before_the_fix_recovers_at_merge_time(engine, repo_root):
    """The live run's change #2 exists already (opened from the stale branch): after install,
    pressing Merge again must land it, through software_factory's tier-1b recovery."""
    pid, adapter = _squash_merged_lineage(engine)
    pr2 = adapter.create_pull_request(BRANCH, "Change 1 · retry 1", "from the stale branch")
    # The live failure, word for word.
    with pytest.raises(local_repo.LocalMergeConflict) as caught:
        adapter.merge_pull_request(pr2.number, "Change 1 · retry 1")
    assert "does not merge cleanly into main" in str(caught.value)
    assert adapter.change_set(pr2.number)["state"] == "open"    # the refusal changed nothing
    # The ONE classifier that arms the recovery now reads it as the conflict it is.
    assert merge_failures.classify(str(caught.value)) == merge_failures.NOT_MERGEABLE
    # Tier 1b (the real method, on the real adapter): branch back on its base, merged.
    events: list[tuple] = []
    stub = SimpleNamespace(github=adapter,
                           events=SimpleNamespace(emit=lambda *a, **k: events.append(a)))
    with engine.begin() as c:
        merge = SoftwareFactoryService._merge_after_restack(
            stub, c, pid, pr2.number, "Change 1 · retry 1", BRANCH, "rcpt_test",
            change_set_url(adapter.repository_full_name, pr2.number))
    assert merge is not None and merge.merged
    assert adapter.change_set(pr2.number)["state"] == "merged"
    assert adapter.read_text_file("main", "tests/test_api.py") == "def test(): pass\n"
    assert any(a[2] == "software_factory.pull_request_restacked" for a in events)


def test_a_replay_that_conflicts_refuses_and_changes_nothing(engine, repo_root):
    """A base that moved past the merge in a way the follow-up cannot absorb needs an agent:
    the restack says so and leaves the branch exactly where it was."""
    _, adapter = _squash_merged_lineage(engine)
    local_repo.commit_files(adapter.git_dir, "main", {"api.py": "API = 'someone else'\n"},
                            "main moved")
    head_before = local_repo.branch_sha(adapter.git_dir, BRANCH)
    merged = adapter.last_merged_pull_request(BRANCH)
    assert merged is not None and merged["number"] == 1 and merged["head_sha"]
    out = adapter.restack_after_merge(BRANCH, merged["head_sha"])
    assert out["restacked"] is False
    assert "conflicted" in out["why"]
    assert local_repo.branch_sha(adapter.git_dir, BRANCH) == head_before


def test_a_deadlettered_merge_job_is_said_on_the_decision(engine):
    """BOSSAPP-982: the approval must never silently vanish. When the merge job dead-letters,
    the decision carries "Merge failed: …" and is answerable again — and a decision
    merge_failures already spoke for is left with its words."""
    pid = _project(engine)
    did = f"dec-{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, recommendation) VALUES "
            "(:d, :p, 'Merge change #2?', 'approved', CAST(:r AS JSONB))"),
            {"d": did, "p": pid, "r": json.dumps({"action": "review_pull_request"})})
    job = {"id": "job_dlq_sqm", "project_id": pid, "kind": "github.pull_request.merge",
           "payload": {"decision_id": did, "pull_request_number": 2}}
    error = {"message": "boss/code/crun_x does not merge cleanly into main: app.py conflict."}
    with engine.begin() as db:
        merge_failures.on_job_deadlettered(db, job, error)
    with engine.begin() as db:
        row = db.execute(text(
            "SELECT status, resolution_note, recommendation FROM decisions WHERE id = :d"),
            {"d": did}).mappings().one()
    assert row["status"] == "pending"                 # answerable again, never a mute 'approved'
    assert str(row["resolution_note"]).startswith("Merge failed:")
    rec = row["recommendation"]
    rec = json.loads(rec) if isinstance(rec, str) else rec
    assert rec[merge_failures.KEY]["kind"] == merge_failures.NOT_MERGEABLE
    note_before = row["resolution_note"]
    with engine.begin() as db:
        merge_failures.on_job_deadlettered(db, job, {"message": "another error"})
        row2 = db.execute(text(
            "SELECT status, resolution_note FROM decisions WHERE id = :d"),
            {"d": did}).mappings().one()
    assert row2["status"] == "pending"
    assert row2["resolution_note"] == note_before
