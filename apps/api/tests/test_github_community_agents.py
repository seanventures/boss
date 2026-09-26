from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from apps.api.app.services.core import emit_event
from apps.api.app.services.event_subscription_service import (
    seed_default_event_subscriptions,
)
from apps.api.app.services.github_adapter import (
    IssueCommentResult,
    PullRequestReviewResult,
)
from apps.api.app.services.github_triage_agent import GitHubTriageAgent
from apps.api.app.services.jobs import JobLeaseService


PROJECT_ID = "boss-dogfood"
SECRET = "community-webhook-secret"


class FakeGitHub:
    def __init__(self, diff: str = "") -> None:
        self.diff = diff
        self.comments: list[tuple[int, str]] = []
        self.reviews: list[dict] = []
        self.diff_reads = 0
        self.merge_calls = 0

    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult:
        self.comments.append((issue_number, body))
        return IssueCommentResult(
            url=f"https://github.test/comments/{len(self.comments)}",
            id=len(self.comments),
        )

    def create_review(
        self, pull_number: int, body: str, event: str = "COMMENT", comments=None
    ) -> PullRequestReviewResult:
        self.reviews.append(
            {
                "pull_number": pull_number,
                "body": body,
                "event": event,
                "comments": comments or [],
            }
        )
        return PullRequestReviewResult(
            url=f"https://github.test/reviews/{len(self.reviews)}",
            id=len(self.reviews),
            state="COMMENTED",
        )

    def get_pull_request_diff(self, pull_number: int) -> str:
        self.diff_reads += 1
        return self.diff

    def merge_pull_request(self, *args, **kwargs):
        self.merge_calls += 1
        raise AssertionError("community agent must never merge")


def _headers(raw: bytes, event: str, delivery: str, secret: str = SECRET) -> dict:
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


def _payload(event: str, *, action: str | None = None, sender: str = "octocat") -> dict:
    actions = {
        "issues": "opened",
        "issue_comment": "created",
        "pull_request": "opened",
        "pull_request_review": "submitted",
    }
    payload = {
        "action": action or actions.get(event, "created"),
        "repository": {"full_name": "seanheiney/boss"},
        "sender": {"login": sender, "type": "User"},
        "number": 42,
    }
    if event == "issues":
        payload["issue"] = {
            "id": 101,
            "number": 42,
            "title": "Add a contributor activity dashboard",
            "body": (
                "Build a dashboard for maintainers so that the team can see contributor "
                "activity, review latency, and whether weekly response targets are met."
            ),
            "html_url": "https://github.test/issues/42",
            "user": {"login": "octocat"},
            "labels": [{"name": "feature"}],
        }
    elif event == "issue_comment":
        payload["issue"] = {
            "id": 101,
            "number": 42,
            "title": "Add a contributor activity dashboard",
            "body": "Maintainers need an activity dashboard.",
            "html_url": "https://github.test/issues/42",
            "user": {"login": "octocat"},
            "labels": [{"name": "feature"}],
        }
        payload["comment"] = {
            "id": 102,
            "body": "It should show review latency so the team knows when response targets are met.",
            "html_url": "https://github.test/issues/42#comment-1",
            "user": {"login": "octocat"},
        }
    elif event == "pull_request":
        payload["pull_request"] = {
            "id": 201,
            "number": 42,
            "title": "Add activity dashboard",
            "body": "Adds the requested maintainer workflow.",
            "html_url": "https://github.test/pull/42",
            "user": {"login": "branch-owner"},
            "head": {"sha": "abc123", "ref": "feature/activity"},
            "base": {"ref": "main"},
            "draft": False,
        }
    elif event == "pull_request_review":
        payload["pull_request"] = {
            "id": 201,
            "number": 42,
            "title": "Add activity dashboard",
            "html_url": "https://github.test/pull/42",
            "head": {"sha": "abc123"},
        }
        payload["review"] = {
            "id": 301,
            "body": "Looks close.",
            "state": "commented",
            "commit_id": "abc123",
            "user": {"login": "reviewer"},
        }
    return payload


def _configure(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "webhook_inbound_secret", "")
    monkeypatch.setattr(settings, "webhook_inbound_secrets_json", "")
    monkeypatch.setattr(
        settings,
        "webhook_project_map_json",
        json.dumps({"seanheiney/boss": PROJECT_ID}),
    )


@pytest.mark.parametrize(
    ("event", "expected_event", "expected_job"),
    [
        ("issues", "github.issue_opened", "github.triage"),
        ("issue_comment", "github.issue_comment", "github.triage"),
        ("pull_request", "github.pr_opened", "github.pr_review"),
        ("pull_request_review", "github.pr_review", "github.pr_review"),
    ],
)
def test_github_alias_verifies_normalizes_and_queues(
    event, expected_event, expected_job, client, engine, monkeypatch
):
    settings = api_main.inbound_webhooks.settings
    _configure(settings, monkeypatch)
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
    raw = json.dumps(_payload(event), separators=(",", ":")).encode()
    response = client.post(
        "/v1/webhooks/github",
        content=raw,
        headers=_headers(raw, event, f"delivery-{event}"),
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with engine.begin() as connection:
        stored = connection.execute(
            text("SELECT event_type, data FROM events WHERE id = :id"),
            {"id": event_id},
        ).mappings().one()
        job = connection.execute(
            text(
                "SELECT kind FROM jobs WHERE payload->>'event_id' = :event_id"
            ),
            {"event_id": event_id},
        ).mappings().one()
    assert stored["event_type"] == expected_event
    assert stored["data"]["normalized"]["repository"] == "seanheiney/boss"
    assert job["kind"] == expected_job


def test_github_alias_security_filtering_loop_guard_and_dedupe(
    client, engine, monkeypatch
):
    settings = api_main.inbound_webhooks.settings
    _configure(settings, monkeypatch)
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
    payload = _payload("issues")
    raw = json.dumps(payload).encode()
    assert client.post(
        "/v1/webhooks/github",
        content=raw,
        headers=_headers(raw, "issues", "bad", secret="wrong"),
    ).status_code == 401
    unsigned_headers = _headers(raw, "issues", "missing-signature")
    unsigned_headers.pop("X-Hub-Signature-256")
    assert client.post(
        "/v1/webhooks/github", content=raw, headers=unsigned_headers
    ).status_code == 401
    assert client.post(
        "/v1/webhooks/github",
        content=b"{bad json",
        headers={
            **_headers(b"{bad json", "issues", "verify-before-parse"),
            "X-Hub-Signature-256": "sha256=invalid",
        },
    ).status_code == 401
    assert client.post(
        "/v1/webhooks/github",
        content=b"{bad json",
        headers=_headers(b"{bad json", "issues", "invalid-json"),
    ).status_code == 400

    unsupported_raw = json.dumps(_payload("ping")).encode()
    assert client.post(
        "/v1/webhooks/github",
        content=unsupported_raw,
        headers=_headers(unsupported_raw, "ping", "unsupported"),
    ).status_code == 204
    bot_raw = json.dumps(_payload("issues", sender="boss[bot]")).encode()
    assert client.post(
        "/v1/webhooks/github",
        content=bot_raw,
        headers=_headers(bot_raw, "issues", "self-bot"),
    ).status_code == 204

    headers = _headers(raw, "issues", "dedupe")
    first = client.post("/v1/webhooks/github", content=raw, headers=headers)
    second = client.post("/v1/webhooks/github", content=raw, headers=headers)
    assert first.status_code == second.status_code == 202
    assert second.json()["duplicate"] is True
    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM jobs WHERE payload->>'event_id' = :event_id"),
            {"event_id": first.json()["event_id"]},
        ).scalar_one()
    assert count == 1

    monkeypatch.setattr(settings, "github_webhook_secret", None)
    assert client.post(
        "/v1/webhooks/github",
        content=raw,
        headers=_headers(raw, "issues", "unconfigured"),
    ).status_code == 503


def test_triage_accept_posts_once_and_approval_dispatches_plan(
    client, engine, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", None)
    fake = FakeGitHub()
    monkeypatch.setattr(api_main.github_community, "adapter_factory", lambda _: fake)
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
        event_id = emit_event(
            connection,
            PROJECT_ID,
            "github.issue_opened",
            "github",
            "issue",
            "101",
            {
                "data_boundary": "internal",
                "normalized": api_main.inbound_webhooks._normalize_github_payload(
                    "issues", _payload("issues")
                ),
            },
        )
        lease = JobLeaseService(settings).claim_job(
            connection, "community-worker", ["github.triage"], 60
        )
    assert lease["payload"]["event_id"] == event_id
    execute_payload = {
        "project_id": PROJECT_ID,
        "worker_id": "community-worker",
        "lease_token": lease["lease_token"],
    }
    result = client.post(
        f"/v1/github/community/jobs/{lease['id']}/execute", json=execute_payload
    )
    assert result.status_code == 200
    assert result.json()["verdict"] == "accept"
    assert len(fake.comments) == 1
    assert "Work has not started" in fake.comments[0][1]
    assert "Maintainer decision:" not in fake.comments[0][1]
    replay = client.post(
        f"/v1/github/community/jobs/{lease['id']}/execute", json=execute_payload
    )
    assert replay.json()["status"] == "reused"
    assert len(fake.comments) == 1

    decision_id = result.json()["decision_id"]
    plan_id = result.json()["plan_id"]
    with engine.begin() as connection:
        decision = connection.execute(
            text(
                "SELECT default_action, status, recommendation FROM decisions WHERE id=:id"
            ),
            {"id": decision_id},
        ).mappings().one()
        child_count = connection.execute(
            text(
                "SELECT count(*) FROM tasks WHERE parent_task_id = "
                "(SELECT task_id FROM plans WHERE id=:plan_id)"
            ),
            {"plan_id": plan_id},
        ).scalar_one()
    assert decision["default_action"] == "hold"
    assert decision["recommendation"]["plan_id"] == plan_id
    assert child_count == 0
    approved = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Proceed with planning."},
    )
    assert approved.status_code == 200
    with engine.begin() as connection:
        plan = connection.execute(
            text("SELECT status FROM plans WHERE id=:id"), {"id": plan_id}
        ).scalar_one()
        child_count = connection.execute(
            text(
                "SELECT count(*) FROM tasks WHERE parent_task_id = "
                "(SELECT task_id FROM plans WHERE id=:plan_id)"
            ),
            {"plan_id": plan_id},
        ).scalar_one()
    assert plan == "dispatched"
    assert child_count > 0


def test_triage_needs_info_and_bug_paths_post_one_comment_each(engine):
    settings = get_settings()
    fake = FakeGitHub()
    agent = GitHubTriageAgent(settings, lambda _: fake)
    base = {
        "repository": "seanheiney/boss",
        "sender": {"login": "octocat", "type": "User"},
        "number": 8,
    }
    with engine.begin() as connection:
        needs = agent.execute(
            connection,
            project_id=PROJECT_ID,
            event_id="evt-needs-info",
            normalized={
                **base,
                "issue": {"number": 8, "title": "Add dark mode", "body": ""},
            },
            job_id=None,
        )
        bug = agent.execute(
            connection,
            project_id=PROJECT_ID,
            event_id="evt-bug",
            normalized={
                **base,
                "number": 9,
                "issue": {
                    "number": 9,
                    "title": "Dashboard crashes",
                    "body": "The dashboard crashes with an exception for every maintainer.",
                },
            },
            job_id=None,
        )
    assert needs["verdict"] == "needs-info"
    assert bug["verdict"] == "maintainer-review"
    assert len(fake.comments) == 2
    assert "Please clarify" in fake.comments[0][1]
    assert "maintainer will review" in fake.comments[1][1]


def test_community_issue_forms_route_reports_without_auto_work():
    agent = GitHubTriageAgent(get_settings())
    installation = agent._classify(
        "[Installation] Ubuntu setup fails when the worker starts.", []
    )
    assert installation["verdict"] == "maintainer-review"
    assert installation["intent"] == "bug_report"

    feature = agent._classify(
        "[Feature] As a maintainer, when reviewing a run, I should be able to "
        "see linked receipts so that I can verify every external effect before "
        "approving the change. Success means the receipt list opens from the run.",
        [],
    )
    assert feature["verdict"] == "accept"
    assert feature["intent"] == "feature_request"

    bug_related_feature = agent._classify(
        "[Feature] Show linked bug reports on each project task. "
        "Success means the task detail opens the related bug report.", []
    )
    assert bug_related_feature["intent"] == "feature_request"


def test_pr_review_is_inline_idempotent_receipted_and_never_merges(
    client, engine, monkeypatch
):
    settings = get_settings()
    diff = """diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 def run():
+    token = "hardcoded-secret"
+    return token
"""
    fake = FakeGitHub(diff)
    monkeypatch.setattr(api_main.github_community, "adapter_factory", lambda _: fake)
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
        event_id = emit_event(
            connection,
            PROJECT_ID,
            "github.pr_opened",
            "github",
            "pull_request",
            "201",
            {
                "data_boundary": "internal",
                "normalized": api_main.inbound_webhooks._normalize_github_payload(
                    "pull_request", _payload("pull_request")
                ),
            },
        )
        lease = JobLeaseService(settings).claim_job(
            connection, "community-worker", ["github.pr_review"], 60
        )
    assert lease["payload"]["event_id"] == event_id
    payload = {
        "project_id": PROJECT_ID,
        "worker_id": "community-worker",
        "lease_token": lease["lease_token"],
    }
    first = client.post(
        f"/v1/github/community/jobs/{lease['id']}/execute", json=payload
    )
    assert first.status_code == 200
    assert first.json()["inline_finding_count"] == 1
    assert fake.diff_reads == 1
    assert len(fake.reviews) == 1
    assert fake.reviews[0]["event"] == "COMMENT"
    assert fake.reviews[0]["comments"][0] == {
        "path": "app.py",
        "line": 2,
        "side": "RIGHT",
        "body": "This appears to hardcode a credential. Use a SecretRef/configured secret boundary and keep the value out of source control.",
    }
    assert len(fake.comments) == 1
    assert fake.comments[0][1].startswith("@branch-owner")
    replay = client.post(
        f"/v1/github/community/jobs/{lease['id']}/execute", json=payload
    )
    assert replay.json()["status"] == "reused"
    assert fake.diff_reads == len(fake.reviews) == len(fake.comments) == 1

    decision_id = first.json()["decision_id"]
    with engine.begin() as connection:
        decision = connection.execute(
            text(
                "SELECT default_action, recommendation FROM decisions WHERE id=:id"
            ),
            {"id": decision_id},
        ).mappings().one()
        receipt_kinds = set(
            connection.execute(
                text("SELECT effect_kind FROM receipts WHERE project_id=:project_id"),
                {"project_id": PROJECT_ID},
            ).scalars().all()
        )
    assert decision["default_action"] == "hold"
    assert decision["recommendation"]["head_sha"] == "abc123"
    assert receipt_kinds >= {
        "github.pull_request.diff.read",
        "github.pull_request.review.create",
        "github.issue_comment.create",
    }
    approved = client.post(
        f"/v1/decisions/{decision_id}/approve",
        json={"actor": "ceo", "note": "Authorized for manual merge review."},
    )
    assert approved.status_code == 200
    with engine.begin() as connection:
        authorized = connection.execute(
            text(
                "SELECT count(*) FROM events "
                "WHERE event_type='github.community_merge_authorized' "
                "AND entity_id=:decision_id"
            ),
            {"decision_id": decision_id},
        ).scalar_one()
    assert authorized == 1
    assert fake.merge_calls == 0


def test_community_job_endpoint_rejects_bad_lease(client, engine):
    settings = get_settings()
    with engine.begin() as connection:
        job = JobLeaseService(settings).create_job(
            connection,
            project_id=PROJECT_ID,
            task_id=None,
            kind="github.triage",
            payload={"event_id": "evt", "event_data": {"normalized": {"x": 1}}},
        )
        lease = JobLeaseService(settings).claim_job(
            connection, "community-worker", ["github.triage"], 60
        )
    assert lease["id"] == job["id"]
    response = client.post(
        f"/v1/github/community/jobs/{job['id']}/execute",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "community-worker",
            "lease_token": "wrong",
        },
    )
    assert response.status_code == 409
