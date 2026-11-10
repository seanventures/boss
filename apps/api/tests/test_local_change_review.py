"""LOCAL REVIEW (BOSSAPP-948, P1): the file view, line comments in one store, Request changes and
the fix round whose required inputs are a person's comments, the agent's replies, and Merge held
while a person's comment is open. Design: docs/product/LOCAL_GIT_REMOTE_AND_REVIEW.md.

Real git in temporary bare repositories, the real database, the real routes."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_projects, local_repo, local_review
from boss_engine.services.local_repo_host import LocalGitAdapter, change_set_url


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("lr-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("lr-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


@pytest.fixture
def builds_start(monkeypatch):
    """A fresh project has no agent roster: the build's consent is not what these tests check."""
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda **kw: None)


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


def _viewer(world):
    return world.acting_as(world.ctx(world.viewer))


APP_V1 = "".join(f"line {i}\n" for i in range(1, 11))


def _project_with_change(engine, world, *, run: bool = False):
    """A fresh local project, a branch with two commits, and its open change set #1."""
    pid = f"lr-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Desk {pid[-4:]}"})
        bound = local_projects.create_for_project(db, get_settings(), pid, name="desk", actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    local_repo.commit_files(bare, "main", {"src/app.js": APP_V1, "logo.png": b"\x89PNG\x00\x01"},
                            "base files")
    branch = "boss/code/crun_lr" + uuid.uuid4().hex[:8] if run else "feature/sla"
    local_repo.ensure_branch(bare, branch)
    local_repo.commit_files(bare, branch, {"src/app.js": APP_V1.replace("line 5\n", "line five\n")},
                            "change line 5")
    local_repo.commit_files(bare, branch, {"src/sla.js": "export const hours = 4;\n"}, "add sla")
    run_id = None
    if run:
        run_id = branch.rsplit("/", 1)[-1]
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                "finalize_ctx) VALUES (:id, :p, 'SLA timer', 'completed', 'done', "
                "CAST(:s AS JSONB), '{}'::jsonb)"),
                {"id": run_id, "p": pid, "s": json.dumps({"owner": bound["owner"],
                                                          "repo": bound["repo"]})})
    adapter = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"],
                              project_id=pid)
    pr = adapter.create_pull_request(branch, "Add an SLA timer", "", run_id=run_id)
    return pid, bound, bare, branch, pr, run_id


def _decision(engine, pid, url, head):
    from boss_engine.services.core import create_blocking_decision

    with engine.begin() as db:
        return create_blocking_decision(
            db, pid, None, "Approve code change: Add an SLA timer (change #1)",
            {"action": "review_pull_request", "pull_request_url": url, "head_sha": head,
             "branch_name": "x", "title": "Add an SLA timer"}, {}, "review")


# ── the file view ─────────────────────────────────────────────────────────────────────────────

def test_the_code_tab_reads_the_tree_and_whole_files_at_head_and_base(client, world, engine,
                                                                      repo_root):
    pid, _b, bare, _branch, _pr, _r = _project_with_change(engine, world)
    with _viewer(world):
        head = client.get(f"/v1/projects/{pid}/local-changes/1/tree")
        base = client.get(f"/v1/projects/{pid}/local-changes/1/tree", params={"ref": "base"})
        f_head = client.get(f"/v1/projects/{pid}/local-changes/1/file",
                            params={"path": "src/app.js"})
        f_base = client.get(f"/v1/projects/{pid}/local-changes/1/file",
                            params={"path": "src/app.js", "ref": "base"})
        png = client.get(f"/v1/projects/{pid}/local-changes/1/file", params={"path": "logo.png"})
    assert head.status_code == 200, head.text
    items = {i["path"]: i for i in head.json()["items"]}
    assert items["src/app.js"]["status"] == "M" and items["src/sla.js"]["status"] == "A"
    assert items["README.md"]["status"] is None
    assert "src/sla.js" not in {i["path"] for i in base.json()["items"]}
    assert f_head.json()["text"].splitlines()[4] == "line five"
    assert f_head.json()["language"] == "javascript" and f_head.json()["lines"] == 10
    assert f_base.json()["text"].splitlines()[4] == "line 5"
    assert png.json()["binary"] is True and png.json()["text"] is None


def test_the_file_view_refuses_traversal_unknown_refs_and_caps_size(client, world, engine,
                                                                    repo_root):
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    local_repo.commit_files(bare, branch, {"big.txt": "x" * (local_review.FILE_CAP_BYTES + 10)},
                            "big")
    other = local_repo.commit_files(bare, "main", {"other.txt": "elsewhere\n"}, "main moved")
    with _viewer(world):
        for path in ("../../etc/passwd", "/etc/passwd", ".git/config", "src/../../x"):
            r = client.get(f"/v1/projects/{pid}/local-changes/1/file", params={"path": path})
            assert r.status_code == 422 and r.json()["error"] == "bad_path", (path, r.text)
        for ref in ("main", other, "HEAD~1", "deadbeef"):
            r = client.get(f"/v1/projects/{pid}/local-changes/1/tree", params={"ref": ref})
            assert r.status_code == 422 and r.json()["error"] == "unknown_ref", (ref, r.text)
        own = local_repo.rev_parse(bare, f"{branch}~1")
        ok = client.get(f"/v1/projects/{pid}/local-changes/1/tree", params={"ref": own})
        big = client.get(f"/v1/projects/{pid}/local-changes/1/file", params={"path": "big.txt"})
        missing = client.get(f"/v1/projects/{pid}/local-changes/1/file", params={"path": "no.js"})
    assert ok.status_code == 200
    assert big.json()["too_large"] is True and big.json()["text"] is None
    assert missing.status_code == 404


# ── comments: one store ───────────────────────────────────────────────────────────────────────

def test_a_line_comment_is_anchored_replied_and_resolved_by_a_person(client, world, engine,
                                                                     repo_root):
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    head = local_repo.branch_sha(bare, branch)
    with _owner(world):
        made = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/app.js", "line": 5, "body": "Weekends should not count.",
            "severity": "must fix"})
        bad_line = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/app.js", "line": 99, "body": "nope"})
        bad_file = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/none.js", "line": 1, "body": "nope"})
    assert made.status_code == 200, made.text
    c = made.json()
    assert (c["path"], c["line"], c["side"], c["commit_sha"]) == ("src/app.js", 5, "new", head)
    assert c["author_kind"] == "human" and c["severity"] == "major" and c["state"] == "open"
    assert c["line_text"] == "line five" and c["origin"] == "console"
    assert bad_line.status_code == 422 and "no line 99" in bad_line.json()["message"]
    assert bad_file.status_code == 422
    with _viewer(world):
        refused = client.post(f"/v1/projects/{pid}/local-changes/1/comments",
                              json={"path": "src/app.js", "line": 1, "body": "viewer"})
    assert refused.status_code == 403
    with _owner(world):
        reply = client.post(f"/v1/projects/{pid}/local-changes/1/comments/{c['id']}/reply",
                            json={"body": "Also holidays."})
        listed = client.get(f"/v1/projects/{pid}/local-changes/1/comments")
    assert reply.json()["reply_to"] == c["id"] and reply.json()["line"] == 5
    thread = listed.json()["items"][0]
    assert thread["id"] == c["id"] and [r["body"] for r in thread["replies"]] == ["Also holidays."]
    assert listed.json()["summary"]["human_open"] == 1
    with _owner(world):
        done = client.post(f"/v1/projects/{pid}/local-changes/1/comments/{c['id']}/resolve",
                           json={})
        view = client.get(f"/v1/projects/{pid}/local-changes/1")
    assert done.json()["state"] == "resolved" and done.json()["resolved_by"]
    assert view.json()["comment_summary"]["human_open"] == 0
    assert view.json()["comments"][0]["state"] == "resolved"
    where = view.json()["where"]
    assert where["branch"] == branch and where["head_sha"] == head and where["ahead"] == 2
    assert where["remote_path"] == f"/git/{pid}.git" and where["storage_path"].endswith("desk.git")


def test_a_comment_follows_its_line_and_goes_outdated_when_the_line_changes(engine, world,
                                                                           repo_root):
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    settings = get_settings()
    with engine.begin() as db:
        moves = local_review.add_comment(db, settings, project_id=pid, number=1, body="moves",
                                         author="p", path="src/app.js", line=8)
        changes = local_review.add_comment(db, settings, project_id=pid, number=1, body="changes",
                                           author="p", path="src/app.js", line=5)
    text_v2 = "top 1\ntop 2\n" + APP_V1.replace("line 5\n", "line FIVE\n")
    new_head = local_repo.commit_files(bare, branch, {"src/app.js": text_v2}, "rewrite")
    with engine.begin() as db:
        row = local_review.change_row(db, pid, 1)
        out = local_review.remap(db, settings, row, new_head)
        a = local_review.comment(db, row["id"], moves["id"])
        b = local_review.comment(db, row["id"], changes["id"])
    assert out == {"moved": 1, "outdated": 1}
    assert a["line"] == 10 and a["commit_sha"] == new_head and not a["outdated"]
    assert b["outdated"] and b["line"] == 5 and b["line_text"] == "line five"


# ── merge held while a person's comment is open ───────────────────────────────────────────────

def test_merge_is_held_while_a_persons_comment_is_open(client, world, engine, repo_root,
                                                       monkeypatch):
    pid, bound, bare, branch, pr, _r = _project_with_change(engine, world)
    _decision(engine, pid, pr.url, local_repo.branch_sha(bare, branch))
    with _owner(world):
        c = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/sla.js", "line": 1, "body": "Read hours from settings."}).json()
        view = client.get(f"/v1/projects/{pid}/local-changes/1").json()
    held = view["merge"]["held"]
    assert held["state"] == "comments_open" and held["label"] == "1 open comment"
    assert "no reply yet" in held["reason"]
    monkeypatch.setattr(main.decisions.settings, "enable_sandbox_merge", True, raising=False)
    with _owner(world):
        refused = client.post(f"/v1/projects/{pid}/local-changes/1/merge",
                              json={"force": True, "note": "ship it anyway"})
    assert refused.status_code == 409 and "comment" in refused.text.lower()
    with _owner(world):
        client.post(f"/v1/projects/{pid}/local-changes/1/comments/{c['id']}/resolve", json={})
        view = client.get(f"/v1/projects/{pid}/local-changes/1").json()
        merged = client.post(f"/v1/projects/{pid}/local-changes/1/merge", json={})
    assert view["merge"]["held"] is None
    assert merged.status_code in (200, 202), merged.text


def test_an_agents_comment_never_holds_the_merge(engine, world, repo_root):
    pid, _b, _bare, _branch, pr, _r = _project_with_change(engine, world)
    with engine.begin() as db:
        local_review.add_comment(db, get_settings(), project_id=pid, number=1, body="nit",
                                 author="vera", author_kind="agent", origin="agent",
                                 path="src/sla.js", line=1)
        assert local_review.merge_hold_for_url(db, pr.url) is None


# ── Request changes → the fix round, the agent's replies ──────────────────────────────────────

def test_request_changes_starts_a_fix_round_with_the_persons_comments_first(
        client, world, engine, repo_root, builds_start):
    from boss_engine.services import decision_actions
    from boss_engine.services.governed_code_task_service import _record_comment_responses

    pid, bound, bare, branch, pr, run_id = _project_with_change(engine, world, run=True)
    decision_id = _decision(engine, pid, pr.url, local_repo.branch_sha(bare, branch))
    with engine.begin() as db:
        card = dict(db.execute(text("SELECT * FROM decisions WHERE id = :i"),
                               {"i": decision_id}).mappings().one())
    assert "revise" in {a["action"] for a in decision_actions.allowed_actions(card)}
    with _owner(world):
        first = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/app.js", "line": 5, "body": "Skip Saturday and Sunday.",
            "severity": "must fix"}).json()
        second = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/sla.js", "line": 1, "body": "Read hours from settings.",
            "severity": "should fix"}).json()
    with _viewer(world):
        assert client.post(f"/v1/projects/{pid}/local-changes/1/request-changes",
                           json={"note": "x"}).status_code == 403
    with _owner(world):
        asked = client.post(f"/v1/projects/{pid}/local-changes/1/request-changes",
                            json={"note": "Please handle weekends."})
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "revision_requested"
    with engine.connect() as db:
        status = db.execute(text("SELECT status FROM decisions WHERE id = :i"),
                            {"i": decision_id}).scalar()
        cs = db.execute(text("SELECT run_id FROM local_change_sets WHERE project_id = :p"),
                        {"p": pid}).scalar()
        fix = db.execute(text("SELECT spec, finalize_ctx, status FROM code_task_runs WHERE id = :r"),
                         {"r": cs}).mappings().one()
        job = db.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND "
                              "kind = 'code.task'"), {"p": pid}).scalar()
    assert status == "revision_requested" and cs != run_id and job >= 1
    fctx = fix["finalize_ctx"]
    assert fctx["parent_run_id"] == run_id and fctx["local_change_set_id"]
    refs = [e["id"] for e in fctx["review_comments"]]
    assert refs == [f"lcc_{first['id']}", f"lcc_{second['id']}"]     # must fix first
    assert all(e["author_kind"] == "human" and e["required"] for e in fctx["review_comments"])
    brief = json.dumps(fix["spec"])
    assert "Please handle weekends." in brief and "REQUIRED" in brief
    assert brief.index("Skip Saturday") < brief.index("Read hours")
    assert "RESPONSE: <number> FIXED" in brief

    # The build answers (its RESPONSE lines, numbered as the brief numbered them) …
    from boss_engine.services import review_comments

    shown = review_comments.implementer_order(
        [review_comments.ReviewComment.from_dict(e) for e in fctx["review_comments"]])
    _record_comment_responses(engine, cs, pid, shown,
                              "RESPONSE: 1 FIXED — skips Sat/Sun in addWorkingHours\n"
                              "RESPONSE: 2 DISPUTED — settings load later; 4 is the default",
                              round_i=1, changed_files=["src/app.js"])
    new_head = local_repo.commit_files(bare, branch, {"src/app.js": APP_V1}, "weekends")
    # … and when the pipeline refreshes the change set, they are its replies in the threads.
    with engine.begin() as db:
        out = local_review.on_change_proposed(db, get_settings(), run_id=cs,
                                              repository=bound["full_name"], number=1)
        again = local_review.on_change_proposed(db, get_settings(), run_id=cs,
                                                repository=bound["full_name"], number=1)
        threads = {t["id"]: t for t in local_review.threads(
            db, local_review.change_row(db, pid, 1)["id"])}
    assert out["replied"] == 2 and again["replied"] == 0
    t1, t2 = threads[first["id"]], threads[second["id"]]
    assert t1["state"] == "addressed" and t2["state"] == "disputed"
    assert t1["replies"][0]["body"] == f"Changed in commit {new_head[:7]}: skips Sat/Sun in addWorkingHours"
    assert t1["replies"][0]["author_kind"] == "agent" and t2["replies"][0]["body"].startswith("Disputed:")
    # Answered is not resolved: the merge still waits for the person.
    with engine.begin() as db:
        held = local_review.merge_hold_for_url(db, pr.url)
    assert held and held["open_comments"] == 2 and "resolve" in held["reason"]


def test_request_changes_on_a_pushed_change_builds_on_that_branch(client, world, engine,
                                                                  repo_root, builds_start):
    pid, _b, _bare, branch, _pr, _r = _project_with_change(engine, world)
    with _owner(world):
        empty = client.post(f"/v1/projects/{pid}/local-changes/1/request-changes", json={})
        client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/sla.js", "line": 1, "body": "Name the constant."})
        asked = client.post(f"/v1/projects/{pid}/local-changes/1/request-changes", json={})
    assert empty.status_code == 422 and empty.json()["error"] == "note_required"
    assert asked.status_code == 200 and asked.json()["status"] == "fix_round_started"
    with engine.connect() as db:
        fix = db.execute(text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id = :r"),
                         {"r": asked.json()["run_id"]}).mappings().one()
    assert fix["finalize_ctx"]["local_change_branch"] == branch
    assert fix["spec"]["base_branch"] == branch


def test_a_github_pull_requests_review_still_offers_no_request_changes():
    from boss_engine.services import decision_actions

    card = {"status": "pending", "recommendation": {
        "action": "review_pull_request", "pull_request_url": "https://github.com/a/b/pull/1"}}
    assert "revise" not in {a["action"] for a in decision_actions.allowed_actions(card)}
    local = {"status": "pending", "recommendation": {
        "action": "review_pull_request", "pull_request_url": change_set_url("local_p/x", 1)}}
    assert decision_actions.consumes_revision(local)


def test_boss_clone_records_a_working_copy(client, world, engine, repo_root):
    pid, _b, _bare, branch, _pr, _r = _project_with_change(engine, world)
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-repo/working-copies",
                        json={"path": "~/code/desk", "branch": branch, "machine": "laptop"})
        view = client.get(f"/v1/projects/{pid}/local-changes/1").json()
    assert r.status_code == 200, r.text
    assert view["where"]["working_copies"][0]["path"] == "~/code/desk"


def test_tree_counts_comment_threads_per_file(engine, world, repo_root):
    pid, _b, _bare, _branch, _pr, _r = _project_with_change(engine, world)
    with engine.begin() as db:
        local_review.add_comment(db, get_settings(), project_id=pid, number=1, body="a",
                                 author="p", path="src/app.js", line=1)
        local_review.add_comment(db, get_settings(), project_id=pid, number=1, body="b",
                                 author="p", path="src/app.js", line=2)
        got = {i["path"]: i for i in local_review.tree(db, get_settings(), pid, 1, "head")["items"]}
    assert got["src/app.js"]["comments"] == 2 and got["src/app.js"]["open_comments"] == 2


def test_the_reconciler_catches_branches_that_moved_any_other_way(client, world, engine,
                                                                 repo_root):
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    local_repo.ensure_branch(bare, "hotfix/typo")
    local_repo.commit_files(bare, "hotfix/typo", {"README.md": "typo fixed\n"}, "Fix the typo")
    local_repo.ensure_branch(bare, "boss/code/crun_elsewhere")
    local_repo.commit_files(bare, "boss/code/crun_elsewhere", {"x.txt": "x\n"}, "a run's work")
    moved = local_repo.commit_files(bare, branch, {"src/late.js": "late\n"}, "moved outside")
    with _viewer(world):
        listed = client.get(f"/v1/projects/{pid}/local-changes").json()["items"]
        again = client.get(f"/v1/projects/{pid}/local-changes").json()["items"]
    by_branch = {i["branch"]: i for i in listed}
    assert set(by_branch) == {branch, "hotfix/typo"}            # a run's branch is the run's
    assert by_branch["hotfix/typo"]["title"] == "Fix the typo"
    assert by_branch[branch]["head_sha"] == moved
    assert len(again) == 2                                       # once, not on every read


def test_the_store_has_one_writer():
    """Nothing but local_review.add_comment inserts a comment (one code path, every surface)."""
    root = Path(__file__).resolve().parents[3]
    writers = []
    for path in list((root / "engine").rglob("*.py")) + list((root / "apps" / "api" / "app").rglob("*.py")):
        if "INSERT INTO local_change_set_comments" in path.read_text(encoding="utf-8", errors="ignore"):
            writers.append(path.name)
    assert writers == ["local_review.py"]


def test_mcp_add_review_comment_on_a_local_change_writes_the_same_store(engine, world, repo_root):
    from contextlib import contextmanager

    from boss_engine.config import Settings
    from boss_engine.tools.builtin import build_default_registry

    pid, _b, _bare, _branch, _pr, _r = _project_with_change(engine, world)

    class Ctx:
        settings = Settings()

        @contextmanager
        def begin(self):
            with engine.begin() as c:
                yield c

    said = build_default_registry().invoke(
        "add_review_comment", Ctx(), project_id=pid, actor="param",
        args={"change_number": 1, "path": "src/sla.js", "line": 1, "body": "Name it SLA_HOURS."})
    assert said["ok"] is True and said["change_number"] == 1, said
    with engine.begin() as db:
        t = local_review.threads(db, local_review.change_row(db, pid, 1)["id"])
    assert [(x["origin"], x["author_kind"], x["path"], x["line"]) for x in t] == [
        ("mcp", "human", "src/sla.js", 1)]
