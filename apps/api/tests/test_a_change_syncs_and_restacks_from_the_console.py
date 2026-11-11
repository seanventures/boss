"""GOVERNED GIT ACTIONS (BOSSAPP-996): the change page's Sync (pull the base in), Restack (after
a squash merge, PR #31's machinery) and the conflict list — one API shape over a LOCAL change set
and a GitHub pull request, every action a recorded decision, never ambient git.

Real git in temporary bare repositories, the real database, the real routes and the real
decision transition (propose → approve as one press)."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.test_change_review import (  # noqa: F401 — pytest fixtures
    FakeGitHub, client, github, world,
)
from apps.api.tests.test_local_change_review import (  # noqa: F401 — pytest fixtures
    APP_V1, _owner, _project_with_change, _viewer, repo_root,
)
from boss_engine.services import local_repo

HEAD = "e" * 40


def _sync_decisions(engine, pid):
    with engine.begin() as db:
        return db.execute(text(
            "SELECT status, recommendation->>'mode' AS mode FROM decisions "
            "WHERE project_id = :p AND recommendation->>'action' = 'change_sync_branch' "
            "ORDER BY created_at"), {"p": pid}).mappings().all()


# ── a local change set ────────────────────────────────────────────────────────────────────────

def test_the_preview_says_where_the_branch_stands(client, world, engine, repo_root):  # noqa: F811
    pid, _b, bare, _branch, _pr, _r = _project_with_change(engine, world)
    with _viewer(world):
        r = client.get(f"/v1/projects/{pid}/local-changes/1/sync-preview")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["ahead"] == 2 and p["behind"] == 0
    assert p["mergeable"] is True and p["conflicts"] == [] and p["conflicts_known"] is True
    assert p["restack"] is None and p["base"] == "main"


def test_sync_pulls_the_base_in_as_one_recorded_decision(client, world, engine, repo_root):  # noqa: F811
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    local_repo.commit_files(bare, "main", {"docs/notes.md": "moved on\n"}, "base moves")
    with _viewer(world):
        before = client.get(f"/v1/projects/{pid}/local-changes/1/sync-preview").json()
    assert before["behind"] == 1
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-changes/1/sync", json={"mode": "update"})
    assert r.status_code == 200, r.text
    # The branch now holds the base's commit — a real merge, in the bare repository.
    main_tip = local_repo.branch_sha(bare, "main")
    assert local_repo.merge_base(bare, main_tip, local_repo.branch_sha(bare, branch)) == main_tip
    rows = _sync_decisions(engine, pid)
    assert [(d["status"], d["mode"]) for d in rows] == [("approved", "update")]
    with _viewer(world):
        after = client.get(f"/v1/projects/{pid}/local-changes/1/sync-preview").json()
    assert after["behind"] == 0


def test_a_conflict_names_its_files_and_the_decision_stays_pending(client, world, engine,  # noqa: F811
                                                                   repo_root):  # noqa: F811
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    # Both sides rewrite line 5 of src/app.js: the base cannot merge in cleanly.
    local_repo.commit_files(bare, "main", {"src/app.js": APP_V1.replace("line 5\n", "line V\n")},
                            "base rewrites line 5")
    with _viewer(world):
        p = client.get(f"/v1/projects/{pid}/local-changes/1/sync-preview").json()
    assert p["mergeable"] is False and p["conflicts"] == ["src/app.js"]
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-changes/1/sync", json={"mode": "update"})
    assert r.status_code == 409, r.text
    assert "src/app.js" in str(r.json())
    # The refusal rolled the approval back: the decision is still pending, nothing moved.
    rows = _sync_decisions(engine, pid)
    assert [(d["status"], d["mode"]) for d in rows] == [("pending", "update")]
    head = local_repo.branch_sha(bare, branch)
    main_tip = local_repo.branch_sha(bare, "main")
    assert local_repo.merge_base(bare, main_tip, head) != main_tip


def test_restack_replays_only_what_came_after_the_squash_merge(client, world, engine, repo_root,  # noqa: F811
                                                               monkeypatch):  # noqa: F811
    from boss_engine.config import get_settings
    from boss_engine.services.local_repo_host import LocalGitAdapter

    pid, bound, bare, branch, _pr, _r = _project_with_change(engine, world)
    adapter = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"],
                              project_id=pid)
    monkeypatch.setattr(get_settings(), "sandbox_merge_method", "squash", raising=False)
    adapter.merge_pull_request(1, "Squash change #1")
    # The branch keeps working after the merge: the next change set from it would re-propose
    # everything the squash already landed (BOSSAPP-980).
    local_repo.commit_files(bare, branch, {"src/after.js": "export const after = 1;\n"},
                            "work after the merge")
    pr2 = adapter.create_pull_request(branch, "Follow-up", "")
    assert pr2.number == 2
    with _viewer(world):
        p = client.get(f"/v1/projects/{pid}/local-changes/2/sync-preview").json()
    assert p["restack"] == {"number": 1, "merged_head_sha": p["restack"]["merged_head_sha"],
                            "merged_at": p["restack"]["merged_at"]}
    assert p["restack"]["merged_head_sha"]
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-changes/2/sync", json={"mode": "restack"})
    assert r.status_code == 200, r.text
    # The branch sits on the base now, proposing only the follow-up commit.
    files = adapter.pull_request_files(2)
    assert [f["path"] for f in files] == ["src/after.js"]
    rows = _sync_decisions(engine, pid)
    assert ("approved", "restack") in {(d["status"], d["mode"]) for d in rows}


def test_a_viewer_reads_the_preview_but_cannot_run_the_action(client, world, engine, repo_root):  # noqa: F811
    pid, _b, _bare, _branch, _pr, _r = _project_with_change(engine, world)
    with _viewer(world):
        read = client.get(f"/v1/projects/{pid}/local-changes/1/sync-preview")
        act = client.post(f"/v1/projects/{pid}/local-changes/1/sync", json={"mode": "update"})
    assert read.status_code == 200
    assert act.status_code == 403, act.text
    assert _sync_decisions(engine, pid) == []


# ── a GitHub pull request, the same shape ─────────────────────────────────────────────────────

def _teach_sync(fake: FakeGitHub, *, mergeable, update_ok: bool = True):
    fake.repository_full_name = f"{fake.owner}/{fake.repo}"
    fake.updated: list[int] = []

    def snapshot(n):
        fake._hit("snapshot")
        return {"number": n, "url": "", "state": "open", "draft": False, "mergeable": mergeable,
                "mergeable_state": "clean" if mergeable else "dirty", "head_ref": "boss/code/crun_x",
                "head_sha": HEAD, "head_repository": fake.repository_full_name,
                "base_ref": "main", "base_sha": "b" * 40,
                "base_repository": fake.repository_full_name, "updated_at": None}

    def request(method, path, payload=None, **kw):
        fake._hit("compare")
        assert "/compare/main...boss%2Fcode%2Fcrun_x" in path
        return {"ahead_by": 2, "behind_by": 1}

    def update(n):
        fake._hit("update_branch")
        if update_ok:
            fake.updated.append(int(n))
        return update_ok
    fake.pull_request_snapshot = snapshot
    fake._request = request
    fake.update_pull_request_branch = update
    fake.last_merged_pull_request = lambda b: None


def test_a_github_pr_syncs_the_same_way(client, world, engine, github):  # noqa: F811
    _teach_sync(github["fake"], mergeable=True)
    with _viewer(world):
        p = client.get(f"{github['base']}/sync-preview").json()
    assert p["source"] == "github" and p["behind"] == 1 and p["ahead"] == 2
    assert p["mergeable"] is True
    # GitHub says CLEAN or DIRTY; it never names the conflicting files, and the API says so.
    assert p["conflicts_known"] is False and p["conflicts"] == []
    with _owner(world):
        r = client.post(f"{github['base']}/sync", json={"mode": "update"})
    assert r.status_code == 200, r.text
    assert github["fake"].updated == [41]
    rows = _sync_decisions(engine, github["pid"])
    assert [(d["status"], d["mode"]) for d in rows] == [("approved", "update")]


def test_a_github_refusal_keeps_the_decision_pending(client, world, engine, github):  # noqa: F811
    _teach_sync(github["fake"], mergeable=False, update_ok=False)
    with _owner(world):
        r = client.post(f"{github['base']}/sync", json={"mode": "update"})
    assert r.status_code == 409, r.text
    rows = _sync_decisions(engine, github["pid"])
    assert [(d["status"], d["mode"]) for d in rows] == [("pending", "update")]
