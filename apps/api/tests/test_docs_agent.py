from __future__ import annotations

from datetime import date
import json

from sqlalchemy import text

from apps.api.app import main as main_module
from apps.api.app.services.core import emit_event, make_id
from apps.api.app.services.docs_agent import (
    DocsDraft,
    MANAGED_END,
    MANAGED_START,
    merge_managed_markdown,
    reconcile_traceability,
    validate_markdown_path,
)
from apps.api.app.services.event_subscription_service import (
    seed_default_event_subscriptions,
)
from apps.api.app.services.github_adapter import PullRequestResult


PROJECT_ID = "boss-dogfood"


class FakeDocsAgent:
    def __init__(self):
        self.calls = []

    def draft(self, feature, context, prior_markdown):
        self.calls.append((feature, context, prior_markdown))
        generated = """# CEO command intake
> status: Partial · source: MET-001

## What it does
Turns an executive command into governed work.

## How it works
The intake service creates canonical records and audit events.

## API / interface
Use `POST /v1/commands/propose`.

## Configuration
No feature-specific configuration.

## Status & limitations
The triggering pull request still requires human review.
"""
        return DocsDraft(
            markdown=merge_managed_markdown(prior_markdown, generated),
            usage={"model": "fake-docs"},
            cost_usd_micros=12_000,
            model="fake-docs",
        )


class FakeGitHub:
    repository_full_name = "example/boss"

    def __init__(self):
        self.files = {}
        self.branches = []
        self.pr_bodies = []

    def read_text_file(self, branch_name, path):
        if path.startswith("docs/features/"):
            return "# Human overview\n\nKeep this operator note exactly.\n"
        return None

    def ensure_branch(self, branch_name):
        self.branches.append(branch_name)
        return "base-sha"

    def upsert_file(self, branch_name, path, content, message):
        self.files[path] = content
        return f"sha-{len(self.files)}"

    def create_pull_request(self, branch_name, title, body):
        self.pr_bodies.append(body)
        return PullRequestResult(url="https://github.example/pull/42", number=42)


def _insert_code_task_run(engine, run_id: str) -> None:
    with engine.begin() as connection:
        main_module.docs_updates.catalog.seed_from_traceability(
            connection, PROJECT_ID
        )
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, status, phase, title, spec, state,
                    pull_request_url, pull_request_number
                )
                VALUES (
                    :id, :project_id, 'completed', 'done', :title,
                    CAST(:spec AS JSONB), CAST(:state AS JSONB), :pr_url, 41
                )
                """
            ),
            {
                "id": run_id,
                "project_id": PROJECT_ID,
                "title": "MET-001 improve command intake",
                "spec": json.dumps(
                    {
                        "requirement": "MET-001",
                        "instructions": "Improve command validation.",
                    }
                ),
                "state": json.dumps(
                    {
                        "impl_files": ["apps/api/app/services/intake.py"],
                        "test_files": ["apps/api/tests/test_api.py"],
                    }
                ),
                "pr_url": "https://github.example/pull/41",
            },
        )


def test_managed_markdown_preserves_human_prose_and_rejects_non_markdown():
    prior = (
        "# Human title\n\nKeep before.\n\n"
        f"{MANAGED_START}\nold generated text\n{MANAGED_END}\n\nKeep after.\n"
    )
    generated = """# Generated title
## What it does
One.
## How it works
Two.
## API / interface
Three.
## Configuration
Four.
## Status & limitations
Five.
"""
    merged = merge_managed_markdown(prior, generated)
    assert "Keep before." in merged
    assert "Keep after." in merged
    assert "old generated text" not in merged
    assert merged.count(MANAGED_START) == 1
    assert validate_markdown_path("docs/features/example.md") == "docs/features/example.md"
    for path in ("docs/features/example.html", "../secret.md", "/tmp/output.md"):
        try:
            validate_markdown_path(path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path accepted: {path}")


def test_traceability_reconciliation_updates_only_label_and_freshness():
    source = """# Traceability

Last updated: June 3, 2026

| Requirement label | Source | Implementation files | Test coverage | Status |
|---|---|---|---|---|
| MET-001 CEO command intake | PRD | `intake.py` | `test.py` | Planned |
| MET-002 Event feed | PRD | `events.py` | `test.py` | Implemented |
"""
    result = reconcile_traceability(
        source, "MET-001", "Partial", today=date(2026, 7, 15)
    )
    assert "Last updated: July 15, 2026" in result
    assert "| MET-001 CEO command intake | PRD | `intake.py` | `test.py` | Partial |" in result
    assert "| MET-002 Event feed | PRD | `events.py` | `test.py` | Implemented |" in result


def test_manual_docs_job_executes_budgeted_receipted_draft_pr(
    client, engine, monkeypatch
):
    run_id = make_id("crun")
    _insert_code_task_run(engine, run_id)
    fake_agent = FakeDocsAgent()
    fake_github = FakeGitHub()
    monkeypatch.setattr(main_module.docs_updates, "agent", fake_agent)
    monkeypatch.setattr(main_module.docs_updates, "github", fake_github)

    request = {
        "project_id": PROJECT_ID,
        "event_id": "evt_docs_manual",
        "event_type": "code_task.pr_opened",
        "entity_type": "code_task_run",
        "entity_id": run_id,
        "data_boundary": "internal",
    }
    first = client.post("/v1/docs/update", json=request)
    second = client.post("/v1/docs/update", json=request)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    executed = client.post(
        "/v1/docs/update/execute",
        json={**request, "created_by_job_id": first.json()["id"]},
    )
    assert executed.status_code == 200
    payload = executed.json()
    assert payload["status"] == "succeeded"
    assert payload["pull_request_url"] == "https://github.example/pull/42"
    assert payload["branch_name"].startswith("boss/docs/met-001-")
    assert len(fake_agent.calls) == 1
    assert fake_agent.calls[0][1]["source_entity"]["state"]["impl_files"] == [
        "apps/api/app/services/intake.py"
    ]
    doc = fake_github.files[payload["doc_path"]]
    assert "Keep this operator note exactly." in doc
    assert "## Status & limitations" in doc
    assert "docs/product/TRACEABILITY.md" in fake_github.files
    assert "MET-001" in fake_github.files["docs/product/TRACEABILITY.md"]
    assert "Human review and merge are required" in fake_github.pr_bodies[0]
    assert "https://github.example/pull/41" in fake_github.pr_bodies[0]

    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                """
                SELECT status, provider_ref FROM receipts
                WHERE id = :receipt_id AND project_id = :project_id
                """
            ),
            {"receipt_id": payload["receipt_id"], "project_id": PROJECT_ID},
        ).mappings().one()
        reservation_status = connection.execute(
            text(
                """
                SELECT status FROM budget_reservations
                WHERE job_id = :job_id AND project_id = :project_id
                """
            ),
            {"job_id": first.json()["id"], "project_id": PROJECT_ID},
        ).scalar_one()
        documented = connection.execute(
            text(
                """
                SELECT status, last_documented_event_id
                FROM feature_catalog
                WHERE id = :feature_id AND project_id = :project_id
                """
            ),
            {"feature_id": payload["feature_id"], "project_id": PROJECT_ID},
        ).mappings().one()
        event_count = connection.execute(
            text(
                """
                SELECT count(*) FROM events
                WHERE project_id = :project_id AND event_type = 'docs.pr_opened'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one()
    assert receipt == {
        "status": "succeeded",
        "provider_ref": "https://github.example/pull/42",
    }
    assert reservation_status == "consumed"
    assert documented == {
        "status": "Implemented",
        "last_documented_event_id": "evt_docs_manual",
    }
    assert event_count == 1

    replay = client.post(
        "/v1/docs/update/execute",
        json={**request, "created_by_job_id": first.json()["id"]},
    )
    assert replay.json()["status"] == "reused"
    assert len(fake_agent.calls) == 1


def test_completion_events_coalesce_to_latest_entity_evidence(engine):
    run_id = make_id("crun")
    _insert_code_task_run(engine, run_id)
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
        emit_event(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "boss",
            "code_task_run",
            run_id,
            {"data_boundary": "internal"},
        )
        latest_event = emit_event(
            connection,
            PROJECT_ID,
            "software_factory.pull_request_merged",
            "boss",
            "code_task_run",
            run_id,
            {"data_boundary": "internal"},
        )
        jobs = connection.execute(
            text(
                """
                SELECT payload FROM jobs
                WHERE project_id = :project_id AND kind = 'docs.update'
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().all()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["event_id"] == latest_event
    assert jobs[0]["payload"]["event_type"] == "software_factory.pull_request_merged"


def test_merged_pull_request_artifact_resolves_to_feature_catalog(engine):
    artifact_id = make_id("art")
    with engine.begin() as connection:
        main_module.docs_updates.catalog.seed_from_traceability(
            connection, PROJECT_ID
        )
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, project_id, artifact_type, title, content, content_hash
                )
                VALUES (
                    :id, :project_id, 'pull_request_merge', :title,
                    CAST(:content AS JSONB), 'test-hash'
                )
                """
            ),
            {
                "id": artifact_id,
                "project_id": PROJECT_ID,
                "title": "MET-001 command intake merged",
                "content": json.dumps({"merged_sha": "abc123"}),
            },
        )
        resolved = main_module.docs_updates.catalog.resolve(
            connection,
            PROJECT_ID,
            "artifact",
            artifact_id,
            event_id="evt_merge_artifact",
        )
    assert resolved["label"] == "MET-001"
    assert resolved["needs_label"] is False


def test_non_allowlisted_manual_event_is_rejected(client):
    response = client.post(
        "/v1/docs/update",
        json={
            "project_id": PROJECT_ID,
            "event_id": "evt_not_allowed",
            "event_type": "task.completed",
            "entity_type": "plan",
            "entity_id": "plan_missing",
        },
    )
    assert response.status_code == 400


def test_docs_execution_blocks_confidential_external_effect(client, engine):
    response = client.post(
        "/v1/docs/update/execute",
        json={
            "project_id": PROJECT_ID,
            "event_id": "evt_confidential",
            "event_type": "discovery.completed",
            "entity_type": "discovery_run",
            "entity_id": "drun_confidential",
            "data_boundary": "confidential",
            "created_by_job_id": "job_confidential",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["decision_id"]
    with engine.begin() as connection:
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM events
                WHERE project_id = :project_id
                  AND event_type = 'policy.denied'
                  AND entity_id = 'drun_confidential'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one() == 1
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM budget_reservations
                WHERE project_id = :project_id AND job_id = 'job_confidential'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one() == 0
