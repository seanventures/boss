from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services import event_dispatch
from apps.api.app.services.core import emit_event, make_id
from apps.api.app.services.event_dispatch import dispatch_event
from apps.api.app.services.event_subscription_service import (
    create_event_subscription,
    event_type_matches,
    list_event_subscriptions,
    seed_default_event_subscriptions,
)
from apps.api.app.services.feature_catalog import (
    FeatureCatalog,
    parse_traceability,
)
from apps.api.app.services.jobs import JobLeaseService, enqueue_job


PROJECT_ID = "boss-dogfood"


def test_enqueue_job_is_deduplicated_and_service_delegates(engine):
    with engine.begin() as connection:
        first = enqueue_job(
            connection,
            PROJECT_ID,
            "docs.update",
            {"event_id": "evt_test"},
            dedupe_key="event-dispatch:test",
        )
        second = enqueue_job(
            connection,
            PROJECT_ID,
            "docs.update",
            {"event_id": "evt_test"},
            dedupe_key="event-dispatch:test",
        )
        regular = JobLeaseService(get_settings()).create_job(
            connection,
            PROJECT_ID,
            "test.regular",
            {"ok": True},
        )

        assert first["created"] is True
        assert second["created"] is False
        assert first["id"] == second["id"]
        assert regular["status"] == "queued"
        assert "created" not in regular
        assert "dedupe_key" not in regular
        assert connection.execute(
            text("SELECT count(*) FROM jobs WHERE project_id = :project_id"),
            {"project_id": PROJECT_ID},
        ).scalar_one() == 2
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM events
                WHERE project_id = :project_id AND event_type = 'job.created'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one() == 2


def test_event_subscription_crud_api_and_glob_matching(client):
    created = client.post(
        "/v1/event-subscriptions",
        json={
            "project_id": PROJECT_ID,
            "event_type": "code_task.*",
            "sink_kind": "job",
            "sink_ref": "docs.update",
            "data_boundary": "internal",
            "config": {"priority": 42},
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    subscription = created.json()
    assert subscription["event_type"] == "code_task.*"
    assert event_type_matches("code_task.*", "code_task.pr_opened") is True
    assert event_type_matches("code_task.*", "plan.created") is False
    duplicate = client.post(
        "/v1/event-subscriptions",
        json={
            "project_id": PROJECT_ID,
            "event_type": "code_task.*",
            "sink_kind": "job",
            "sink_ref": "docs.update",
        },
    )
    assert duplicate.status_code == 400

    second = client.post(
        "/v1/event-subscriptions",
        json={
            "project_id": PROJECT_ID,
            "event_type": "plan.created",
            "sink_kind": "job",
            "sink_ref": "docs.update",
        },
    )
    assert second.status_code == 201
    conflicting_update = client.patch(
        f"/v1/event-subscriptions/{second.json()['id']}",
        json={"project_id": PROJECT_ID, "event_type": "code_task.*"},
    )
    assert conflicting_update.status_code == 400
    assert client.delete(
        f"/v1/event-subscriptions/{second.json()['id']}",
        params={"project_id": PROJECT_ID},
    ).status_code == 204

    listed = client.get(
        "/v1/event-subscriptions", params={"project_id": PROJECT_ID}
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [subscription["id"]]

    updated = client.patch(
        f"/v1/event-subscriptions/{subscription['id']}",
        json={
            "project_id": PROJECT_ID,
            "enabled": False,
            "actor": "ceo",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert client.get(
        "/v1/event-subscriptions",
        params={"project_id": PROJECT_ID, "enabled_only": True},
    ).json()["items"] == []

    removed = client.delete(
        f"/v1/event-subscriptions/{subscription['id']}",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert removed.status_code == 204
    missing = client.patch(
        f"/v1/event-subscriptions/{subscription['id']}",
        json={"project_id": PROJECT_ID, "enabled": True},
    )
    assert missing.status_code == 404

    invalid = client.post(
        "/v1/event-subscriptions",
        json={
            "project_id": PROJECT_ID,
            "event_type": "*",
            "sink_kind": "job",
            "sink_ref": "docs.update",
        },
    )
    assert invalid.status_code == 400


def test_dispatcher_allowlist_dedupe_and_boundary_gate(engine):
    with engine.begin() as connection:
        exact = create_event_subscription(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "job",
            "docs.update.exact",
        )
        prefix = create_event_subscription(
            connection,
            PROJECT_ID,
            "code_task.*",
            "job",
            "docs.update.prefix",
        )
        create_event_subscription(
            connection,
            PROJECT_ID,
            "code_task.*",
            "job",
            "docs.update.public",
            data_boundary="public",
        )
        create_event_subscription(
            connection,
            PROJECT_ID,
            "code_task.*",
            "job",
            "docs.update.disabled",
            enabled=False,
        )
        event_id = emit_event(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "boss",
            "code_task_run",
            "crun_dispatch",
            {"task_id": None, "data_boundary": "internal"},
        )
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM jobs
                WHERE project_id = :project_id AND kind LIKE 'docs.update.%'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one() == 2

        replay = dispatch_event(
            connection,
            event_id=event_id,
            project_id=PROJECT_ID,
            event_type="code_task.pr_opened",
            actor="boss",
            entity_type="code_task_run",
            entity_id="crun_dispatch",
            data={"data_boundary": "internal"},
        )
        assert {item["id"] for item in replay} == {
            connection.execute(
                text(
                    "SELECT id FROM jobs WHERE payload->>'subscription_id' = :id"
                ),
                {"id": exact["id"]},
            ).scalar_one(),
            connection.execute(
                text(
                    "SELECT id FROM jobs WHERE payload->>'subscription_id' = :id"
                ),
                {"id": prefix["id"]},
            ).scalar_one(),
        }
        assert all(item["created"] is False for item in replay)

        emit_event(
            connection,
            PROJECT_ID,
            "job.heartbeat",
            "worker",
            "job",
            "job_no_dispatch",
            {},
        )
        emit_event(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "boss",
            "code_task_run",
            "crun_restricted",
            {"data_boundary": "restricted"},
        )
        assert connection.execute(
            text(
                """
                SELECT count(*) FROM jobs
                WHERE project_id = :project_id AND kind LIKE 'docs.update.%'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one() == 2


def test_dispatch_failure_does_not_roll_back_canonical_event(
    engine, monkeypatch, caplog
):
    def explode(*args, **kwargs):
        raise RuntimeError("dispatcher unavailable")

    monkeypatch.setattr(event_dispatch, "dispatch_event", explode)
    with engine.begin() as connection:
        event_id = emit_event(
            connection,
            PROJECT_ID,
            "plan.dispatched",
            "ceo",
            "plan",
            "plan_dispatch_failure",
            {},
        )

    with engine.begin() as connection:
        events = connection.execute(
            text(
                """
                SELECT event_type, entity_id FROM events
                WHERE project_id = :project_id
                  AND (id = :event_id OR entity_id = :event_id)
                ORDER BY created_at
                """
            ),
            {"project_id": PROJECT_ID, "event_id": event_id},
        ).mappings().all()
    assert {row["event_type"] for row in events} == {
        "plan.dispatched",
        "job.dispatch_failed",
    }
    assert "event_dispatch.failed" in caplog.text


def test_default_subscriptions_prove_internal_reactive_loop(engine):
    with engine.begin() as connection:
        defaults = seed_default_event_subscriptions(connection, PROJECT_ID)
        assert len(defaults) == 5
        assert len(list_event_subscriptions(connection, PROJECT_ID)) == 5
        emit_event(
            connection,
            PROJECT_ID,
            "code_task.completed",
            "boss",
            "code_task_run",
            "crun_default_refresh",
            {"data_boundary": "internal"},
        )
        refresh = connection.execute(
            text(
                """
                SELECT kind, status FROM jobs
                WHERE project_id = :project_id AND kind = 'docs.refresh'
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().one()
        assert refresh == {"kind": "docs.refresh", "status": "queued"}
        event_id = emit_event(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "boss",
            "code_task_run",
            "crun_default_docs",
            {"data_boundary": "internal"},
        )
        job = connection.execute(
            text(
                """
                SELECT kind, payload FROM jobs
                WHERE project_id = :project_id AND kind = 'docs.update'
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().one()
        assert job["payload"]["event_id"] == event_id
        assert job["payload"]["entity_id"] == "crun_default_docs"


def test_feature_catalog_seed_resolve_emergent_and_idempotency(engine):
    settings = get_settings()
    catalog = FeatureCatalog(settings)
    parsed = parse_traceability(
        catalog.traceability_path.read_text(encoding="utf-8")
    )
    assert parsed
    assert any(entry["label"] == "MET-001" for entry in parsed)

    with engine.begin() as connection:
        seeded = catalog.seed_from_traceability(connection, PROJECT_ID)
        reseeded = catalog.seed_from_traceability(connection, PROJECT_ID)
        assert len(seeded) == len(parsed)
        assert len(reseeded) == len(parsed)
        assert connection.execute(
            text(
                "SELECT count(*) FROM feature_catalog WHERE project_id = :project_id"
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one() == len(parsed)

        run_id = make_id("crun")
        connection.execute(
            text(
                """
                INSERT INTO code_task_runs (
                    id, project_id, status, phase, title, spec
                )
                VALUES (
                    :id, :project_id, 'completed', 'done', :title,
                    CAST(:spec AS JSONB)
                )
                """
            ),
            {
                "id": run_id,
                "project_id": PROJECT_ID,
                "title": "Improve command intake",
                "spec": json.dumps({"requirement": "MET-001"}),
            },
        )
        resolved = catalog.resolve(
            connection,
            PROJECT_ID,
            "code_task_run",
            run_id,
            event_id="evt_documented",
        )
        assert resolved["label"] == "MET-001"
        assert resolved["needs_label"] is False
        catalog.mark_documented(
            connection,
            PROJECT_ID,
            resolved["id"],
            "evt_documented",
        )
        repeated = catalog.resolve(
            connection,
            PROJECT_ID,
            "code_task_run",
            run_id,
            event_id="evt_documented",
        )
        assert repeated["id"] == resolved["id"]
        assert repeated["idempotent"] is True
        catalog.seed_from_traceability(connection, PROJECT_ID)
        reseeded_resolved = next(
            item for item in catalog.list_entries(connection, PROJECT_ID)
            if item["label"] == "MET-001"
        )
        assert {
            "kind": "entity",
            "entity_type": "code_task_run",
            "entity_id": run_id,
        } in reseeded_resolved["source_refs"]

        plan_id = make_id("plan")
        connection.execute(
            text(
                """
                INSERT INTO plans (
                    id, project_id, title, objective, created_by
                )
                VALUES (
                    :id, :project_id, 'Reactive foundation rollout',
                    'Complete EVT-001 with acceptance evidence.', 'codex'
                )
                """
            ),
            {"id": plan_id, "project_id": PROJECT_ID},
        )
        resolved_plan = catalog.resolve(
            connection,
            PROJECT_ID,
            "plan",
            plan_id,
            event_id="evt_plan",
        )
        assert resolved_plan["label"] == "EVT-001"
        assert resolved_plan["needs_label"] is False

        discovery_id = make_id("drun")
        connection.execute(
            text(
                """
                INSERT INTO discovery_runs (
                    id, project_id, status, phase, title, seed, context
                )
                VALUES (
                    :id, :project_id, 'completed', 'done',
                    'Emergent operator insight', 'new signal', 'no requirement yet'
                )
                """
            ),
            {"id": discovery_id, "project_id": PROJECT_ID},
        )
        emergent = catalog.resolve(
            connection,
            PROJECT_ID,
            "discovery_run",
            discovery_id,
            event_id="evt_emergent",
        )
        assert emergent["label"] is None
        assert emergent["needs_label"] is True
        assert emergent["doc_path"].startswith("docs/features/")


def test_event_dispatch_module_has_no_direct_commercial_imports():
    source = event_dispatch.__file__
    assert source is not None
    text_source = Path(source).read_text(encoding="utf-8")
    for forbidden in ("budget", "entitlements", "commercial"):
        assert f"import {forbidden}" not in text_source
        assert f"from .{forbidden}" not in text_source
