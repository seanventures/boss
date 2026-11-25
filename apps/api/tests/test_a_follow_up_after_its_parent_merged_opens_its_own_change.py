"""A FOLLOW-UP AFTER ITS PARENT MERGED OPENS ITS OWN CHANGE: THE MERGE TAKES WHAT WAS APPROVED.

THE CASE (laptop, neighborhood-board-37936fb9, 2026-11-19). Build crun_92ba… opened change #1
(head 452c7f6) with the card "Neighborhood board is ready — 4 follow-ups queued, BOSS is fixing
them". The follow-up crun_5aa1… started at once on the SAME lineage branch and pushed its
write_tests checkpoint (3182afb, the fixes + tests) at 03:10:46. The person approved the card
at 03:11:10; the merge squashed the branch TIP, so main took 3182afb's unreviewed tree. The
follow-up's own review then REFUTED one fix (a new major finding), its PR step found every file
already on main (pr_follow_up.same_as_merged) and "reused" the merged change #1: no change #2,
no merge card, its review posted onto a merged change, the new finding orphaned.

THE RULE UNDER TEST. A merge merges the head the person approved (the decision's `head_sha`),
never whatever the branch holds by then. Commits pushed after the approval stay on the branch,
and the follow-up's PR step (pr_follow_up.prepare_branch) restacks exactly them onto the base as
its own change — "change #2", carrying only its own commits.

Real git in temporary bare repositories, the real database.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import local_repo, merge_failures, pr_follow_up
from boss_engine.services.local_repo_host import LocalGitAdapter, change_set_url
from boss_engine.services.software_factory import SoftwareFactoryService

BRANCH = "boss/code/crun_fuam1"


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _project(engine) -> str:
    pid = f"fuam-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO orgs (id, name) VALUES ('org_fuam', 'FUAM') ON CONFLICT DO NOTHING"))
        db.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_fuam', 'org_fuam', 'FUAM') "
            "ON CONFLICT DO NOTHING"))
        db.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
            "(:id, 'ws_fuam', :n, :id)"), {"id": pid, "n": f"Follow-up {pid[-4:]}"})
    return pid


def _parent_open_and_follow_up_checkpointed(engine):
    """The live shape: change #1 open at the approved head, then the follow-up's checkpoint
    pushed onto the same lineage branch before the person pressed approve."""
    pid = _project(engine)
    settings = get_settings().model_copy(update={"sandbox_merge_method": "squash"})
    repo = local_repo.create(pid, "board", settings=settings, title="Board")
    adapter = LocalGitAdapter(settings, owner=repo["owner"], repo=repo["repo"],
                              project_id=pid, engine=engine)
    git_dir = adapter.git_dir
    local_repo.ensure_branch(git_dir, BRANCH)
    local_repo.commit_files(git_dir, BRANCH, {"lib/notices.js": "parse(v1)\n",
                                              "app/page.tsx": "page(v1)\n"}, "BOSS: Board")
    pr1 = adapter.create_pull_request(BRANCH, "Board", "the feature")
    approved = local_repo.branch_sha(git_dir, BRANCH)
    # The follow-up's checkpoint (code_task.work_checkpointed) lands on the parent's branch.
    local_repo.commit_files(git_dir, BRANCH, {"lib/notices.js": "parse(v2-fixed)\n",
                                              "tests/notices.test.mjs": "test()\n"},
                            "governed: checkpoint after write_tests — Follow-up")
    assert local_repo.branch_sha(git_dir, BRANCH) != approved
    return pid, settings, adapter, pr1.number, approved


def test_the_merge_takes_the_approved_head_and_the_follow_up_becomes_change_2(engine, repo_root):
    _, _, adapter, number, approved = _parent_open_and_follow_up_checkpointed(engine)
    assert adapter.merge_pull_request(number, "Board", head_sha=approved).merged
    # main holds what the person approved — not the follow-up's unreviewed checkpoint.
    assert adapter.read_text_file("main", "lib/notices.js") == "parse(v1)\n"
    assert adapter.read_text_file("main", "tests/notices.test.mjs") is None
    # The change set records the merged head as the approved commit, so the restack replays
    # exactly the commits made after it.
    assert adapter.last_merged_pull_request(BRANCH)["head_sha"] == approved
    # The follow-up's PR step (software_factory.propose_pull_request → prepare_branch).
    follow = pr_follow_up.prepare_branch(adapter, BRANCH, base="main")
    assert follow and follow["after"] == number and follow["restacked"] is True
    assert follow["paths"] == ["lib/notices.js", "tests/notices.test.mjs"]
    # Its proposal is not "the same as merged": the fix differs from main.
    assert pr_follow_up.same_as_merged(
        adapter, BRANCH, [("lib/notices.js", "parse(v2-fixed)\n")], base="main") is None
    pr2 = adapter.create_pull_request(
        BRANCH, pr_follow_up.follow_up_title("Board", follow["after"]), "the follow-up")
    assert pr2.number == number + 1
    assert {f["path"] for f in adapter.pull_request_files(pr2.number)} == \
        {"lib/notices.js", "tests/notices.test.mjs"}
    assert adapter.merge_pull_request(pr2.number, "Fix: Board after #1").merged
    assert adapter.read_text_file("main", "lib/notices.js") == "parse(v2-fixed)\n"
    assert adapter.read_text_file("main", "app/page.tsx") == "page(v1)\n"


def test_a_rewritten_branch_refuses_the_pinned_merge_and_changes_nothing(engine, repo_root):
    _, _, adapter, number, _ = _parent_open_and_follow_up_checkpointed(engine)
    main_before = local_repo.branch_sha(adapter.git_dir, "main")
    gone = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(local_repo.LocalMergeMoved) as caught:
        adapter.merge_pull_request(number, "Board", head_sha=gone)
    assert adapter.change_set(number)["state"] == "open"
    assert local_repo.branch_sha(adapter.git_dir, "main") == main_before
    # Said in words with no hex in it, so it is never misread as GitHub's 409 and retried.
    assert merge_failures.classify(str(caught.value)) == merge_failures.FAILED


def test_the_factory_merge_pins_the_decisions_head(engine, repo_root, monkeypatch):
    """The seam every approval reaches (software_factory.merge_pull_request): it reads the
    decision's head_sha and hands it to a host that can merge an exact commit."""
    pid, settings, adapter, number, approved = _parent_open_and_follow_up_checkpointed(engine)
    url = change_set_url(adapter.repository_full_name, number)
    did = f"dec-{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, recommendation) VALUES "
            "(:d, :p, 'Merge change #1?', 'approved', CAST(:r AS JSONB))"),
            {"d": did, "p": pid, "r": json.dumps({
                "action": "review_pull_request", "pull_request_url": url,
                "branch_name": BRANCH, "head_sha": approved})})
    sf = SoftwareFactoryService(settings)
    monkeypatch.setattr(sf, "bind_project_github",
                        lambda *a, **k: setattr(sf, "github", adapter), raising=False)
    sf.github = adapter
    with engine.begin() as db:
        assert sf._approved_head_sha(db, pid, did) == approved
        done = sf.merge_pull_request(db, pid, None, did, f"merge:{did}", number, url, "Board",
                                     BRANCH)
    assert done.get("merged_sha"), done
    assert adapter.read_text_file("main", "lib/notices.js") == "parse(v1)\n"
    assert adapter.last_merged_pull_request(BRANCH)["head_sha"] == approved
