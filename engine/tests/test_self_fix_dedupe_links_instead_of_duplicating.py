"""DEDUPE BY CLASS SIGNATURE (BOSSAPP-571 R11, spec §7.4, F13): before a new upstream PR, BOSS
searches upstream for the same cause. A merged fix → nothing to send (In release); an open PR →
evidence added to it, not a duplicate; a closed PR → said, with its link; an open issue → the new
PR fixes it; nothing → a new PR. GitHub not answering is said, never fatal."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_upstream as up

from ._self_fix_upstream_support import FakeGitHub, client_for, insert_verified
from ._self_fix_upstream_support import settings as _settings
from ._self_fix_support import clean_project
from .test_workflow_capability_runner import eng, project  # noqa: F401

KLASS = "deploy:port_held@stack_deploy.plan_stack_deploy"
PR = "https://github.com/seanventures/boss/pull/"


@pytest.mark.parametrize("commits, issues, state", [
    ([{"html_url": "https://github.com/seanventures/boss/commit/abc",
       "commit": {"message": "fix(deploy): retire"}}], [], "merged"),
    ([], [{"html_url": PR + "9", "number": 9, "state": "closed", "title": "t",
           "pull_request": {"merged_at": "2026-10-01T00:00:00Z"}}], "merged"),
    ([], [{"html_url": PR + "300", "number": 300, "state": "open", "title": "t",
           "pull_request": {"merged_at": None}}], "open_pr"),
    ([], [{"html_url": PR + "8", "number": 8, "state": "closed", "title": "t",
           "pull_request": {"merged_at": None}}], "closed_pr"),
    ([], [{"html_url": "https://github.com/seanventures/boss/issues/7", "number": 7,
           "state": "open", "title": "port held"}], "open_issue"),
    ([], [], "none"),
])
def test_each_upstream_state_is_told_apart(commits, issues, state):
    fake = FakeGitHub(commits=commits, issues=issues)
    found = up.dedupe(client_for(fake), "seanventures/boss", KLASS)
    assert found["state"] == state
    queries = [p for m, p, _ in fake.calls if p.startswith("/search/")]
    assert any("BOSS-Signature" in q for q in queries) and any("boss-self-fix" in q
                                                               for q in queries)
    assert all("deploy%3Aport_held%40stack_deploy.plan_stack_deploy" in q for q in queries)


def test_github_not_answering_is_said_and_never_fatal():
    found = up.dedupe(client_for(FakeGitHub(search_status=503)), "seanventures/boss", KLASS)
    assert found["state"] == "skipped" and "did not answer" in found["why"]


@pytest.fixture()
def world(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.delenv("BOSS_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setattr(up, "_names", lambda c, p: [])
    yield {"eng": eng, "project": project}
    clean_project(eng, project)


def test_an_open_pr_gets_evidence_instead_of_a_duplicate(world, monkeypatch):
    fake = FakeGitHub(fork="pat-example/boss", issues=[{
        "html_url": PR + "300", "number": 300, "state": "open", "title": "retire",
        "pull_request": {"merged_at": None}}])
    monkeypatch.setattr(up, "CLIENT", lambda c, actor: client_for(fake))
    sfx_id = insert_verified(world["eng"], world["project"])
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_test")
    assert got["channel"] == "github_comment" and got["target"]["pr_url"] == PR + "300"
    assert got["body"].startswith("+1 verified on BOSS")
    with world["eng"].begin() as c:
        sent = up.send(c, _settings(), sfx_id, got["send_id"], actor="usr_test", checked=True)
    assert sent["status"] == "sent"
    posts = [(p, b) for m, p, b in fake.calls if m == "POST"]
    assert [p for p, _ in posts] == ["/repos/seanventures/boss/issues/300/comments"], \
        "no new pull request: evidence on the open one"
    with world["eng"].begin() as c:
        events = [r[0] for r in c.execute(text(
            "SELECT event_type FROM events WHERE data->>'self_fix_id' = :i"), {"i": sfx_id})]
        row = sf.get(c, sfx_id)
    assert "self_fix.upstream_linked" in events and row["upstream_state"] == "sent"
    assert row["chip"] == "Sent upstream"


def test_a_merged_fix_is_in_release_and_nothing_is_sent(world, monkeypatch):
    fake = FakeGitHub(commits=[{"html_url": "https://github.com/seanventures/boss/commit/abc",
                                "commit": {"message": "fix"}}])
    monkeypatch.setattr(up, "CLIENT", lambda c, actor: client_for(fake))
    sfx_id = insert_verified(world["eng"], world["project"])
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_test")
        row = sf.get(c, sfx_id)
    assert got["dedupe"]["state"] == "merged" and got["channel"] == "patch_export"
    assert row["upstream_state"] == "merged" and row["chip"] == "In release"


def test_an_open_issue_is_fixed_by_the_new_pr(world, monkeypatch):
    fake = FakeGitHub(fork="pat-example/boss", issues=[{
        "html_url": "https://github.com/seanventures/boss/issues/7", "number": 7,
        "state": "open", "title": "port held"}])
    monkeypatch.setattr(up, "CLIENT", lambda c, actor: client_for(fake))
    sfx_id = insert_verified(world["eng"], world["project"])
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_test")
    assert got["channel"] == "github_pr" and "Fixes #7" in got["body"]
