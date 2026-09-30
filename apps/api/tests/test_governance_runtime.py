from __future__ import annotations

from sqlalchemy import text


PROJECT_ID = "boss-dogfood"


def _lease_job(
    client,
    kind: str,
    worker_id: str,
    dedupe: str | None = None,
    payload: dict | None = None,
) -> dict:
    created = client.post(
        "/v1/jobs",
        json={
            "project_id": PROJECT_ID,
            "kind": kind,
            "payload": payload or {"purpose": "governance runtime acceptance"},
            "workload_class": "owner_sandbox",
            "data_boundary": "internal",
            "dedupe_key": dedupe,
        },
    )
    assert created.status_code == 201, created.text
    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": worker_id,
            "capabilities": [kind],
            "lease_ttl_seconds": 600,
        },
    )
    assert leased.status_code == 200, leased.text
    return leased.json()["job"]


def _create_case(client, suffix: str, risk_tier: int = 2) -> dict:
    response = client.post(
        "/v1/assurance/cases",
        json={
            "project_id": PROJECT_ID,
            "subject_type": "service",
            "subject_ref": f"service:{suffix}",
            "title": f"Assure {suffix}",
            "risk_tier": risk_tier,
            "references": {},
            "dedupe_key": f"case-{suffix}",
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_claim(client, case_id: str, suffix: str, claim_type: str = "fixed") -> dict:
    response = client.post(
        "/v1/assurance/claims",
        json={
            "project_id": PROJECT_ID,
            "assurance_case_id": case_id,
            "claim_type": claim_type,
            "statement": f"{suffix} is {claim_type}",
            "evidence_contract": {},
            "producer_ref": "software_engineer",
            "dedupe_key": f"claim-{suffix}",
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_evidence(
    client,
    case_id: str,
    claim_id: str,
    evidence_type: str,
    suffix: str,
    payload: dict | None = None,
) -> dict:
    response = client.post(
        "/v1/assurance/evidence",
        json={
            "project_id": PROJECT_ID,
            "assurance_case_id": case_id,
            "claim_id": claim_id,
            "evidence_type": evidence_type,
            "uri": f"artifact://{suffix}/{evidence_type}",
            "payload": payload or {"contract_ref": f"contract-{suffix}"},
            "provenance": {"producer": "pytest", "source": "test-run"},
            "reproduction": {"command": f"pytest {suffix}"},
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_ground_truth_metric_detects_breach_recovers_and_rejects_stale_lease(
    client, engine
):
    created = client.post(
        "/v1/assurance/metrics",
        json={
            "project_id": PROJECT_ID,
            "metric_key": "catalog.coverage",
            "metric_kind": "coverage",
            "unit": "records",
            "comparator": "gte",
            "baseline": 275,
            "threshold": 250,
            "cadence_seconds": 3600,
            "alert_sla_seconds": 7200,
            "owner_ref": "data-collector",
            "evidence_query": {"kind": "sql", "query_ref": "coverage-view"},
            "metadata": {},
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    metric = created.json()
    job = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "metric-worker",
            "capabilities": ["ground_truth.evaluate"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    assert job["id"] == metric["evaluation_job_id"]

    breach = client.post(
        f"/v1/assurance/metrics/{metric['id']}/observations",
        json={
            "project_id": PROJECT_ID,
            "value": 100,
            "evidence": {"query_ref": "coverage-view", "row_count": 100},
            "evaluator_job_id": job["id"],
            "evaluator_worker_id": "metric-worker",
            "lease_token": job["lease_token"],
            "dedupe_key": "coverage-hour-1",
        },
    )
    assert breach.status_code == 201, breach.text
    assert breach.json()["observation"]["status"] == "breached"
    assert breach.json()["signal"]["status"] == "open"
    assert breach.json()["signal"]["decision_id"]

    replay = client.post(
        f"/v1/assurance/metrics/{metric['id']}/observations",
        json={
            "project_id": PROJECT_ID,
            "value": 100,
            "evidence": {"query_ref": "coverage-view", "row_count": 100},
            "evaluator_job_id": job["id"],
            "evaluator_worker_id": "metric-worker",
            "lease_token": job["lease_token"],
            "dedupe_key": "coverage-hour-1",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["duplicate"] is True
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT status FROM jobs WHERE id=:id"), {"id": job["id"]}
        ).scalar_one() == "succeeded"

    normalized = client.post(
        f"/v1/assurance/metrics/{metric['id']}/baseline",
        json={
            "project_id": PROJECT_ID,
            "baseline": 100,
            "threshold": 90,
            "reason": "hide the regression",
            "actor": "ceo",
        },
    )
    assert normalized.status_code == 409
    assert "cannot normalize" in normalized.json()["detail"]
    unauthorized = client.post(
        f"/v1/assurance/metrics/{metric['id']}/baseline",
        json={
            "project_id": PROJECT_ID,
            "baseline": 275,
            "threshold": 250,
            "reason": "unauthorized edit",
            "actor": "cto",
        },
    )
    assert unauthorized.status_code == 403

    scheduled_recovery = client.post(
        "/v1/assurance/metrics/schedule",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "force": True,
            "metric_definition_id": metric["id"],
        },
    )
    assert scheduled_recovery.status_code == 200, scheduled_recovery.text
    recovery_job = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "metric-worker",
            "capabilities": ["ground_truth.evaluate"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    recovery = client.post(
        f"/v1/assurance/metrics/{metric['id']}/observations",
        json={
            "project_id": PROJECT_ID,
            "value": 280,
            "evidence": {"query_ref": "coverage-view", "row_count": 280},
            "evaluator_job_id": recovery_job["id"],
            "evaluator_worker_id": "metric-worker",
            "lease_token": recovery_job["lease_token"],
            "dedupe_key": "coverage-hour-2",
        },
    )
    assert recovery.status_code == 201, recovery.text
    assert recovery.json()["signal"]["status"] == "recovered"

    stale_job = _lease_job(
        client,
        "ground_truth.evaluate",
        "metric-worker-stale",
        payload={
            "metric_definition_id": metric["id"],
            "metric_definition_version": metric["version"],
            "metric_key": metric["metric_key"],
            "evidence_query": metric["evidence_query"],
        },
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": stale_job["id"]},
        )
    stale = client.post(
        f"/v1/assurance/metrics/{metric['id']}/observations",
        json={
            "project_id": PROJECT_ID,
            "value": 281,
            "evidence": {"query_ref": "coverage-view", "row_count": 281},
            "evaluator_job_id": stale_job["id"],
            "evaluator_worker_id": "metric-worker-stale",
            "lease_token": stale_job["lease_token"],
            "dedupe_key": "coverage-hour-3",
        },
    )
    assert stale.status_code == 409
    assert "expired" in stale.json()["detail"]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET status='deadlettered' WHERE id=:id"),
            {"id": stale_job["id"]},
        )

    metrics = client.get(
        "/v1/assurance/metrics", params={"project_id": PROJECT_ID}
    )
    assert metrics.status_code == 200
    projection = metrics.json()["items"][0]
    assert projection["current_value"] == 280
    assert projection["last_good_value"] == 280
    assert projection["current_evidence"]["row_count"] == 280
    assert len(projection["trend"]) == 2

    slow_metric_response = client.post(
        "/v1/assurance/metrics",
        json={
            "project_id": PROJECT_ID,
            "metric_key": "catalog.slow_bleed",
            "metric_kind": "coverage",
            "unit": "records",
            "comparator": "gte",
            "baseline": 275,
            "threshold": 250,
            "cadence_seconds": 3600,
            "alert_sla_seconds": 7200,
            "owner_ref": "data-collector",
            "evidence_query": {"kind": "sql", "query_ref": "slow-bleed-view"},
            "actor": "ceo",
        },
    )
    assert slow_metric_response.status_code == 201
    slow_metric = slow_metric_response.json()
    slow_job_1 = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "slow-worker-1",
            "capabilities": ["ground_truth.evaluate"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    healthy = client.post(
        f"/v1/assurance/metrics/{slow_metric['id']}/observations",
        json={
            "project_id": PROJECT_ID,
            "value": 260,
            "evidence": {"query_ref": "slow-bleed-view", "row_count": 260},
            "evaluator_job_id": slow_job_1["id"],
            "evaluator_worker_id": "slow-worker-1",
            "lease_token": slow_job_1["lease_token"],
            "dedupe_key": "slow-bleed-1",
        },
    )
    assert healthy.json()["observation"]["status"] == "healthy"
    client.post(
        "/v1/assurance/metrics/schedule",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "force": True,
            "metric_definition_id": slow_metric["id"],
        },
    )
    slow_job_2 = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "slow-worker-2",
            "capabilities": ["ground_truth.evaluate"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    slow_breach = client.post(
        f"/v1/assurance/metrics/{slow_metric['id']}/observations",
        json={
            "project_id": PROJECT_ID,
            "value": 249,
            "evidence": {"query_ref": "slow-bleed-view", "row_count": 249},
            "evaluator_job_id": slow_job_2["id"],
            "evaluator_worker_id": "slow-worker-2",
            "lease_token": slow_job_2["lease_token"],
            "dedupe_key": "slow-bleed-2",
        },
    )
    assert slow_breach.json()["observation"]["status"] == "breached"
    assert slow_breach.json()["signal"]["decision_id"]

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ground_truth_metric_definitions SET cadence_seconds=1,"
                "alert_sla_seconds=2 WHERE id=:id"
            ),
            {"id": metric["id"]},
        )
        connection.execute(
            text(
                "UPDATE ground_truth_observations SET observed_at=now()-interval '10 seconds' "
                "WHERE metric_definition_id=:id"
            ),
            {"id": metric["id"]},
        )
    overdue = client.post(
        "/v1/assurance/metrics/schedule",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "metric_definition_id": metric["id"],
        },
    )
    assert overdue.status_code == 200, overdue.text
    assert any(
        item["metric_definition_id"] == metric["id"]
        for item in overdue.json()["overdue"]
    )


def test_closed_question_tripwire_blocks_actionable_prior_but_allows_negation(client):
    created = client.post(
        "/v1/assurance/conclusions",
        json={
            "project_id": PROJECT_ID,
            "conclusion_key": "collector-access-root-cause",
            "conclusion": "The failure is parser drift, not IP reputation.",
            "scope": {"publication_boundaries": ["plan", "final"]},
            "owner_ref": "data-platform",
            "tell_patterns": [
                "rotate IP",
                {"pattern": "residential\\s+(proxy|exit)", "kind": "regex"},
            ],
            "cause_menu": ["parser drift", "missing session state", "schema change"],
            "evidence_refs": ["artifact://collector/isolation-test"],
            "reopen_conditions": ["new isolation evidence"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text

    blocked = client.post(
        "/v1/assurance/publication-guard",
        json={
            "project_id": PROJECT_ID,
            "publication_boundary": "plan",
            "content": "We should rotate IP and let it cool before trying again.",
            "dedupe_key": "tripwire-actionable",
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["allowed"] is False
    assert blocked.json()["match"]["status"] == "blocked"
    assert blocked.json()["match"]["stop_response"]["settled_conclusion"].startswith(
        "The failure is parser drift"
    )

    quoted = client.post(
        "/v1/assurance/publication-guard",
        json={
            "project_id": PROJECT_ID,
            "publication_boundary": "final",
            "content": "Do not rotate IP; that was the old, disproven recommendation.",
            "dedupe_key": "tripwire-negated",
        },
    )
    assert quoted.status_code == 200
    assert quoted.json()["allowed"] is True
    assert quoted.json()["match"]["context_classification"] == "negated"

    out_of_scope = client.post(
        "/v1/assurance/publication-guard",
        json={
            "project_id": PROJECT_ID,
            "publication_boundary": "internal_note",
            "content": "rotate IP",
            "dedupe_key": "tripwire-other-boundary",
        },
    )
    assert out_of_scope.status_code == 200
    assert out_of_scope.json()["match"] is None


def test_claim_gate_requires_red_to_green_and_causal_isolation(client):
    case = _create_case(client, "claim-gates")
    fixed = _create_claim(client, case["id"], "claim-gates-fixed", "fixed")
    other_case = _create_case(client, "claim-gates-other")
    mismatched = client.post(
        "/v1/assurance/evidence",
        json={
            "project_id": PROJECT_ID,
            "assurance_case_id": other_case["id"],
            "claim_id": fixed["id"],
            "evidence_type": "red_pre_fix",
            "payload": {"contract_ref": "wrong-case"},
            "provenance": {"producer": "pytest"},
            "reproduction": {"command": "pytest wrong-case"},
            "actor": "ceo",
        },
    )
    assert mismatched.status_code == 422
    assert "must belong" in mismatched.json()["detail"]
    _add_evidence(client, case["id"], fixed["id"], "red_pre_fix", "claim-gates")
    missing_green = client.post(
        f"/v1/assurance/claims/{fixed['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "dedupe_key": "fixed-eval-missing-green",
            "actor": "ceo",
        },
    )
    assert missing_green.status_code == 200, missing_green.text
    assert missing_green.json()["claim"]["status"] == "blocked"
    assert missing_green.json()["decision_id"]

    _add_evidence(
        client,
        case["id"],
        fixed["id"],
        "green_post_change",
        "claim-gates",
    )
    passed = client.post(
        f"/v1/assurance/claims/{fixed['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "dedupe_key": "fixed-eval-complete",
            "actor": "ceo",
        },
    )
    assert passed.status_code == 200, passed.text
    assert passed.json()["claim"]["status"] == "authoritative"
    assert passed.json()["verification"]["outcome"] == "passed"

    causal = _create_claim(client, case["id"], "claim-gates-cause", "caused_by")
    _add_evidence(
        client,
        case["id"],
        causal["id"],
        "isolation_experiment",
        "causal",
        {"changed_variables": ["IP", "cookie"]},
    )
    inconclusive = client.post(
        f"/v1/assurance/claims/{causal['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "dedupe_key": "causal-not-isolated",
            "actor": "ceo",
        },
    )
    assert inconclusive.status_code == 200
    assert inconclusive.json()["claim"]["status"] == "inconclusive"

    evaluated_case = client.post(
        f"/v1/assurance/cases/{case['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "org_rules": {"require_evidence": True},
            "project_rules": {"require_evidence": False},
            "actor": "ceo",
        },
    )
    assert evaluated_case.status_code == 200
    assert evaluated_case.json()["case"]["status"] == "blocked"
    assert "cannot weaken" in evaluated_case.json()["policy_snapshot"]["conflicts"][0]


def test_surface_ownership_is_single_owner_and_version_fenced(client):
    acquired = client.post(
        "/v1/assurance/surfaces/acquire",
        json={
            "project_id": PROJECT_ID,
            "surface_key": "collector:dining",
            "environment": "production",
            "owner_ref": "ceo",
            "coordination_route": "a2a://ceo",
            "reason": "single deploying owner",
            "dedupe_key": "surface-acquire-a",
            "actor": "ceo",
        },
    )
    assert acquired.status_code == 200, acquired.text
    ownership = acquired.json()["ownership"]
    assert acquired.json()["allowed"] is True

    conflict = client.post(
        "/v1/assurance/surfaces/acquire",
        json={
            "project_id": PROJECT_ID,
            "surface_key": "collector:dining",
            "environment": "production",
            "owner_ref": "session-b",
            "coordination_route": "a2a://session-b",
            "reason": "competing deployer",
            "dedupe_key": "surface-acquire-b",
            "actor": "ceo",
        },
    )
    assert conflict.status_code == 200
    assert conflict.json()["allowed"] is False
    assert conflict.json()["ownership"]["owner_ref"] == "ceo"

    denied = client.post(
        "/v1/assurance/surfaces/preflight",
        json={
            "project_id": PROJECT_ID,
            "surface_key": "collector:dining",
            "environment": "production",
            "actor_ref": "session-b",
            "expected_version": 1,
            "effect_ref": "deploy:123",
            "dedupe_key": "preflight-b",
        },
    )
    assert denied.status_code == 200
    assert denied.json()["allowed"] is False

    allowed = client.post(
        "/v1/assurance/surfaces/preflight",
        json={
            "project_id": PROJECT_ID,
            "surface_key": "collector:dining",
            "environment": "production",
            "actor_ref": "ceo",
            "expected_version": 1,
            "effect_ref": "deploy:124",
            "dedupe_key": "preflight-a",
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["allowed"] is True

    stale = client.post(
        f"/v1/assurance/surfaces/{ownership['id']}/transfer",
        json={
            "project_id": PROJECT_ID,
            "expected_version": 9,
            "to_owner_ref": "session-b",
            "coordination_route": "a2a://session-b",
            "reason": "handoff",
            "dedupe_key": "transfer-stale",
            "actor": "ceo",
        },
    )
    assert stale.status_code in {403, 409}

    transferred = client.post(
        f"/v1/assurance/surfaces/{ownership['id']}/transfer",
        json={
            "project_id": PROJECT_ID,
            "expected_version": 1,
            "to_owner_ref": "session-b",
            "coordination_route": "a2a://session-b",
            "reason": "handoff",
            "dedupe_key": "transfer-valid",
            "actor": "ceo",
        },
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["ownership"]["owner_ref"] == "session-b"
    assert transferred.json()["ownership"]["version"] == 2


def _skill_contract(side_effect_class: str = "read") -> dict:
    return {
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "permissions": ["workflow_execute"],
        "risk_tier": 2,
        "cost": {"mode": "free"},
        "data_boundaries": ["internal"],
        "retry": {"max_attempts": 3},
        "artifact_requirements": [],
        "evidence_requirements": ["result"],
        "side_effect_class": side_effect_class,
        "receipt_required": side_effect_class != "read",
        "dedupe_required": side_effect_class != "read",
    }


def test_integration_registry_resolves_only_current_intersection(client, engine):
    created = client.post(
        "/v1/integration-registry/definitions",
        json={
            "project_id": PROJECT_ID,
            "integration_key": "example.monitoring",
            "name": "Example Monitoring",
            "category": "observability",
            "skill_contracts": {"monitoring.read": _skill_contract()},
            "executor_bindings": [{"kind": "internal", "executor": "monitor.read"}],
            "provenance": {"trusted": True, "source": "operator"},
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    definition = created.json()

    connection = client.post(
        "/v1/integration-registry/connections",
        json={
            "project_id": PROJECT_ID,
            "integration_definition_id": definition["id"],
            "connection_key": "monitoring-prod",
            "scope_type": "project",
            "scope_id": PROJECT_ID,
            "owner_ref": "platform",
            "secret_ref_ids": [],
            "granted_capabilities": ["monitoring.read"],
            "data_boundaries": ["internal"],
            "max_risk_tier": 2,
            "freshness_seconds": 3600,
            "provenance": {"installed_by": "ceo"},
            "actor": "ceo",
        },
    )
    assert connection.status_code == 201, connection.text
    connection_id = connection.json()["id"]
    activated = client.patch(
        f"/v1/integration-registry/connections/{connection_id}",
        json={
            "project_id": PROJECT_ID,
            "health_status": "healthy",
            "status": "active",
            "rate_limit_state": {"remaining": 99},
            "actor": "ceo",
        },
    )
    assert activated.status_code == 200, activated.text

    allowed = client.post(
        "/v1/integration-registry/resolve",
        json={
            "project_id": PROJECT_ID,
            "skill_id": "monitoring.read",
            "actor_ref": "ceo",
            "role_allowed": ["monitoring.*"],
            "group_allowed": ["monitoring.read"],
            "workflow_allowed": ["monitoring.read"],
            "project_allowed": ["monitoring.read"],
            "requested_data_boundary": "internal",
            "risk_tier": 1,
            "dedupe_key": "capability-allowed",
        },
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["decision"] == "allowed"
    assert allowed.json()["integration_connection_id"] == connection_id

    denied = client.post(
        "/v1/integration-registry/resolve",
        json={
            "project_id": PROJECT_ID,
            "skill_id": "monitoring.read",
            "actor_ref": "ceo",
            "role_allowed": ["monitoring.*"],
            "group_allowed": [],
            "workflow_allowed": ["monitoring.read"],
            "project_allowed": ["monitoring.read"],
            "requested_data_boundary": "internal",
            "risk_tier": 1,
            "dedupe_key": "capability-denied",
        },
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["decision"] == "denied"
    assert "group_allowed" in " ".join(denied.json()["denial_reasons"])

    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE integration_connections SET health_checked_at=now()-interval '2 hours',"
                "freshness_seconds=60 WHERE id=:id"
            ),
            {"id": connection_id},
        )
    stale = client.post(
        "/v1/integration-registry/resolve",
        json={
            "project_id": PROJECT_ID,
            "skill_id": "monitoring.read",
            "actor_ref": "ceo",
            "role_allowed": ["monitoring.read"],
            "group_allowed": ["monitoring.read"],
            "workflow_allowed": ["monitoring.read"],
            "project_allowed": ["monitoring.read"],
            "requested_data_boundary": "internal",
            "risk_tier": 1,
            "dedupe_key": "capability-stale",
        },
    )
    assert stale.status_code == 200
    assert stale.json()["decision"] == "denied"
    assert "stale" in " ".join(stale.json()["denial_reasons"])


def test_agent_group_persists_partial_failure_dissent_and_never_treats_quorum_as_proof(
    client,
    engine,
):
    created = client.post(
        "/v1/agent-groups",
        json={
            "project_id": PROJECT_ID,
            "group_key": "release-review",
            "name": "Release Review",
            "purpose": "Independently review release evidence",
            "owner_ref": "cto",
            "topology": "panel",
            "members": [
                {"member_key": "architecture", "role_key": "cto", "independent": True},
                {"member_key": "quality", "role_key": "qa", "independent": True},
            ],
            "collaboration_protocol": {"rounds": 1, "share_before_submit": False},
            "allowed_capabilities": [],
            "context_policy": {"shared": ["spec"], "private": ["analysis"]},
            "independence_policy": {"require_independent_first_pass": True},
            "bounds": {
                "max_rounds": 1,
                "timeout_seconds": 600,
                "budget_usd_micros": 1000,
            },
            "completion_rule": {"kind": "quorum", "min_count": 1},
            "escalation_target": "ceo",
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["version"]["id"]
    started = client.post(
        "/v1/agent-groups/runs",
        json={
            "project_id": PROJECT_ID,
            "agent_group_version_id": version_id,
            "input_payload": {"question": "ship?"},
            "context": {"data_boundary": "internal"},
            "capability_resolution_ids": [],
            "governance_gate_results": {"evidence_gate": True, "budget_gate": True},
            "dedupe_key": "release-review-run",
            "actor": "ceo",
        },
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["run"]["id"]

    first = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "group-worker-1",
            "capabilities": ["agent_group.member"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    first_member = first["payload"]["member_key"]
    completed = client.post(
        f"/v1/agent-groups/runs/{run_id}/members/{first_member}/complete",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "group-worker-1",
            "job_lease_token": first["lease_token"],
            "succeeded": True,
            "output": {"verdict": "approve"},
            "artifacts": ["artifact://review"],
            "evidence_refs": ["artifact://review/evidence"],
            "receipt_refs": [],
            "cost_usd_micros": 0,
        },
    )
    assert completed.status_code == 200, completed.text

    second = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "group-worker-2",
            "capabilities": ["agent_group.member"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    second_member = second["payload"]["member_key"]
    failed = client.post(
        f"/v1/agent-groups/runs/{run_id}/members/{second_member}/complete",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "group-worker-2",
            "job_lease_token": second["lease_token"],
            "succeeded": False,
            "output": {"dissent": "Could not verify the migration."},
            "artifacts": [],
            "evidence_refs": [],
            "receipt_refs": [],
            "cost_usd_micros": 0,
            "error": {"message": "verification unavailable"},
        },
    )
    assert failed.status_code == 200, failed.text
    with engine.begin() as db:
        job_states = dict(
            db.execute(
                text("SELECT id,status FROM jobs WHERE id = ANY(:ids)"),
                {"ids": [first["id"], second["id"]]},
            ).all()
        )
    assert job_states[first["id"]] == "succeeded"
    assert job_states[second["id"]] == "deadlettered"

    finalized = client.post(
        f"/v1/agent-groups/runs/{run_id}/finalize",
        json={
            "project_id": PROJECT_ID,
            "output": {"recommendation": "ship with recorded dissent"},
            "actor": "ceo",
        },
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["run"]["status"] == "completed"
    assert finalized.json()["run"]["dissent"]
    with engine.begin() as db:
        assert db.execute(
            text(
                "SELECT status FROM budget_reservations "
                "WHERE id=:id"
            ),
            {"id": started.json()["run"]["budget_reservation_id"]},
        ).scalar_one() == "released"

    no_proof = client.post(
        "/v1/agent-groups/runs",
        json={
            "project_id": PROJECT_ID,
            "agent_group_version_id": version_id,
            "input_payload": {"question": "claim fixed?"},
            "context": {"data_boundary": "internal"},
            "capability_resolution_ids": [],
            "governance_gate_results": {"evidence_gate": True},
            "dedupe_key": "release-review-no-proof",
            "actor": "ceo",
        },
    )
    assert no_proof.status_code == 201
    run2 = no_proof.json()["run"]["id"]
    jobs = []
    for index in range(2):
        job = client.post(
            "/v1/worker/poll",
            json={
                "worker_id": f"no-proof-{index}",
                "capabilities": ["agent_group.member"],
                "lease_ttl_seconds": 600,
            },
        ).json()["job"]
        jobs.append(job)
        response = client.post(
            f"/v1/agent-groups/runs/{run2}/members/{job['payload']['member_key']}/complete",
            json={
                "project_id": PROJECT_ID,
                "worker_id": f"no-proof-{index}",
                "job_lease_token": job["lease_token"],
                "succeeded": True,
                "output": {"verdict": "approve"},
                "evidence_refs": [],
                "receipt_refs": [],
                "cost_usd_micros": 0,
            },
        )
        assert response.status_code == 200, response.text
    blocked = client.post(
        f"/v1/agent-groups/runs/{run2}/finalize",
        json={"project_id": PROJECT_ID, "output": {}, "actor": "ceo"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["run"]["status"] == "blocked"
    assert "not proof" in blocked.json()["run"]["output"]["disposition_reason"]

    with engine.begin() as db:
        db.execute(
            text(
                """
                INSERT INTO capability_resolutions (
                  id,project_id,skill_id,actor_ref,requested_data_boundary,
                  risk_tier,authority_inputs,decision,denial_reasons,
                  resolved_contract,dedupe_key
                ) VALUES (
                  'capres_group_scope',:p,'monitoring.read','ceo','internal',
                  1,'{}'::jsonb,'allowed','[]'::jsonb,'{}'::jsonb,
                  'capres-group-scope'
                )
                """
            ),
            {"p": PROJECT_ID},
        )
    unauthorized = client.post(
        "/v1/agent-groups/runs",
        json={
            "project_id": PROJECT_ID,
            "agent_group_version_id": version_id,
            "input_payload": {"question": "read monitoring?"},
            "context": {"data_boundary": "internal"},
            "capability_resolution_ids": ["capres_group_scope"],
            "governance_gate_results": {},
            "dedupe_key": "release-review-unauthorized-capability",
            "actor": "ceo",
        },
    )
    assert unauthorized.status_code == 409
    assert "does not authorize" in unauthorized.json()["detail"]

    timed = client.post(
        "/v1/agent-groups/runs",
        json={
            "project_id": PROJECT_ID,
            "agent_group_version_id": version_id,
            "input_payload": {"question": "deadline?"},
            "context": {"data_boundary": "internal"},
            "capability_resolution_ids": [],
            "governance_gate_results": {},
            "dedupe_key": "release-review-timeout",
            "actor": "ceo",
        },
    )
    assert timed.status_code == 201, timed.text
    timed_run_id = timed.json()["run"]["id"]
    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE agent_group_runs SET deadline_at=now()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": timed_run_id},
        )
    timed_out = client.post(
        f"/v1/agent-groups/runs/{timed_run_id}/finalize",
        json={"project_id": PROJECT_ID, "output": {}, "actor": "ceo"},
    )
    assert timed_out.status_code == 200, timed_out.text
    assert timed_out.json()["run"]["status"] == "failed"
    assert {member["status"] for member in timed_out.json()["members"]} == {
        "timed_out"
    }

    expensive = client.post(
        "/v1/agent-groups",
        json={
            "project_id": PROJECT_ID,
            "group_key": "over-budget-review",
            "name": "Over Budget Review",
            "purpose": "Prove deterministic budget refusal",
            "owner_ref": "cto",
            "topology": "panel",
            "members": [
                {"member_key": "quality", "role_key": "qa", "independent": True}
            ],
            "collaboration_protocol": {"rounds": 1},
            "allowed_capabilities": [],
            "context_policy": {},
            "independence_policy": {},
            "bounds": {
                "max_rounds": 1,
                "timeout_seconds": 600,
                "budget_usd_micros": 999_999_999_999,
            },
            "completion_rule": {"kind": "all"},
            "escalation_target": "ceo",
            "actor": "ceo",
        },
    )
    assert expensive.status_code == 201, expensive.text
    denied = client.post(
        "/v1/agent-groups/runs",
        json={
            "project_id": PROJECT_ID,
            "agent_group_version_id": expensive.json()["version"]["id"],
            "input_payload": {},
            "context": {},
            "capability_resolution_ids": [],
            "governance_gate_results": {},
            "dedupe_key": "over-budget-run",
            "actor": "ceo",
        },
    )
    assert denied.status_code == 409
    assert "Budget cap exceeded" in denied.json()["detail"]


def test_standing_verifier_refutation_reopens_authoritative_status(client, engine):
    case = _create_case(client, "standing-verifier", risk_tier=3)
    claim = _create_claim(client, case["id"], "standing-verifier", "fixed")
    _add_evidence(client, case["id"], claim["id"], "red_pre_fix", "standing-verifier")
    _add_evidence(
        client, case["id"], claim["id"], "green_post_change", "standing-verifier"
    )
    evaluated = client.post(
        f"/v1/assurance/claims/{claim['id']}/evaluate",
        json={
            "project_id": PROJECT_ID,
            "dedupe_key": "standing-verifier-claim-eval",
            "actor": "ceo",
        },
    )
    assert evaluated.json()["claim"]["status"] == "authoritative"

    policy = client.post(
        "/v1/assurance/verification-policies",
        json={
            "project_id": PROJECT_ID,
            "policy_key": "critical-fixes",
            "surface_patterns": ["service:*"],
            "claim_types": ["fixed"],
            "risk_tiers": [3],
            "cadence_seconds": 3600,
            "sample_rate": 1,
            "verification_method": {"kind": "reproduce"},
            "independence_policy": {"require_distinct_path": True},
            "escalation_target": "ceo",
            "estimated_cost_usd_micros": 1_000,
            "actor": "ceo",
        },
    )
    assert policy.status_code == 201, policy.text
    assert len(policy.json()["scheduled_runs"]) == 1
    run = policy.json()["scheduled_runs"][0]
    leased = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "verifier-worker",
            "capabilities": ["assurance.verify"],
            "lease_ttl_seconds": 600,
        },
    ).json()["job"]
    assert leased["id"] == run["job_id"]
    receipt_id = "receipt_standing_verifier"
    with engine.begin() as db:
        db.execute(
            text(
                """
                INSERT INTO receipts (
                  id,org_id,workspace_id,project_id,dedupe_key,effect_kind,
                  effect_scope,payload_hash,status,provider_ref,result
                )
                SELECT :id,w.org_id,p.workspace_id,p.id,:dedupe,'assurance.verify',
                  '{}'::jsonb,:hash,'succeeded','verifier-test','{}'::jsonb
                FROM projects p JOIN workspaces w ON w.id=p.workspace_id
                WHERE p.id=:project
                """
            ),
            {
                "id": receipt_id,
                "dedupe": "standing-verifier-receipt",
                "hash": "0" * 64,
                "project": PROJECT_ID,
            },
        )
    refuted = client.post(
        f"/v1/assurance/verification-runs/{run['id']}/complete",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "verifier-worker",
            "job_lease_token": leased["lease_token"],
            "outcome": "refuted",
            "evidence_bundle": [
                {
                    "kind": "reproduction",
                    "result": "failed again",
                    "artifact": "artifact://refutation",
                }
            ],
            "receipt_refs": [receipt_id],
            "result": {
                "summary": "The claimed fix does not reproduce.",
                "actual_cost_usd_micros": 900,
            },
        },
    )
    assert refuted.status_code == 200, refuted.text
    assert refuted.json()["status"] == "refuted"
    assert refuted.json()["decision_id"]
    with engine.begin() as db:
        assert db.execute(
            text("SELECT status FROM jobs WHERE id=:id"),
            {"id": leased["id"]},
        ).scalar_one() == "succeeded"
        budget_evidence = db.execute(
            text(
                """
                SELECT br.status,count(cr.id) AS cost_receipts
                FROM budget_reservations br
                LEFT JOIN cost_receipts cr ON cr.reservation_id=br.id
                WHERE br.id=:id GROUP BY br.status
                """
            ),
            {"id": run["budget_reservation_id"]},
        ).mappings().one()
        assert budget_evidence["status"] == "consumed"
        assert int(budget_evidence["cost_receipts"]) == 1
    overview = client.get("/v1/assurance/overview", params={"project_id": PROJECT_ID})
    assert overview.status_code == 200
    assert overview.json()["cases"][0]["status"] == "refuted"
