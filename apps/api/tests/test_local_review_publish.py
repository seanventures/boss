"""PUBLISH CARRIES THE LOCAL REVIEW (BOSSAPP-948, P4): a published change's line comments go to its
pull request — inline where the line is in the PR's diff, otherwise in the review body with the
old line quoted; replies as replies; resolved threads resolved; nothing posted twice.

Against the plumbing gate's fake GitHub (tests/e2e/gate/remote.py), which answers a comment on a
line outside the diff as GitHub does (422), and serves the GraphQL review threads."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app import main  # noqa: F401 — the API engine ports
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_repo, local_review, transient_retry

_GATE = Path(__file__).resolve().parents[3] / "tests" / "e2e" / "gate"
if str(_GATE) not in sys.path:
    sys.path.insert(0, str(_GATE))
from remote import LocalGitHub  # noqa: E402

from apps.api.tests.test_publish_and_github_outage import (  # noqa: E402
    _empty_on_hub, _github_connection, _project, _reach)


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    monkeypatch.setattr(transient_retry, "_sleep", lambda _s: None)
    return root


@pytest.fixture
def hub(tmp_path):
    github = LocalGitHub(tmp_path / "github").start()
    yield github
    github.stop()


APP = "".join(f"line {i}\n" for i in range(1, 61))


def test_publish_carries_comments_replies_and_resolved_threads_once(engine, world, repo_root, hub):
    from boss_engine.services import local_projects, local_publish
    from boss_engine.services.local_repo_host import LocalGitAdapter

    settings = get_settings()
    pid = _project(engine, world)
    with engine.begin() as db:
        bound = local_projects.create_for_project(db, settings, pid, name="desk", actor="t")
    adapter = LocalGitAdapter(settings, owner=bound["owner"], repo=bound["repo"], project_id=pid,
                              engine=engine)
    adapter.commit_files("main", {"src/app.js": APP}, "app")
    adapter.ensure_branch("feature/sla")
    first = adapter.commit_files("feature/sla", {"src/app.js": APP.replace("line 31\n", "line 31 v1\n")},
                                 "v1")
    adapter.create_pull_request("feature/sla", "Add an SLA timer", "")
    with engine.begin() as db:
        put = lambda **kw: local_review.add_comment(db, settings, project_id=pid, number=1,  # noqa: E731
                                                    author="param", **kw)
        stale = put(body="This line will change.", path="src/app.js", line=31)
    # The head moves: the stale comment's line changed (outdated); the next ones are on the head.
    adapter.commit_files("feature/sla", {"src/app.js": APP.replace("line 31\n", "line 31 v2\n")}, "v2")
    with engine.begin() as db:
        local_review.remap(db, settings, local_review.change_row(db, pid, 1))
        put = lambda **kw: local_review.add_comment(db, settings, project_id=pid, number=1,  # noqa: E731
                                                    author="param", **kw)
        resolved = put(body="Read the hours from settings.", path="src/app.js", line=31)
        local_review.add_comment(db, settings, project_id=pid, number=1, author="forge",
                                 author_kind="agent", origin="agent", reply_to=resolved["id"],
                                 body="Changed in commit abc1234: hours from settings.")
        local_review.resolve(db, project_id=pid, number=1, comment_id=resolved["id"], actor="param")
        open_inline = put(body="Weekends should not count.", path="src/app.js", line=32,
                          severity="should fix")
        outside = put(body="Rename this file.", path="src/app.js", line=2)   # far from the hunk
        put(body="A note on the whole change.")
    assert first
    gh_bare = _empty_on_hub(hub, "acme", "desk")
    assert gh_bare
    conn_id = _github_connection(engine, pid)
    with engine.begin() as db:
        out = local_publish.publish(db, settings, pid, owner="acme", repo="desk", actor="param",
                                    reach=_reach(hub, conn_id))
    step = next(s for s in out["steps"] if s["key"] == "comments")
    assert step["ok"] and step["posted"] == 5 and step["replies"] == 1 and step["resolved"] == 1

    reviews = [r for r in hub.world.reviews if r["repo"] == "acme/desk"]
    assert len(reviews) == 1 and reviews[0]["event"] == "COMMENT"
    body = reviews[0]["body"]
    assert "`src/app.js:31`** (outdated: the line changed since)" in body
    assert "> `line 31 v1`" in body                        # the old line, quoted
    assert "`src/app.js:2`** (not a line of this pull request's diff)" in body
    assert "**param** · local review" in body and "A note on the whole change." in body
    inline = [c for c in hub.world.review_comments if c["repo"] == "acme/desk"]
    roots = {c["line"]: c for c in inline if not c["in_reply_to"]}
    assert set(roots) == {31, 32} and all(c["side"] == "RIGHT" for c in roots.values())
    assert roots[31]["body"].startswith("**param** · local review")
    replies = [c for c in inline if c["in_reply_to"]]
    assert [(r["in_reply_to"], r["body"].split("\n")[0]) for r in replies] == [
        (roots[31]["id"], "**agent forge** · local review · " + replies[0]["body"].split(" · ")[2].split("\n")[0])]
    assert hub.world.resolved_threads == {roots[31]["id"]}
    with engine.connect() as db:
        ids = dict(db.execute(text("SELECT id, github_comment_id FROM local_change_set_comments "
                                   "WHERE project_id = :p"), {"p": pid}).all())
    assert ids[open_inline["id"]] == roots[32]["id"] and ids[outside["id"]] < 0
    assert ids[stale["id"]] < 0

    # Again: nothing new is posted. A reply made after Publish goes as a reply, once.
    with engine.begin() as db:
        again = local_publish.publish(db, settings, pid, owner="acme", repo="desk", actor="param",
                                      reach=_reach(hub, conn_id))
    assert len(hub.world.reviews) == 1 and len(hub.world.review_comments) == 3
    assert again["status"] == "published"
    with engine.begin() as db:
        local_review.add_comment(db, settings, project_id=pid, number=1, author="param",
                                 reply_to=open_inline["id"], body="Holidays too.")
        row = dict(db.execute(text("SELECT * FROM local_change_sets WHERE project_id = :p"),
                              {"p": pid}).mappings().one())
        got = local_review.publish_comments(db, settings, row, _reach(hub, conn_id).adapter_for(
            "acme", "desk"), int(row["github_pr_number"]))
    assert got["posted"] == 0 and got["replies"] == 1
    last = hub.world.review_comments[-1]
    assert last["in_reply_to"] == roots[32]["id"] and last["body"].endswith("Holidays too.")
    assert json.dumps(hub.world.reviews)                    # serialisable for the gate's world


def test_the_fake_refuses_a_comment_outside_the_diff_like_github(hub):
    """The fallback exists because GitHub answers 422 for a line outside the diff."""
    import httpx

    hub.seed("acme", "x", {"a.txt": "".join(f"{i}\n" for i in range(1, 30))})
    bare = hub.remotes.root / "acme" / "x.git"
    local_repo.ensure_branch(bare, "f")
    local_repo.commit_files(bare, "f", {"a.txt": "".join(f"{i}\n" for i in range(1, 30)).replace("15\n", "fifteen\n")}, "f")
    pr = httpx.post(f"{hub.api_url}/repos/acme/x/pulls", json={"head": "f", "base": "main", "title": "t"}).json()
    far = httpx.post(f"{hub.api_url}/repos/acme/x/pulls/{pr['number']}/reviews",
                     json={"body": "", "event": "COMMENT",
                           "comments": [{"path": "a.txt", "line": 2, "side": "RIGHT", "body": "x"}]})
    near = httpx.post(f"{hub.api_url}/repos/acme/x/pulls/{pr['number']}/reviews",
                      json={"body": "", "event": "COMMENT",
                            "comments": [{"path": "a.txt", "line": 15, "side": "RIGHT", "body": "y"}]})
    assert far.status_code == 422 and near.status_code == 200
    assert uuid  # (imported for parity with the publish tests' helpers)
