"""The GitHub poller on Postgres (AMEX-G1, BOSSAPP-1175) and the pull-request file filter it
serves (AMEX-G11, BOSSAPP-1169), against a fake GitHub API.

A laptop install listens on 127.0.0.1, so GitHub's webhook never reaches it. A `poll` connection
is read by ONE poller at a time (its cursor row is locked for the poll), on its interval; each
new head commit becomes an inbox row shaped exactly like GitHub's delivery, so the processor,
loop prevention and the routes are the ones a webhook goes through. A commit that arrives both
ways is one event.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.connectors.intake.github import adapter as gh
from boss_engine.services import event_intake

from ._intake_support import (
    deploy_local_agent,
    github_pr_opened,
    github_request,
    make_project,
    make_route,
    pg_engine,
    purge,
    runs_of,
    token_of,
)
from .test_github_intake_files_and_poll import REPO, FakeGitHub, _pr

TENANT_FILTER = {"all": [
    {"field": "type", "op": "in", "value": ["com.github.pull_request.opened",
                                            "com.github.pull_request.synchronize"]},
    {"field": "data.changed_files", "op": "glob", "value": ["tenants/*.yaml", "tenants/*.yml"]},
]}
LATER = "2099-01-01T00:00:00Z"      # after the connection began polling


@pytest.fixture()
def world(monkeypatch):
    engine = pg_engine()
    pid = make_project(engine)
    event_intake.BUCKETS.reset()
    api = FakeGitHub()
    monkeypatch.setattr(gh, "TRANSPORT", api)
    try:
        yield engine, pid, api
    finally:
        purge(engine, pid)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _create(engine, pid, *, mode="poll", config=None, secrets=None):
    with engine.begin() as c:
        return event_intake.create_connection(c, project_id=pid, kind="github", actor="ceo",
                                              name="tenants", mode=mode, config=config or {},
                                              secrets=secrets or {})


def _rows(engine, connection_id):
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, dedupe_key, status, outcome, envelope FROM intake_events "
            "WHERE connection_id = :c ORDER BY received_at, id"), {"c": connection_id}).mappings()]


def _health(engine, connection_id):
    with engine.begin() as c:
        return c.execute(text("SELECT health FROM intake_connections WHERE id = :c"),
                         {"c": connection_id}).scalar()["poll"]


def _due_now(engine, connection_id):
    with engine.begin() as c:
        c.execute(text("UPDATE intake_connections SET health = health #- '{poll,next_poll_at}' "
                       "WHERE id = :c"), {"c": connection_id})


def test_poll_mode_needs_a_repository_and_no_webhook_secret(world):
    engine, pid, _ = world
    with pytest.raises(event_intake.IntakeServiceError, match="Repository"):
        _create(engine, pid, mode="poll")
    out = _create(engine, pid, mode="poll", config={"repository": REPO, "poll_seconds": 5})
    assert out["mode"] == "poll"
    assert event_intake.poll_seconds({"poll_seconds": 5}) == event_intake.POLL_SECONDS_MIN
    assert event_intake.poll_seconds({}) == event_intake.POLL_SECONDS_DEFAULT
    # Push still needs the secret its signatures are checked with.
    with pytest.raises(event_intake.IntakeServiceError, match="Webhook secret"):
        _create(engine, pid, mode="push", config={"repository": REPO})


def test_a_polled_pull_request_touching_a_tenant_file_starts_a_run(world):
    engine, pid, api = world
    dep = deploy_local_agent(engine, pid)
    conn = _create(engine, pid, config={"repository": REPO, "bot_login": "boss-localhost[bot]"},
                   secrets={"api_token": "ghp_TEST"})
    route = make_route(engine, pid, dep["id"], conn["id"], name="Tenant config PRs",
                       config={"match": TENANT_FILTER})
    api.pulls = [_pr(4, "2" * 40, updated=LATER), _pr(5, "3" * 40, updated=LATER)]
    api.files = {4: [{"filename": "tenants/lumen-pay.yaml", "status": "added"}],
                 5: [{"filename": "README.md", "status": "modified"}]}

    got = event_intake.poll_due(engine, _settings(), connection_id=conn["id"])
    assert got == {"polled": 1, "inserted": 2, "skipped": 0, "failed": 0}
    rows = _rows(engine, conn["id"])
    assert {r["dedupe_key"] for r in rows} == {f"github:poll:pr:{REPO}#4@{'2' * 40}",
                                               f"github:poll:pr:{REPO}#5@{'3' * 40}"}
    health = _health(engine, conn["id"])
    assert health["failures"] == 0 and health["last_inserted"] == 2
    assert health["interval_seconds"] == event_intake.POLL_SECONDS_DEFAULT

    # Counts are per pass over the whole inbox (a shared test database); the rows are ours.
    event_intake.process_due(engine, _settings())
    by_pr = {r["envelope"]["subject"]: r for r in _rows(engine, conn["id"])}
    tenant = by_pr[f"{REPO}#4"]
    assert tenant["status"] == "routed"
    assert tenant["envelope"]["data"]["changed_files"] == [
        {"filename": "tenants/lumen-pay.yaml", "status": "added", "additions": 0,
         "deletions": 0}]
    assert tenant["envelope"]["data"]["delivered_by"] == "poll"
    assert by_pr[f"{REPO}#5"]["status"] in ("filtered", "ignored")
    [run] = runs_of(engine, pid)
    assert run["trigger_id"] == route["id"]
    # The token reached GitHub and nothing else.
    assert all(h.get("Authorization") == "Bearer ghp_TEST" for _, h in api.calls)
    assert "ghp_TEST" not in str(_rows(engine, conn["id"]))

    # Not due again until its interval has passed.
    assert event_intake.poll_due(engine, _settings(), connection_id=conn["id"])["skipped"] == 1


def test_the_same_commit_by_webhook_and_by_poll_is_one_event(world):
    engine, pid, api = world
    dep = deploy_local_agent(engine, pid)
    conn = _create(engine, pid, config={"repository": REPO},
                   secrets={"webhook_secret": "gh-s", "api_token": "ghp_TEST"})
    make_route(engine, pid, dep["id"], conn["id"], config={"match": TENANT_FILTER})
    api.files = {4: [{"filename": "tenants/lumen-pay.yaml", "status": "added"}]}
    body = github_pr_opened(4, repo=REPO)
    got = event_intake.receive(engine, "github", token_of(conn["url_path"]),
                               github_request("gh-s", "pull_request", body, delivery="d-hook"))
    assert got["http_status"] == 202
    event_intake.process_due(engine, _settings())
    assert _rows(engine, conn["id"])[0]["status"] == "routed"

    # The poller then sees the same head commit.
    api.pulls = [{**_pr(4, body["pull_request"]["head"]["sha"], updated=LATER)}]
    assert event_intake.poll_due(engine, _settings(), connection_id=conn["id"])["inserted"] == 1
    event_intake.process_due(engine, _settings())
    rows = _rows(engine, conn["id"])
    polled = [r for r in rows if r["dedupe_key"].startswith("github:poll:")][0]
    assert polled["status"] == "filtered"
    assert any(o.get("duplicate_of") == rows[0]["id"] for o in polled["outcome"])
    assert len(runs_of(engine, pid)) == 1

    # A NEW commit by poll is a new event.
    _due_now(engine, conn["id"])
    api.etag = {"pulls": 'W/"p9"', "branches": 'W/"b9"'}
    api.pulls = [_pr(4, "8" * 40, updated=LATER)]
    assert event_intake.poll_due(engine, _settings(), connection_id=conn["id"])["inserted"] == 1
    event_intake.process_due(engine, _settings())
    assert _rows(engine, conn["id"])[-1]["status"] == "routed"
    assert len(runs_of(engine, pid)) == 2


def test_one_poller_per_connection_and_a_refusal_backs_off(world):
    engine, pid, api = world
    conn = _create(engine, pid, config={"repository": REPO})
    assert event_intake.poll_due(engine, _settings(), connection_id=conn["id"])["polled"] == 1      # the baseline
    _due_now(engine, conn["id"])
    # Another worker holds this connection's cursor: this one skips it.
    with engine.connect() as other:
        tx = other.begin()
        other.execute(text("SELECT 1 FROM intake_cursors WHERE connection_id = :c "
                           "FOR UPDATE"), {"c": conn["id"]})
        got = event_intake.poll_due(engine, _settings(), connection_id=conn["id"])
        tx.rollback()
    assert got["polled"] == 0 and got["skipped"] == 1

    api.answer = (401, {"message": "Bad credentials"}, {})
    got = event_intake.poll_due(engine, _settings(), connection_id=conn["id"])
    assert got["failed"] == 1
    health = _health(engine, conn["id"])
    assert health["failures"] == 1 and health["last_error"].startswith("auth_failed")
    assert event_intake.poll_due(engine, _settings(), connection_id=conn["id"])["skipped"] == 1, "backing off"
    api.answer = None
    _due_now(engine, conn["id"])
    assert event_intake.poll_due(engine, _settings(), connection_id=conn["id"])["polled"] == 1
    assert _health(engine, conn["id"])["failures"] == 0
