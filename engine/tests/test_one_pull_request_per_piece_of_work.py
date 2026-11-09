"""A retry updates the open pull request; a fix after the merge is a named follow-up.

Hello World (paramg/boss-sandbox, 2026-11-08): with #236 merged (squash), a retry pushed to the
same branch and opened #237 re-proposing all of #236's files, which GitHub then refused to merge
(405). pr_follow_up.prepare_branch and GitHubAdapter.restack_after_merge are what decide it; this
drives them against an in-memory GitHub.
"""
from __future__ import annotations

import base64

from boss_engine.config import Settings
from boss_engine.services import pr_follow_up
from boss_engine.services.github_adapter import GitHubAdapter

BRANCH = "boss/code/crun_root"


class FakeGitHub(GitHubAdapter):
    def __init__(self, *, open_prs=(), closed_prs=(), compare=None, files=None):
        super().__init__(Settings(), owner="acme", repo="hello", token="t")
        self.refs = {"main": "main_sha", BRANCH: "head_sha"}
        self.open_prs, self.closed_prs = list(open_prs), list(closed_prs)
        self.compare = compare or {}
        self.files = files or {}
        self.calls: list[tuple] = []
        self.upserts: list[tuple] = []

    def _request(self, method, path, json_body=None, **_k):
        self.calls.append((method, path, json_body))
        if path == "/repos/acme/hello":
            return {"default_branch": "main"}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": self.refs[path.split("/git/ref/heads/")[1]]}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "tree_of_" + path.rsplit("/", 1)[1]}}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "restacked_tree"}
        if method == "POST" and path.endswith("/git/commits"):
            return {"sha": "restacked_commit"}
        if method == "PATCH" and "/git/refs/heads/" in path:
            self.refs[BRANCH] = json_body["sha"]
            return {"object": {"sha": json_body["sha"]}}
        if "/pulls?state=open" in path:
            return self.open_prs
        if "/pulls?state=closed" in path:
            return self.closed_prs
        if method == "PATCH" and "/pulls/" in path:
            return {}
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/acme/hello/pull/238", "number": 238}
        if "/compare/" in path:
            key = path.split("/compare/")[1]
            return self.compare[key]
        if "/contents/" in path:
            name, ref = path.split("/contents/")[1].split("?ref=")
            text = self.files[(ref, name)]
            return {"content": base64.b64encode(text.encode()).decode()}
        raise AssertionError(f"unexpected {method} {path}")

    def upsert_file(self, branch_name, path, content, message):
        self.upserts.append((branch_name, path, content, message))
        return f"commit_{len(self.upserts)}"


MERGED_236 = {"number": 236, "html_url": "https://github.com/acme/hello/pull/236",
              "merged_at": "2026-11-09T00:37:48Z", "head": {"sha": "head236"},
              "merge_commit_sha": "squash236"}


def _diverged_after_merge(**kw):
    return FakeGitHub(closed_prs=[MERGED_236], compare={
        "main_sha...head_sha": {"status": "diverged", "ahead_by": 18, "behind_by": 1},
        "head236...head_sha": {"status": "ahead", "ahead_by": 2, "files": [
            {"filename": "hello-api/serve_hello.py", "status": "modified"},
            {"filename": "runbooks/no-traffic.md", "status": "added"}]}},
        files={("head_sha", "hello-api/serve_hello.py"): "print('fixed')\n",
               ("head_sha", "runbooks/no-traffic.md"): "# No traffic\n"}, **kw)


def test_a_retry_while_the_pr_is_open_updates_that_pr_and_never_opens_another():
    gh = FakeGitHub(open_prs=[{"number": 236, "html_url": "https://github.com/acme/hello/pull/236",
                               "title": "Hello World REST API + sample"}])
    assert pr_follow_up.prepare_branch(gh, BRANCH) is None
    pr = gh.create_pull_request(BRANCH, "Hello World REST API + sample · retry 1", "body")
    assert pr.number == 236
    assert not [c for c in gh.calls if c[0] == "POST"]              # no second pull request
    assert not [c for c in gh.calls if c[0] == "PATCH" and "/git/refs/" in c[1]]


def test_a_fix_after_the_merge_is_a_named_follow_up_showing_only_its_own_change():
    gh = _diverged_after_merge()
    out = pr_follow_up.prepare_branch(gh, BRANCH, paths=["hello-api/serve_hello.py"])
    assert out["after"] == 236 and out["restacked"] is True
    # One commit on main holding only what changed after #236, and the branch moves to it in
    # ONE step: it never sits on main, where its pull request would be empty and GitHub would
    # close it (PR #237, 2026-11-09).
    force = [c for c in gh.calls if c[0] == "PATCH" and "/git/refs/heads/" in c[1]]
    assert force == [("PATCH", force[0][1], {"sha": "restacked_commit", "force": True})]
    assert gh.refs[BRANCH] == "restacked_commit" and not gh.upserts
    tree = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/git/trees")][0][2]
    assert tree["base_tree"] == "tree_of_main_sha"
    assert [(e["path"], e["content"]) for e in tree["tree"]] == [
        ("hello-api/serve_hello.py", "print('fixed')\n"), ("runbooks/no-traffic.md", "# No traffic\n")]
    commit = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/git/commits")][0][2]
    assert commit["parents"] == ["main_sha"] and "after #236" in commit["message"]
    assert out["paths"] == ["hello-api/serve_hello.py", "runbooks/no-traffic.md"]
    title = pr_follow_up.follow_up_title("Hello World REST API + sample · retry 1", 236)
    assert title == "Fix: Hello World REST API + sample after #236"
    assert pr_follow_up.follow_up_title("Follow-up: More tests — " + title, 238) == \
        "Fix: Hello World REST API + sample after #238"
    reason = pr_follow_up.follow_up_reason("Follow-up: More tests — Hello World", 236, out["paths"])
    assert reason == ("Follow-up fix after #236 merged: More tests (2 files: "
                      "hello-api/serve_hello.py, runbooks/no-traffic.md).")


def test_a_branch_already_on_its_base_is_not_restacked_again():
    gh = _diverged_after_merge()
    gh.compare["main_sha...head_sha"] = {"status": "ahead", "ahead_by": 1}
    out = pr_follow_up.prepare_branch(gh, BRANCH)
    assert out["after"] == 236 and out["restacked"] is False
    assert out["why"] == "already on its base" and gh.refs[BRANCH] == "head_sha"


def test_a_removed_file_is_left_for_an_agent_and_changes_nothing():
    gh = _diverged_after_merge()
    gh.compare["head236...head_sha"]["files"].append({"filename": "old.py", "status": "removed"})
    out = gh.restack_after_merge(BRANCH, "head236", number=236)
    assert out["restacked"] is False and "removed" in out["why"]
    assert gh.refs[BRANCH] == "head_sha" and not gh.upserts


def test_a_branch_with_no_pr_merged_opens_its_first_pr_as_before():
    assert pr_follow_up.prepare_branch(FakeGitHub(), BRANCH) is None


def test_a_host_that_cannot_tell_opens_the_pr_as_before():
    class Bare:
        pass

    assert pr_follow_up.prepare_branch(Bare(), BRANCH) is None


def test_merging_a_pr_github_already_merged_is_a_no_op_with_its_sha():
    from boss_engine.services.software_factory import SoftwareFactoryService

    class Host:
        def __init__(self, answer):
            self.answer = answer

        def pull_request_state(self, number):
            return self.answer

    sf = SoftwareFactoryService.__new__(SoftwareFactoryService)
    sf.github = Host({"state": "closed", "merged": True, "merge_commit_sha": "squash236"})
    assert sf._pull_request_settled(236) == {"state": "merged", "sha": "squash236"}
    sf.github = Host({"state": "closed", "merged": False})
    assert sf._pull_request_settled(237) == {"state": "closed"}
    sf.github = Host({"state": "open", "merged": False})
    assert sf._pull_request_settled(237) is None
