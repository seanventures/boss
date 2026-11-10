"""THE CHANGE PAGE AS A GIT-STYLE REVIEW, ONE API OVER TWO SOURCES (BOSSAPP-954; wireframe
docs/product/wireframes/agentic-factory/6-git-review.html).

LOCAL: real git in a temporary bare repository, the real database, the real routes.
GITHUB: the same routes under /github-changes/{owner}/{repo}/{n}, GitHub faked AT THE ADAPTER
(change_review.github_adapter_for returns FakeGitHub): the summary, files, compare, commits, one
commit, the tree and a file at a ref, checks, comments read back and written (as the person when
their GitHub account is linked, else by the app "for <name>"), resolve, the short cache, the stale
copy and the queued comment while GitHub is down, Fix N open comments, and Merge.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_local_change_review import APP_V1, _decision, _project_with_change
from boss_engine.services import change_review, local_repo


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("cr-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("cr-viewer")
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


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


def _viewer(world):
    return world.acting_as(world.ctx(world.viewer))


# ── the local source ──────────────────────────────────────────────────────────────────────────

def test_local_files_commits_one_commit_and_compare(client, world, engine, repo_root):
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    base = f"/v1/projects/{pid}/local-changes/1"
    with _viewer(world):
        files = client.get(f"{base}/files").json()
        commits = client.get(f"{base}/commits").json()["items"]
    assert {f["path"] for f in files["files"]} == {"src/app.js", "src/sla.js"}
    assert "line five" in files["diff"]
    assert [c["message"] for c in commits] == ["add sla", "change line 5"]   # newest first
    assert all(c["author"] and c["date"] and c["fixes"] == [] for c in commits)
    newest, oldest = commits[0]["sha"], commits[1]["sha"]
    with _viewer(world):
        one = client.get(f"{base}/commits/{oldest}").json()
        cmp_ = client.get(f"{base}/compare", params={"base": oldest, "head": newest}).json()
        since_base = client.get(f"{base}/compare", params={"base": "base", "head": oldest}).json()
        other = client.get(f"{base}/compare", params={"base": "main", "head": "head"})
    assert one["message"] == "change line 5" and [f["path"] for f in one["files"]] == ["src/app.js"]
    assert "+line five" in one["diff"] and "sla.js" not in one["diff"]
    assert [f["path"] for f in cmp_["files"]] == ["src/sla.js"]                # only what came after
    assert [f["path"] for f in since_base["files"]] == ["src/app.js"]
    assert other.status_code == 422                                          # a branch name is refused


def test_local_viewed_is_per_person_and_since_my_review(client, world, engine, repo_root):
    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    base = f"/v1/projects/{pid}/local-changes/1"
    head = local_repo.branch_sha(bare, branch)
    with _owner(world):
        r = client.post(f"{base}/viewed", json={"path": "src/app.js"})
        client.post(f"{base}/viewed", json={"path": ""})                      # "I reviewed this head"
        mine = client.get(f"{base}/viewed").json()
    assert r.status_code == 200, r.text
    assert mine["paths"] == {"src/app.js": head} and mine["reviewed_sha"] == head
    with _viewer(world):
        theirs = client.get(f"{base}/viewed").json()
        refused = client.post(f"{base}/viewed", json={"path": "src/app.js"})
    assert theirs["paths"] == {} and theirs["reviewed_sha"] is None
    assert refused.status_code == 403
    with _owner(world):
        client.post(f"{base}/viewed", json={"path": "src/app.js", "viewed": False})
        assert client.get(f"{base}/viewed").json()["paths"] == {}


def test_local_agent_pane_chat_is_kept_for_the_fix_round_or_steered(client, world, engine,
                                                                    repo_root, monkeypatch):
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda **kw: None)
    pid, _b, bare, branch, pr, run_id = _project_with_change(engine, world, run=True)
    base = f"/v1/projects/{pid}/local-changes/1"
    with _owner(world):
        c1 = client.post(f"{base}/comments", json={"path": "src/app.js", "line": 5,
                                                   "body": "Tick once a minute."}).json()
        said = client.post(f"{base}/agent/messages", json={"body": "Why before the render?"})
        pane = client.get(f"{base}/agent").json()
    assert said.status_code == 200 and said.json()["delivery"] == "queued"   # the build is done
    assert pane["run"]["run_id"] == run_id and pane["run"]["running"] is False
    assert pane["open"] == 1 and pane["fixable"] == 1
    assert pane["comments"][0]["id"] == c1["id"] and pane["comments"][0]["fixed_in"] is None
    assert [m["body"] for m in pane["messages"]] == ["Why before the render?"]
    with _owner(world):
        fixed = client.post(f"{base}/request-changes", json={"note": ""})
    assert fixed.status_code == 200, fixed.text
    with engine.connect() as db:
        fix_run = db.execute(text("SELECT run_id FROM local_change_sets WHERE project_id = :p"),
                             {"p": pid}).scalar()
        spec = db.execute(text("SELECT spec FROM code_task_runs WHERE id = :r"),
                          {"r": fix_run}).scalar()
        chat = db.execute(text("SELECT delivery, run_id FROM change_review_chat WHERE "
                               "project_id = :p"), {"p": pid}).mappings().one()
    assert "Why before the render?" in json.dumps(spec)                     # the chat went with it
    assert chat["delivery"] == "sent" and chat["run_id"] == fix_run
    # The fix run is running: a message now steers it (the steer endpoint's own path).
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET status = 'running' WHERE id = :r"), {"r": fix_run})
    with _owner(world):
        steered = client.post(f"{base}/agent/messages", json={"body": "Keep it after render."})
    assert steered.json()["delivery"] == "steered"
    with engine.connect() as db:
        guidance = db.execute(text("SELECT finalize_ctx->'pending_guidance' FROM code_task_runs "
                                   "WHERE id = :r"), {"r": fix_run}).scalar()
    assert guidance[-1]["text"] == "Keep it after render."


def test_a_comment_the_agent_fixed_says_fixed_in_its_commit(engine, world, repo_root):
    from boss_engine.config import get_settings
    from boss_engine.services import local_review

    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    head = local_repo.branch_sha(bare, branch)
    with engine.begin() as db:
        root = local_review.add_comment(db, get_settings(), project_id=pid, number=1,
                                        body="Use the calendar.", author="Param",
                                        path="src/app.js", line=5)
        local_review.add_comment(db, get_settings(), project_id=pid, number=1,
                                 body=f"Changed in commit {head[:7]}: done.", author="builder",
                                 author_kind="agent", origin="agent", reply_to=root["id"],
                                 response_commit=head)
        db.execute(text("UPDATE local_change_set_comments SET state = 'addressed', "
                        "response_commit = :h WHERE id = :i"), {"h": head, "i": root["id"]})
        src = change_review.LocalSource(db, get_settings(), pid, 1)
        thread = next(t for t in src.threads() if t["id"] == root["id"])
        commits = src.commits()
    assert thread["fixed_in"] == head
    assert commits[0]["sha"] == head and commits[0]["fixes"] == [root["id"]]


# ── the GitHub source, faked at the adapter ───────────────────────────────────────────────────

HEAD = "a" * 40
MID = "b" * 40
BASE = "c" * 40
DIFF = ("diff --git a/src/app.js b/src/app.js\n--- a/src/app.js\n+++ b/src/app.js\n"
        "@@ -1,2 +1,2 @@\n-old\n+new\n same\n")


class _Comment:
    def __init__(self, id_: int):
        self.id = id_


class FakeGitHub:
    """The GitHub adapter's methods the change page uses, with a switch that takes GitHub down."""

    def __init__(self, owner: str, repo: str, as_person: str = ""):
        self.owner, self.repo, self.settings = owner, repo, None
        self.api_base_url = "https://api.github.com"
        self.down = False
        self.calls: list[str] = []
        self.as_person = as_person
        self.posted: list[dict] = []
        self.comments = [
            {"id": 11, "in_reply_to_id": None, "path": "src/app.js", "line": 2, "original_line": 2,
             "side": "RIGHT", "commit_id": HEAD, "body": "Tick once a minute.", "user": "octo",
             "bot": False, "created_at": "2026-11-01T10:00:00Z"},
            {"id": 12, "in_reply_to_id": 11, "path": "src/app.js", "line": 2, "original_line": 2,
             "side": "RIGHT", "commit_id": HEAD, "body": "Agreed.", "user": "pat", "bot": False,
             "created_at": "2026-11-01T10:05:00Z"},
            {"id": 13, "in_reply_to_id": None, "path": "src/sla.js", "line": 1, "original_line": 1,
             "side": "RIGHT", "commit_id": HEAD,
             "body": "_for Param (via BOSS)_\n\n**Nit:** name it slaHours", "user": "boss-app[bot]",
             "bot": True, "created_at": "2026-11-01T11:00:00Z"},
        ]
        self.resolved = {11: False, 13: True}

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise httpx.ConnectError("GitHub is down")

    def pull_request_view(self, n):
        self._hit("view")
        return {"number": n, "url": f"https://github.com/{self.owner}/{self.repo}/pull/{n}",
                "title": "Add an SLA timer", "body": "Sorts the inbox by SLA.", "state": "open",
                "draft": False, "merged": False, "mergeable_state": "clean",
                "head_ref": "boss/code/crun_x", "head_sha": HEAD, "base_ref": "main",
                "base_sha": BASE, "user": "boss-app[bot]", "created_at": None, "updated_at": None}

    def pull_request_files(self, n):
        self._hit("files")
        return [{"path": "src/app.js", "status": "modified", "additions": 1, "deletions": 1},
                {"path": "src/sla.js", "status": "added", "additions": 3, "deletions": 0}]

    def get_pull_request_diff(self, n):
        self._hit("diff")
        return DIFF

    def compare_changed_files(self, b, h):
        self._hit(f"compare {b[:1]}..{h[:1]}")
        return [{"filename": "src/sla.js", "status": "added", "additions": 3, "deletions": 0}]

    def compare_diff(self, b, h):
        self._hit("compare_diff")
        return "diff --git a/src/sla.js b/src/sla.js\n"

    def pull_request_commits(self, n):
        self._hit("commits")
        return [{"sha": HEAD, "message": "Fix the copy", "author": "forge", "date": "2026-11-01"},
                {"sha": MID, "message": "Add sla.js", "author": "forge", "date": "2026-10-31"}]

    def commit_detail(self, sha):
        self._hit(f"commit {sha[:1]}")
        return {"sha": sha, "parent": MID, "author": "forge", "date": None, "message": "Fix the copy",
                "files": [{"path": "src/app.js", "status": "modified", "additions": 1,
                           "deletions": 1}], "diff": DIFF}

    def list_paths(self, ref):
        self._hit(f"tree {ref[:1]}")
        return ["README.md", "src/app.js", "src/sla.js"]

    def read_blob(self, ref, path):
        self._hit(f"blob {ref[:1]} {path}")
        return {"src/app.js": b"new\nsame\n", "logo.png": b"\x89PNG\x00"}.get(path)

    def commit_checks(self, sha):
        self._hit("checks")
        return [{"name": "ci / test", "status": "completed", "conclusion": "success", "app": "actions"}]

    def pull_request_reviews(self, n):
        self._hit("reviews")
        return [{"id": 5, "reviewer": "octo", "state": "COMMENTED", "commit_sha": HEAD,
                 "submitted_at": "2026-11-01T10:00:00Z"}]

    def pull_request_review_comments(self, n):
        self._hit("comments")
        return list(self.comments)

    def review_thread_states(self, n):
        self._hit("threads")
        return dict(self.resolved)

    def pull_request_issue_comments(self, n):
        self._hit("talk")
        return []

    def create_review_comment(self, n, *, body, commit_id, path, line, side="RIGHT"):
        self._hit("post")
        new_id = 100 + len(self.posted)
        self.posted.append({"as": self.as_person or "app", "body": body, "path": path, "line": line,
                            "commit_id": commit_id, "side": side})
        self.comments.append({"id": new_id, "in_reply_to_id": None, "path": path, "line": line,
                              "original_line": line, "side": side, "commit_id": commit_id,
                              "body": body, "user": self.as_person or "boss-app[bot]",
                              "bot": not self.as_person, "created_at": "2026-11-02T00:00:00Z"})
        return new_id

    def create_issue_comment(self, n, body):
        self._hit("post")
        self.posted.append({"as": self.as_person or "app", "body": body})
        return _Comment(900)

    def reply_to_review_comment(self, n, comment_id, body):
        self._hit("reply")
        self.posted.append({"as": self.as_person or "app", "body": body, "reply_to": comment_id})
        return 901

    def set_review_thread_resolved(self, n, comment_id, resolved):
        self._hit("resolve")
        self.resolved[int(comment_id)] = resolved
        return True


@pytest.fixture
def github(engine, world, monkeypatch):
    """A project with a GitHub repository bound, the adapter faked, and a build that opened PR #41."""
    pid = f"gh-{uuid.uuid4().hex[:8]}"
    owner, repo = "seanventures", f"helpdesk{uuid.uuid4().hex[:4]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Desk {pid[-4:]}"})
        conn = f"ghc_{uuid.uuid4().hex[:8]}"
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, created_by) VALUES (:c, :p, 'seed', 'personal_access_token', "
            "'secret:none', 'active', 'seed')"), {"c": conn, "p": pid})
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, status, created_by, host) VALUES (:id, :p, :c, :o, :r, :f, "
            "'primary', 'active', 't', 'github')"),
            {"id": f"grb_{uuid.uuid4().hex[:8]}", "p": pid, "c": conn, "o": owner, "r": repo,
             "f": f"{owner}/{repo}"})
        run_id = f"crun_{uuid.uuid4().hex[:8]}"
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx, "
            "pull_request_url, pull_request_number, usage) VALUES (:id, :p, 'SLA timer', "
            "'completed', 'done', CAST(:s AS JSONB), CAST(:f AS JSONB), :u, 41, "
            "CAST(:usage AS JSONB))"),
            {"id": run_id, "p": pid, "s": json.dumps({"owner": owner, "repo": repo}),
             "f": json.dumps({"builder_name": "Forge"}),
             "u": f"https://github.com/{owner}/{repo}/pull/41",
             "usage": json.dumps({"cost_usd_micros": 410000})})
    fake = FakeGitHub(owner, repo)
    made: list[tuple] = []

    def adapter_for(c, settings, project_id, binding_id):
        made.append((project_id, binding_id))
        return fake

    monkeypatch.setattr(change_review, "github_adapter_for", adapter_for)
    monkeypatch.setattr(change_review, "person_adapter",
                        lambda base, token: _as_person(fake, token))
    return {"pid": pid, "owner": owner, "repo": repo, "fake": fake, "run_id": run_id,
            "base": f"/v1/projects/{pid}/github-changes/{owner}/{repo}/41",
            "url": f"https://github.com/{owner}/{repo}/pull/41", "made": made}


def _as_person(fake: FakeGitHub, token: str) -> FakeGitHub:
    fake.as_person = f"person:{len(token)}"
    return fake


def test_a_github_pr_reads_like_a_local_change(client, world, github):
    g = github
    with _viewer(world):
        r = client.get(g["base"])
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["source"] == "github" and v["state"] == "open" and v["title"] == "Add an SLA timer"
    assert v["branch"] == "boss/code/crun_x" and v["base_branch"] == "main"
    assert v["github_pr_url"] == g["url"] and v["run_id"] == g["run_id"]
    assert v["clone"]["url"].endswith(f"{g['owner']}/{g['repo']}.git")
    assert [f["path"] for f in v["files"]] == ["src/app.js", "src/sla.js"] and "+new" in v["diff"]
    assert [c["sha"] for c in v["commits"]] == [HEAD, MID]
    assert v["checks"]["items"][0]["conclusion"] == "success" and v["stale"] is False
    # Comments read back: threads with replies, resolved state, and the app's "for Param".
    threads = {t["id"]: t for t in v["comments"]}
    assert threads[11]["replies"][0]["body"] == "Agreed." and threads[11]["state"] == "open"
    assert threads[13]["author"] == "Param" and threads[13]["author_kind"] == "human"
    assert threads[13]["body"] == "name it slaHours" and threads[13]["severity"] == "nit"
    assert threads[13]["state"] == "resolved"
    assert v["comment_summary"]["human_open"] == 1


def test_github_tree_file_compare_commit_and_checks(client, world, github):
    g = github
    with _viewer(world):
        tree = client.get(f"{g['base']}/tree").json()
        f = client.get(f"{g['base']}/file", params={"path": "src/app.js"}).json()
        png = client.get(f"{g['base']}/file", params={"path": "logo.png", "ref": MID}).json()
        missing = client.get(f"{g['base']}/file", params={"path": "nope.js"})
        bad = client.get(f"{g['base']}/file", params={"path": "../etc/passwd"})
        cmp_ = client.get(f"{g['base']}/compare", params={"base": MID, "head": "head"}).json()
        one = client.get(f"{g['base']}/commits/{HEAD}").json()
        checks = client.get(f"{g['base']}/checks").json()
    items = {i["path"]: i for i in tree["items"]}
    assert items["src/sla.js"]["status"] == "A" and items["src/app.js"]["status"] == "M"
    assert items["README.md"]["status"] is None and items["src/app.js"]["comments"] == 1
    assert f["text"] == "new\nsame\n" and f["language"] == "javascript" and f["lines"] == 2
    assert png["binary"] is True and png["text"] is None
    assert missing.status_code == 404 and bad.status_code == 422
    assert [x["path"] for x in cmp_["files"]] == ["src/sla.js"] and cmp_["base"] == MID
    assert one["sha"] == HEAD and one["parent"] == MID
    assert checks["checks"]["run_status"] == "completed"


def test_github_reads_are_cached_briefly_and_stale_when_github_is_down(client, world, github,
                                                                       engine):
    g = github
    with _viewer(world):
        client.get(g["base"])
        n = g["fake"].calls.count("comments")
        client.get(f"{g['base']}/comments")
    assert g["fake"].calls.count("comments") == n                         # the short cache answered
    with engine.begin() as db:
        db.execute(text("UPDATE change_review_cache SET fetched_at = now() - interval '1 hour' "
                        "WHERE project_id = :p"), {"p": g["pid"]})
    g["fake"].down = True
    with _viewer(world):
        stale = client.get(f"{g['base']}/comments").json()
        page = client.get(g["base"]).json()
    assert stale["stale"] is True and {t["id"] for t in stale["items"]} == {11, 13}
    assert page["stale"] is True and page["title"] == "Add an SLA timer"
    assert [f["path"] for f in page["files"]] == ["src/app.js", "src/sla.js"]
    # Nothing cached yet and GitHub down: said in words, not a blank page.
    with _viewer(world):
        never = client.get(f"{g['base']}/commits/{'9' * 40}")
    assert never.status_code == 503 and never.json()["error"] == "github_unreachable"


def test_a_comment_is_posted_by_the_app_for_the_person_or_as_them(client, world, github,
                                                                  monkeypatch):
    g = github
    with _owner(world):
        r = client.post(f"{g['base']}/comments", json={
            "path": "src/app.js", "line": 2, "body": "Only tickets due today.",
            "severity": "should fix"})
    assert r.status_code == 200, r.text
    posted = g["fake"].posted[-1]
    assert posted["as"] == "app" and posted["commit_id"] == HEAD and posted["side"] == "RIGHT"
    assert posted["body"].startswith("_for ") and "(via BOSS)_" in posted["body"]
    assert "**Should fix:** Only tickets due today." in posted["body"]
    # Read back: the person's comment, not the bot's.
    with _owner(world):
        back = client.get(f"{g['base']}/comments").json()["items"]
    mine = next(t for t in back if t["id"] == r.json()["id"])
    assert mine["author_kind"] == "human" and mine["body"] == "Only tickets due today."
    assert mine["severity"] == "minor"
    # Linked GitHub account: posted as the person, no prefix.
    from boss_engine.identity import vault

    class Cred:
        token = "gho_" + "x" * 10

    monkeypatch.setattr(vault, "get_credential", lambda *a, **k: Cred())
    with _owner(world):
        client.post(f"{g['base']}/comments", json={"path": "src/app.js", "line": 1, "body": "Mine."})
    assert g["fake"].posted[-1]["as"].startswith("person:")
    assert g["fake"].posted[-1]["body"] == "Mine."
    with _viewer(world):
        assert client.post(f"{g['base']}/comments", json={"body": "x"}).status_code == 403


def test_reply_resolve_and_reopen_on_github(client, world, github):
    g = github
    with _owner(world):
        reply = client.post(f"{g['base']}/comments/11/reply", json={"body": "Done in 9e4f1c2."})
        resolved = client.post(f"{g['base']}/comments/11/resolve", json={})
    assert reply.status_code == 200 and g["fake"].posted[-1]["reply_to"] == 11
    assert resolved.json()["state"] == "resolved" and g["fake"].resolved[11] is True
    with _owner(world):
        threads = {t["id"]: t for t in client.get(f"{g['base']}/comments").json()["items"]}
        reopened = client.post(f"{g['base']}/comments/11/resolve", json={"reopen": True})
        unknown = client.post(f"{g['base']}/comments/999/resolve", json={})
    assert threads[11]["state"] == "resolved"
    assert reopened.json()["state"] == "open" and g["fake"].resolved[11] is False
    assert unknown.status_code == 404


def test_a_comment_written_while_github_is_down_is_queued_then_posted(client, world, github,
                                                                       engine):
    g = github
    with _owner(world):
        client.get(f"{g['base']}/comments")                                  # a copy to fall back on
    g["fake"].down = True
    with _owner(world):
        r = client.post(f"{g['base']}/comments", json={"path": "src/app.js", "line": 2,
                                                       "body": "Queued while down."})
        shown = client.get(f"{g['base']}/comments").json()
    assert r.status_code == 200 and r.json()["queued"] is True and r.json()["id"] < 0
    queued = [t for t in shown["items"] if t.get("queued")]
    assert [t["body"] for t in queued] == ["Queued while down."] and shown["stale"] in (True, False)
    assert not g["fake"].posted
    g["fake"].down = False
    with engine.begin() as db:
        db.execute(text("DELETE FROM change_review_cache WHERE project_id = :p"), {"p": g["pid"]})
    with _owner(world):
        after = client.get(f"{g['base']}/comments").json()["items"]
    assert len(g["fake"].posted) == 1 and "Queued while down." in g["fake"].posted[0]["body"]
    assert not [t for t in after if t.get("queued")]
    assert any(t["body"] == "Queued while down." for t in after)


def test_fix_open_comments_starts_a_fix_run_and_fixed_in_reads_back(client, world, github,
                                                                    monkeypatch, engine):
    from boss_engine.services import code_task_service

    g = github
    started: dict = {}

    def followup(c, *, parent_run_id, instructions, jobs, actor, title, finalize_extra, **kw):
        new = f"crun_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
            "VALUES (:id, :p, :t, 'running', 'coding', '{}'::jsonb, CAST(:f AS JSONB))"),
            {"id": new, "p": g["pid"], "t": title, "f": json.dumps(finalize_extra)})
        started.update({"parent": parent_run_id, "brief": instructions, "extra": finalize_extra,
                        "run_id": new})
        return {"run_id": new}

    monkeypatch.setattr(code_task_service, "start_followup_task", followup)
    with _owner(world):
        client.post(f"{g['base']}/agent/messages", json={"body": "Mind the weekend."})
        pane = client.get(f"{g['base']}/agent").json()
        asked = client.post(f"{g['base']}/request-changes", json={"note": ""})
    assert pane["agent"] == "Forge" and pane["run"]["cost_usd"] == 0.41 and pane["fixable"] == 1
    assert asked.status_code == 200, asked.text
    assert started["parent"] == g["run_id"] and asked.json()["required"] == [11]
    assert [e["id"] for e in started["extra"]["review_comments"]] == ["ghc_11"]
    assert started["extra"]["github_review_fix"]["url"] == g["url"]
    assert "Tick once a minute." in started["brief"] and "Mind the weekend." in started["brief"]
    # The fix run is now the PR agent; its answer marks the comment fixed in its commit.
    with engine.begin() as db:
        db.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = jsonb_set(finalize_ctx, '{review_comments}', "
            "CAST(:rc AS JSONB)) WHERE id = :r"),
            {"r": started["run_id"], "rc": json.dumps([{
                "id": "ghc_11", "response_kind": "fixed", "response": "Once a minute now.",
                "response_commit": HEAD}])})
    with _owner(world):
        pane = client.get(f"{g['base']}/agent").json()
        commits = client.get(f"{g['base']}/commits").json()["items"]
        steer = client.post(f"{g['base']}/agent/messages", json={"body": "Also log it."})
    assert pane["run"]["run_id"] == started["run_id"]
    c11 = next(c for c in pane["comments"] if c["id"] == 11)
    assert c11["state"] == "addressed" and c11["fixed_in"] == HEAD
    assert commits[0]["fixes"] == [11]
    assert steer.json()["delivery"] == "steered"


def test_merge_a_github_pr_is_a_persons_approval_of_its_decision(client, world, github,
                                                                 monkeypatch, engine):
    g = github
    with _owner(world):
        none_yet = client.post(f"{g['base']}/merge", json={})
    assert none_yet.status_code == 409 and none_yet.json()["error"] == "no_review_decision"
    _decision(engine, g["pid"], g["url"], HEAD)
    approved: dict = {}

    def approve(decision_id, body):
        approved.update({"id": decision_id, "actor": body.actor})
        return {"status": "approved"}

    monkeypatch.setattr(main, "approve_decision_card", approve)
    with _viewer(world):
        assert client.post(f"{g['base']}/merge", json={}).status_code == 403
    with _owner(world):
        view = client.get(g["base"]).json()
        merged = client.post(f"{g['base']}/merge", json={})
    assert view["merge"]["decision_id"] and view["decision"]["status"] == "pending"
    assert merged.status_code == 200 and approved["id"] == view["merge"]["decision_id"]


def test_a_github_repository_resolves_to_its_project_and_others_are_refused(client, world, github,
                                                                            engine):
    g = github
    with _viewer(world):
        found = client.get("/v1/github-changes/resolve",
                           params={"owner": g["owner"], "repo": g["repo"]}).json()
        nobody = client.get("/v1/github-changes/resolve",
                            params={"owner": "acme", "repo": "elsewhere"}).json()
        wrong = client.get(f"/v1/projects/{g['pid']}/github-changes/acme/elsewhere/41")
    assert found["projects"] == [g["pid"]] and nobody["projects"] == []
    assert wrong.status_code == 404 and wrong.json()["error"] == "not_this_project"


# ── a local project published to GitHub keeps the same page ───────────────────────────────────

def test_a_published_local_change_opens_its_github_pr_with_the_comments_carried(
        client, world, engine, repo_root, monkeypatch):
    """The old /changes/<project>/<n> link opens the PR it became, read from GitHub; the comment
    the publish carried as a line comment reads back as the person's; the one GitHub could not
    take on a line (README, outside the diff: it went in the review body) is shown, marked kept
    from before publish."""
    from boss_engine.config import get_settings
    from boss_engine.services import local_review

    pid, bound, bare, branch, pr, _r = _project_with_change(engine, world)
    with _owner(world):
        inline = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "src/app.js", "line": 5, "body": "Skip weekends."}).json()
        loose = client.post(f"/v1/projects/{pid}/local-changes/1/comments", json={
            "path": "README.md", "line": 1, "body": "Say what SLA means."}).json()
    owner, repo = "seanventures", f"desk{uuid.uuid4().hex[:4]}"
    posted: dict = {}

    class Review:
        id = 77

    class PublishAdapter:            # local_publish's adapter, as publish_comments uses it
        def create_review(self, n, body, event, comments=None):
            posted.update({"body": body, "comments": comments or []})
            return Review()

        def review_comments_of(self, n, review_id):
            return [{"id": 501, "path": c["path"], "line": c["line"], "body": c["body"]}
                    for c in posted["comments"]]

        def reply_to_review_comment(self, *a):
            raise AssertionError("no replies here")

    with engine.begin() as db:
        row = dict(db.execute(text("SELECT * FROM local_change_sets WHERE project_id = :p"),
                              {"p": pid}).mappings().one())
        got = local_review.publish_comments(db, get_settings(), row, PublishAdapter(), 7)
        db.execute(text("UPDATE local_change_sets SET state = 'published', github_pr_url = :u, "
                        "github_pr_number = 7 WHERE id = :i"),
                   {"u": f"https://github.com/{owner}/{repo}/pull/7", "i": row["id"]})
        conn = f"ghc_{uuid.uuid4().hex[:8]}"
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, created_by) VALUES (:c, :p, 'seed', 'personal_access_token', "
            "'secret:none', 'active', 'seed')"), {"c": conn, "p": pid})
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, status, created_by, host) VALUES (:id, :p, :c, :o, :r, :f, "
            "'source', 'active', 't', 'github')"),
            {"id": f"grb_{uuid.uuid4().hex[:8]}", "p": pid, "c": conn, "o": owner, "r": repo,
             "f": f"{owner}/{repo}"})
    assert got["inline"] == 1 and got["in_body"] == 1
    fake = FakeGitHub(owner, repo)
    fake.comments = [{"id": 501, "in_reply_to_id": None, "path": "src/app.js", "line": 5,
                      "original_line": 5, "side": "RIGHT", "commit_id": HEAD,
                      "body": posted["comments"][0]["body"], "user": "boss-app[bot]", "bot": True,
                      "created_at": "2026-11-02T00:00:00Z"}]
    fake.resolved = {501: False}
    monkeypatch.setattr(change_review, "github_adapter_for", lambda *a: fake)
    with _viewer(world):
        r = client.get(f"/v1/projects/{pid}/local-changes/1")          # the OLD link
        files = client.get(f"/v1/projects/{pid}/local-changes/1/files").json()
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["source"] == "github" and v["number"] == 7 and v["repository"] == f"{owner}/{repo}"
    assert v["published_from"] == {"number": 1, "repository": bound["full_name"]}
    assert v["github_pr_url"] == f"https://github.com/{owner}/{repo}/pull/7"
    assert [f["path"] for f in files["files"]] == ["src/app.js", "src/sla.js"]    # from GitHub
    threads = {t["body"]: t for t in v["comments"]}
    carried = threads["Skip weekends."]
    assert carried["id"] == 501 and carried["author_kind"] == "human" and not carried.get("kept")
    assert carried["author"] == inline["author"]
    kept = threads["Say what SLA means."]
    assert kept["kept"] is True and kept["in_review_body"] is True and kept["id"] < 0
    assert kept["path"] == "README.md" and loose["id"] + change_review.KEPT_ID_BASE == -kept["id"]
    # GitHub down and no copy yet: the old link still shows the change, from the local record.
    fake.down = True
    with engine.begin() as db:
        db.execute(text("DELETE FROM change_review_cache WHERE project_id = :p"), {"p": pid})
    with _viewer(world):
        down = client.get(f"/v1/projects/{pid}/local-changes/1").json()
    assert down["source"] == "local" and down["stale"] is True and down["published_unreachable"]


# ── commits by sha after merge, and the project's history ─────────────────────────────────────

def test_a_merged_change_keeps_its_commits_after_the_branch_is_gone_and_history_links_it(
        client, world, engine, repo_root):
    """Merged into main with a merge commit (main moved meanwhile), the branch then deleted: the
    change page still shows All commits and each commit, by sha. The project's history (the
    default branch) lists the merge, links it to the change, and its own diff is what it
    brought in."""
    from boss_engine.config import get_settings
    from boss_engine.services.local_repo_host import LocalGitAdapter

    pid, bound, bare, branch, _pr, _r = _project_with_change(engine, world)
    commits_before = local_repo.git(["rev-list", f"main..{branch}"], git_dir=bare).split()
    local_repo.commit_files(bare, "main", {"docs/notes.md": "meanwhile on main\n"}, "main moved")
    merged = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"],
                             project_id=pid).merge_pull_request(1, "Merge change #1")
    local_repo.git(["update-ref", "-d", f"refs/heads/{branch}"], git_dir=bare)
    base = f"/v1/projects/{pid}/local-changes/1"
    with _viewer(world):
        view = client.get(base).json()
        whole = client.get(f"{base}/files").json()
        commits = client.get(f"{base}/commits").json()["items"]
        one = client.get(f"{base}/commits/{commits_before[-1]}")
        hist = client.get(f"/v1/projects/{pid}/history").json()
        merge = client.get(f"/v1/projects/{pid}/history/{merged.sha}").json()
        plain = client.get(f"/v1/projects/{pid}/history/{commits_before[-1]}").json()
        missing = client.get(f"/v1/projects/{pid}/history/{'0' * 40}")
    assert view["state"] == "merged"
    assert {f["path"] for f in whole["files"]} == {"src/app.js", "src/sla.js"}     # All commits
    assert [c["sha"] for c in commits] == commits_before
    assert commits[-1]["additions"] == 1 and commits[-1]["deletions"] == 1 and commits[-1]["files"] == 1
    assert one.status_code == 200, one.text
    assert [f["path"] for f in one.json()["files"]] == ["src/app.js"]
    assert hist["branch"] == "main" and hist["source"] == "local"
    top = hist["items"][0]
    assert top["sha"] == merged.sha and top["merge"] is True        # a merge or squash of change #1
    assert top["change"] == {"source": "local", "number": 1, "href": f"/changes/{pid}/1"}
    assert merge["merge"] is True and merge["change"]["number"] == 1
    assert {f["path"] for f in merge["files"]} == {"src/app.js", "src/sla.js"}      # what it brought in
    assert plain["merge"] is False and "+line five" in plain["diff"]
    assert missing.status_code == 404


def test_a_github_projects_history_links_the_pr_a_merge_brought_in(client, world, github):
    g = github
    merge_sha, first = "d" * 40, "e" * 40

    def branch_commits(branch, limit=50):
        g["fake"]._hit(f"history {branch}")
        return [{"sha": merge_sha, "parents": [first, HEAD], "author": "pat", "date": "2026-11-03",
                 "message": "Merge pull request #41 from seanventures/boss/code/crun_x"},
                {"sha": first, "parents": ["f" * 40], "author": "pat", "date": "2026-11-01",
                 "message": "Readme"}]

    g["fake"].branch_commits = branch_commits
    plain_detail = g["fake"].commit_detail
    g["fake"].commit_detail = lambda sha: {**plain_detail(sha), "sha": sha, "parents": [first, HEAD],
                                           "message": "Merge pull request #41 from x"}
    with engine_free_viewer(world):
        hist = client.get(f"/v1/projects/{g['pid']}/history").json()
        one = client.get(f"/v1/projects/{g['pid']}/history/{merge_sha}").json()
    assert hist["source"] == "github" and hist["repository"].endswith(g["repo"])
    assert hist["items"][0]["merge"] is True
    assert hist["items"][0]["change"]["href"] == f"/changes/{g['pid']}/41?repo={g['owner']}/{g['repo']}"
    assert hist["items"][1]["merge"] is False and hist["items"][1]["change"] is None
    assert one["change"]["number"] == 41 and [f["path"] for f in one["files"]] == ["src/app.js"]


def engine_free_viewer(world):
    return _viewer(world)


# ── a comment shows its code; a changed line makes it outdated ────────────────────────────────

def test_a_comment_carries_its_code_and_goes_outdated_with_the_code_as_it_was(
        client, world, engine, repo_root):
    from boss_engine.config import get_settings
    from boss_engine.services import local_review

    pid, _b, bare, branch, _pr, _r = _project_with_change(engine, world)
    made_on = local_repo.branch_sha(bare, branch)
    base = f"/v1/projects/{pid}/local-changes/1"
    with _owner(world):
        c = client.post(f"{base}/comments", json={"path": "src/app.js", "line": 5,
                                                  "body": "Name it properly."}).json()
        general = client.post(f"{base}/comments", json={"body": "Looks good overall."}).json()
        threads = {t["id"]: t for t in client.get(base).json()["comments"]}
    snip = threads[c["id"]]["snippet"]
    assert snip["start"] == 3 and snip["line"] == 5 and snip["commit"] == made_on
    assert snip["lines"] == ["line 3", "line 4", "line five", "line 6", "line 7"]
    assert threads[general["id"]]["snippet"] is None and threads[general["id"]]["path"] is None
    # A later commit changes that line: the thread is outdated, its snippet the code it was about.
    local_repo.commit_files(bare, branch, {"src/app.js": APP_V1.replace("line 5\n", "line 5!\n")}, "rename")
    with engine.begin() as db:
        row = local_review.change_row(db, pid, 1)
        local_review.remap(db, get_settings(), row)
    with _owner(world):
        again = {t["id"]: t for t in client.get(base).json()["comments"]}[c["id"]]
    assert again["outdated"] is True and again["commit_sha"] == made_on
    assert again["snippet"]["lines"][2] == "line five" and again["snippet"]["commit"] == made_on


def test_a_github_review_comment_shows_its_diff_hunk_and_its_original_commit(client, world, github):
    g = github
    g["fake"].comments[0].update({
        "diff_hunk": "@@ -1,3 +1,3 @@\n a\n-old\n+new\n same", "line": None, "original_line": 2,
        "original_commit_id": MID})
    with _viewer(world):
        t = {x["id"]: x for x in client.get(f"{g['base']}/comments").json()["items"]}[11]
    assert t["outdated"] is True and t["made_on"] == MID
    assert t["snippet"]["lines"] == ["a", "old", "new", "same"] and t["snippet"]["marks"] == [" ", "-", "+", " "]
    assert t["snippet"]["commit"] == MID and t["snippet"]["line"] == 2
