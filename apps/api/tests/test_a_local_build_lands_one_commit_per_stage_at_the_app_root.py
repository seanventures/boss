"""A LOCAL BUILD'S CHANGE SET READS LIKE A PULL REQUEST (BOSSAPP-946, finding 7).

The local-first HelpDesk's change set had 52 commits, one per file, each "BOSS proposal update:
<file>"; the whole app sat in local_helpdesk-laptop/helpdesk-laptop/ inside the repository; and
the review notes pointed at paths the change set did not have.

Pinned, on real git and the real database:
  * a stage (and the proposal) is ONE commit on a host that can batch (the local repository),
    with the stage's own words; GitHub's per-file contents API keeps its per-file commits;
  * the agents are never told a local repository's `local_<project>/<name>` as if it were a path,
    and are told the repository's root is where the app goes;
  * every inline review note names a file of the change set: the same path, or the one changed
    file that ends with it; a path the change does not have is said in the round's body instead.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import local_projects, local_repo, pr_review_post
from boss_engine.services.local_repo_host import LocalGitAdapter
from boss_engine.services.software_factory import FileChange, SoftwareFactoryService, commit_change
from boss_engine.services import workflow_capability_runner as runner


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _local_project(engine) -> tuple[str, str, str]:
    pid = f"lfc-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Desk {pid[-4:]}"})
        bound = local_projects.create_for_project(db, get_settings(), pid, name="Help Desk",
                                                  actor="t")
    return pid, bound["owner"], bound["repo"]


class PerFileHost:
    """GitHub's contents API: one commit per file, no batch."""

    def __init__(self):
        self.calls = []

    def upsert_file(self, branch, path, content, message):
        self.calls.append((path, message))
        return f"sha{len(self.calls)}"


def test_a_stage_is_one_commit_where_the_host_can_batch(engine, repo_root):
    pid, owner, repo = _local_project(engine)
    host = LocalGitAdapter(get_settings(), owner=owner, repo=repo, project_id=pid)
    host.ensure_branch("boss/code/x", "main")
    shas = commit_change(host, "boss/code/x", [("server/app.js", "x\n"), ("server/db.js", "y\n"),
                                              ("web/index.html", "<p>\n")],
                         "governed: implement (SE) — Help desk")
    assert len(shas) == 1
    commits = local_repo.commits_between(host.git_dir, "main", "boss/code/x")
    assert [c["message"].splitlines()[0] for c in commits] == ["governed: implement (SE) — Help desk"]
    # GitHub keeps its per-file commits, named per file when asked.
    gh = PerFileHost()
    assert commit_change(gh, "b", [("a.js", "1"), ("b.js", "2")], "stage",
                         per_file=lambda p: f"BOSS proposal update: {p}") == ["sha1", "sha2"]
    assert gh.calls == [("a.js", "BOSS proposal update: a.js"), ("b.js", "BOSS proposal update: b.js")]


def test_the_proposal_of_a_local_build_is_one_commit_with_its_title(engine, repo_root):
    pid, owner, repo = _local_project(engine)
    settings = get_settings()
    sf = SoftwareFactoryService(settings)
    with engine.begin() as db:
        sf.bind_project_github(db, pid)
        reservation = sf.budget.reserve(db, pid, category="code_task", amount_usd_micros=1000,
                                        reason="local-first test", expires_in_minutes=30)
    branch = f"boss/code/crun_lfc{uuid.uuid4().hex[:8]}"
    files = [FileChange(path=p, content=f"// {p}\n") for p in
             ("package.json", "server/app.js", "server/routes.js", "web/src/App.jsx")]
    with engine.begin() as db:
        out = sf.propose_pull_request(db, pid, None, reservation_id=reservation["reservation_id"],
                                      dedupe_key=f"code:task:{branch}", title="Help desk",
                                      branch_name=branch, summary="A help desk.", files=files,
                                      pr_base="main")
    assert out["status"] in ("created", "succeeded"), out
    git_dir = local_repo.bare_path(owner, repo, settings)
    commits = local_repo.commits_between(git_dir, "main", branch)
    assert len(commits) == 1, commits
    assert commits[0]["message"].startswith("BOSS: Help desk")
    assert not any("proposal update" in c["message"] for c in commits)
    # At the root, as the build wrote them: nothing nested under the repository's own name.
    paths = local_repo.list_paths(git_dir, branch)
    assert "package.json" in paths and not any(p.startswith(f"{owner}/") for p in paths)


def test_agents_are_never_told_a_local_repository_name_as_a_path():
    note = runner._existing_code_note({"repository": "local_helpdesk-laptop/helpdesk-laptop"})
    assert "local_helpdesk-laptop/helpdesk-laptop" not in note
    assert "'helpdesk-laptop'" in note and "kept on this machine" in note
    assert runner.ROOT_IS_THE_WORKING_DIRECTORY in note
    github = runner._existing_code_note({"repository": "acme/helpdesk"})
    assert "the repository acme/helpdesk" in github and runner.ROOT_IS_THE_WORKING_DIRECTORY in github


def test_review_notes_name_files_of_the_change_set(engine, repo_root):
    changed = {"helpdesk/server/app.js", "helpdesk/server/db.js", "helpdesk/.env.example"}
    comments = [
        {"id": "c1", "round": 0, "path": "helpdesk/server/app.js", "line": 3, "body": "same"},
        {"id": "c2", "round": 0, "path": "server/db.js", "line": 1, "body": "from the app dir"},
        {"id": "c3", "round": 0, "path": "client/nowhere.js", "line": 9, "body": "not changed"},
        {"id": "c4", "round": 0, "body": "no line"},
    ]
    out = {c["id"]: c for c in pr_review_post.anchor_to_change(comments, changed)}
    assert out["c1"]["path"] == "helpdesk/server/app.js" and not out["c1"].get("off_change")
    assert out["c2"]["path"] == "helpdesk/server/db.js" and out["c2"]["said_path"] == "server/db.js"
    assert out["c3"].get("off_change") is True
    assert not out["c4"].get("off_change")

    # Posted on a real local change set: every inline note is on a changed file; the off-change
    # finding is in the round's words.
    pid, owner, repo = _local_project(engine)
    host = LocalGitAdapter(get_settings(), owner=owner, repo=repo, project_id=pid)
    branch = "boss/code/crun_review"
    host.ensure_branch(branch, "main")
    host.commit_files(branch, {p: "a\nb\nc\nd\n" for p in changed}, "governed: implement")
    pr = host.create_pull_request(branch, "Help desk", "body")
    got = pr_review_post.post(host, pr.number, comments=comments,
                              rounds=[{"round": 0, "seats": {"review_code": {"verdict": "survived"}}}],
                              repository=host.repository_full_name)
    assert got["posted"] is True, got
    notes = host.notes(pr.number)
    review = next(n for n in notes if n["kind"] == "review")
    inline = {c["path"] for c in review["comments"]}
    assert inline <= changed and inline == {"helpdesk/server/app.js", "helpdesk/server/db.js"}
    assert "client/nowhere.js:9" in review["body"] and "not changed" in review["body"]
