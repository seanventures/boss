from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app import main as api_main
from apps.api.app.services.jobs import JobLeaseService
from apps.api.app.services.work_orchestration import WorkOrchestrationService


def test_ranked_portfolio_is_explainable_and_confidence_weighted(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)

    with engine.begin() as connection:
        high = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="customer-recovery",
            title="Restore a broken customer workflow",
            strategic_value=95,
            customer_impact=100,
            urgency=95,
            cost_of_delay=90,
            risk_reduction=80,
            dependency_pressure=80,
            confidence=95,
            rationale="Production customers cannot complete the core workflow.",
            actor="product_manager",
        )
        speculative = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="speculative-polish",
            title="Speculative visual polish",
            strategic_value=60,
            customer_impact=40,
            urgency=20,
            cost_of_delay=10,
            risk_reduction=10,
            dependency_pressure=10,
            confidence=35,
            actor="product_manager",
        )
        ranked = service.list_work_items(
            connection, settings.default_project_id
        )

    assert high["effective_score"] > speculative["effective_score"]
    assert high["priority_band"] in {"p0", "p1"}
    assert ranked[0]["id"] == high["id"]
    assert high["rationale"].startswith("Production customers")


def test_allocator_grants_ranked_capacity_and_throttles_lower_work(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)

    with engine.begin() as connection:
        service.upsert_quota(
            connection,
            project_id=settings.default_project_id,
            scope_type="project",
            scope_id=settings.default_project_id,
            resource_kind="job_concurrency",
            reserved_min=0,
            soft_limit=1,
            hard_limit=1,
            actor="chief_operator",
        )
        top = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="ticket",
            subject_id="top",
            title="Top customer blocker",
            strategic_value=100,
            customer_impact=100,
            urgency=100,
            cost_of_delay=100,
            risk_reduction=80,
            dependency_pressure=90,
            confidence=100,
            actor="product_manager",
        )
        lower = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="ticket",
            subject_id="lower",
            title="Lower priority enhancement",
            strategic_value=40,
            customer_impact=30,
            urgency=20,
            cost_of_delay=20,
            risk_reduction=20,
            dependency_pressure=20,
            confidence=80,
            actor="product_manager",
        )
        plan = service.plan_allocations(
            connection,
            project_id=settings.default_project_id,
            actor="chief_operator",
        )
        by_work = {item["work_item_id"]: item for item in plan["allocations"]}

    assert by_work[top["id"]]["granted_units"] == 1
    assert by_work[top["id"]]["status"] == "active"
    assert by_work[lower["id"]]["granted_units"] == 0
    assert by_work[lower["id"]]["status"] == "throttled"


def test_tracked_agent_work_consumes_and_releases_job_quota(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        service.upsert_quota(
            connection,
            project_id=settings.default_project_id,
            scope_type="project",
            scope_id=settings.default_project_id,
            resource_kind="job_concurrency",
            reserved_min=0,
            soft_limit=1,
            hard_limit=1,
            actor="chief_operator",
        )
        tracked = service.ensure_tracked_work(
            connection,
            project_id=settings.default_project_id,
            subject_type="agent_run",
            subject_id="quota-proof",
            title="Prove tracked quota execution",
            workload_class="agent:qa",
            actor="product_manager",
        )
        first = jobs.create_job(
            connection,
            project_id=settings.default_project_id,
            kind="test.unit",
            payload={"proof": "first"},
            work_item_id=tracked["work_item"]["id"],
            tracker_binding_id=tracked["tracker_binding"]["id"],
            allocation_id=tracked["allocation"]["id"],
            priority_score=tracked["work_item"]["effective_score"],
            workload_class="agent:qa",
        )
        second = jobs.create_job(
            connection,
            project_id=settings.default_project_id,
            kind="test.unit",
            payload={"proof": "second"},
            work_item_id=tracked["work_item"]["id"],
            tracker_binding_id=tracked["tracker_binding"]["id"],
            allocation_id=tracked["allocation"]["id"],
            priority_score=tracked["work_item"]["effective_score"],
            workload_class="agent:qa",
        )
        leased = jobs.claim_job(
            connection, "worker-1", ["test.unit"], lease_ttl_seconds=120
        )
        assert leased and leased["id"] == first["id"]
        assert leased["work_item_id"] == tracked["work_item"]["id"]

        blocked = jobs.claim_job(
            connection, "worker-2", ["test.unit"], lease_ttl_seconds=120
        )
        assert blocked is None
        deferred = connection.execute(
            text("SELECT status, last_error FROM jobs WHERE id=:id"),
            {"id": second["id"]},
        ).mappings().one()
        assert deferred["status"] == "queued"
        assert deferred["last_error"]["code"] == "resource_quota_exhausted"

        jobs.complete(
            connection,
            "worker-1",
            leased["id"],
            leased["lease_token"],
            {"ok": True},
        )
        connection.execute(
            text(
                "UPDATE jobs SET run_after=now() - interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": second["id"]},
        )
        next_lease = jobs.claim_job(
            connection, "worker-2", ["test.unit"], lease_ttl_seconds=120
        )
        assert next_lease and next_lease["id"] == second["id"], {
            "allocation": dict(
                connection.execute(
                    text(
                        "SELECT status, granted_units, used_units "
                        "FROM resource_allocations WHERE id=:id"
                    ),
                    {"id": tracked["allocation"]["id"]},
                ).mappings().one()
            ),
            "job": dict(
                connection.execute(
                    text(
                        "SELECT status, run_after, last_error "
                        "FROM jobs WHERE id=:id"
                    ),
                    {"id": second["id"]},
                ).mappings().one()
            ),
        }


def test_global_quota_caps_jobs_across_ranked_work_items(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        service.upsert_quota(
            connection,
            project_id=settings.default_project_id,
            scope_type="project",
            scope_id=settings.default_project_id,
            resource_kind="job_concurrency",
            reserved_min=0,
            soft_limit=3,
            hard_limit=3,
            actor="chief_operator",
        )
        global_quota = service.upsert_quota(
            connection,
            project_id=settings.default_project_id,
            scope_type="global",
            scope_id="global",
            resource_kind="job_concurrency",
            reserved_min=0,
            soft_limit=1,
            hard_limit=1,
            actor="chief_operator",
        )
        tracked_items = [
            service.ensure_tracked_work(
                connection,
                project_id=settings.default_project_id,
                subject_type="agent_run",
                subject_id=f"global-{index}",
                title=f"Globally bounded work {index}",
                actor="product_manager",
            )
            for index in range(2)
        ]
        queued = [
            jobs.create_job(
                connection,
                project_id=settings.default_project_id,
                kind="test.global",
                work_item_id=item["work_item"]["id"],
                tracker_binding_id=item["tracker_binding"]["id"],
                allocation_id=item["allocation"]["id"],
                priority_score=item["work_item"]["effective_score"],
            )
            for item in tracked_items
        ]
        first = jobs.claim_job(
            connection,
            "worker-global-1",
            ["test.global"],
            lease_ttl_seconds=120,
        )
        second = jobs.claim_job(
            connection,
            "worker-global-2",
            ["test.global"],
            lease_ttl_seconds=120,
        )
        visible_quota = next(
            item
            for item in service.list_quotas(
                connection, settings.default_project_id
            )
            if item["id"] == global_quota["id"]
        )

    assert first and first["id"] == queued[0]["id"]
    assert second is None
    assert visible_quota["used_units"] == 1


def test_plane_tracker_and_delivery_presence_plugins_feed_embedded_overview(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)

    with engine.begin() as connection:
        tracked = service.ensure_tracked_work(
            connection,
            project_id=settings.default_project_id,
            subject_type="prd",
            subject_id="agent-work-prd",
            title="Agent work orchestration PRD",
            workload_class="product",
            actor="product_manager",
        )
        sync_job = service.enqueue_tracker_sync(
            connection,
            project_id=settings.default_project_id,
            binding_id=tracked["tracker_binding"]["id"],
            actor="product_manager",
        )
        binding = service.bind_tracker(
            connection,
            project_id=settings.default_project_id,
            work_item_id=tracked["work_item"]["id"],
            provider_key="plane",
            external_project_id="plane-project",
            external_issue_id="plane-issue",
            external_key="BOSS-270",
            external_state="In Progress",
            actor="product_manager",
        )
        bot = service.upsert_bot_identity(
            connection,
            project_id=settings.default_project_id,
            provider="mattermost",
            agent_key="product_manager",
            display_name="BOSS Product Manager",
            external_identity="boss-product-manager",
            actor="chief_operator",
        )
        channel = service.upsert_status_channel(
            connection,
            project_id=settings.default_project_id,
            provider="mattermost",
            channel_key="boss-delivery",
            display_name="BOSS Delivery",
            purpose="Automatic ticket, PR, and deployment status.",
            bot_identity_id=bot["id"],
            event_kinds=["ticket.updated", "deployment.status"],
            actor="chief_operator",
        )
        service.record_delivery_event(
            connection,
            project_id=settings.default_project_id,
            provider="mattermost",
            event_kind="ticket.updated",
            subject_type="work_item",
            subject_id=tracked["work_item"]["id"],
            status="in_progress",
            summary="BOSS-270 is in progress.",
            dedupe_key="presence:BOSS-270:in-progress",
            channel_id=channel["id"],
            bot_identity_id=bot["id"],
            external_url=binding["external_url"],
        )
        deployment_presence = service.record_delivery_event(
            connection,
            project_id=settings.default_project_id,
            provider="github",
            event_kind="deployment.status",
            subject_type="work_item",
            subject_id=tracked["work_item"]["id"],
            status="healthy",
            summary="Production deployment is healthy.",
            dedupe_key="presence:deploy-270:healthy",
            external_url="https://github.example/deployments/270",
        )
        overview = service.overview(connection, settings.default_project_id)

    assert binding["sync_status"] == "synced"
    assert sync_job["status"] == "queued"
    assert sync_job["job_id"]
    assert binding["external_key"] == "BOSS-270"
    assert overview["tracker_providers"][0]["provider_key"] == "plane"
    assert overview["summary"]["active_channels"] == 1
    assert overview["presence_events"][0]["bot_name"] == "BOSS Product Manager"
    assert deployment_presence["projected_channel_ids"] == [channel["id"]]
    projected = next(
        item
        for item in overview["presence_events"]
        if item["dedupe_key"].endswith(f":channel:{channel['id']}")
    )
    assert projected["provider"] == "mattermost"
    assert projected["channel_name"] == "BOSS Delivery"
    assert projected["bot_name"] == "BOSS Product Manager"
    assert projected["metadata"]["source_provider"] == "github"
    assert projected["metadata"]["projection_only"] is True
    assert {item["provider"] for item in overview["delivery_plugins"]} == {
        "github",
        "mattermost",
        "plane",
    }


def test_plane_sync_executes_only_through_lease_and_persists_receipt(
    client, engine, monkeypatch
):
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    jobs = JobLeaseService(settings)
    monkeypatch.setattr(
        api_main.PlaneClient,
        "create_issue",
        lambda _self, project_id, **_payload: {
            "id": "plane-issue-31",
            "sequence_id": 31,
            "state": "Backlog",
        },
    )

    with engine.begin() as connection:
        tracked = service.ensure_tracked_work(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="plane-sync-proof",
            title="Create the external Plane issue through a leased job",
            description="The tracker write must be durable and auditable.",
            actor="product_manager",
        )
        binding = service.bind_tracker(
            connection,
            project_id=settings.default_project_id,
            work_item_id=tracked["work_item"]["id"],
            provider_key="plane",
            external_project_id="plane-project",
            actor="product_manager",
        )
        queued = service.enqueue_tracker_sync(
            connection,
            project_id=settings.default_project_id,
            binding_id=binding["id"],
            actor="product_manager",
        )
        leased = jobs.claim_job(
            connection,
            "worker-tracker",
            ["work_tracker.issue.sync"],
            lease_ttl_seconds=120,
        )

    assert leased and leased["id"] == queued["job_id"]
    response = client.post(
        "/v1/work-orchestration/tracker-sync/execute",
        json={
            "project_id": settings.default_project_id,
            "binding_id": binding["id"],
            "worker_id": "worker-tracker",
            "lease_token": leased["lease_token"],
            "created_by_job_id": leased["id"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["binding"]["external_issue_id"] == "plane-issue-31"
    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                "SELECT status, effect_kind, provider_ref FROM receipts "
                "WHERE id=:id"
            ),
            {"id": payload["receipt_id"]},
        ).mappings().one()
    assert receipt["status"] == "succeeded"
    assert receipt["effect_kind"] == "plane.issue.ensure"
    assert receipt["provider_ref"] == "plane-issue-31"
