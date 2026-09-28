"""Paid Enterprise cluster validation and support API routes.

Community self-hosted identity/SSO remains registered in the shared API; this
module is imported only by the paid composition root.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from sqlalchemy.engine import Connection, Engine

from boss_engine.services.enterprise import EnterpriseService

from ..db import begin
from .paid_schemas import (
    EnterpriseClusterListResponse,
    EnterpriseClusterSummary,
    EnterpriseClusterUpsertRequest,
    EnterpriseClusterValidationRunCreateRequest,
    EnterpriseClusterValidationRunListResponse,
    EnterpriseClusterValidationRunSummary,
    EnterpriseSupportCaseCreateRequest,
    EnterpriseSupportCaseListResponse,
    EnterpriseSupportCaseSummary,
    EnterpriseSupportCaseUpdateRequest,
)


def register_enterprise_operations_routes(
    app: FastAPI,
    engine: Engine,
    enterprise: EnterpriseService,
    require_project_actor: Callable[[Connection, str, str, str], None],
) -> None:
    """Register paid cluster and support routes before edition partitioning."""

    def enterprise_cluster_summary_from_service(payload) -> EnterpriseClusterSummary:
        return EnterpriseClusterSummary(**payload)

    def enterprise_cluster_validation_run_summary_from_service(
        payload,
    ) -> EnterpriseClusterValidationRunSummary:
        return EnterpriseClusterValidationRunSummary(**payload)

    def enterprise_support_case_summary_from_service(
        payload,
    ) -> EnterpriseSupportCaseSummary:
        return EnterpriseSupportCaseSummary(**payload)

    @app.get("/v1/enterprise/clusters", response_model=EnterpriseClusterListResponse)
    def list_enterprise_clusters(project_id: str) -> EnterpriseClusterListResponse:
        with begin(engine) as connection:
            items = enterprise.list_clusters(connection, project_id)
            return EnterpriseClusterListResponse(
                project_id=project_id,
                items=[enterprise_cluster_summary_from_service(item) for item in items],
            )

    @app.post(
        "/v1/enterprise/clusters",
        response_model=EnterpriseClusterSummary,
        status_code=201,
    )
    def upsert_enterprise_cluster(
        payload: EnterpriseClusterUpsertRequest,
    ) -> EnterpriseClusterSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "enterprise_manage",
            )
            try:
                cluster = enterprise.upsert_cluster(
                    connection,
                    payload.project_id,
                    deployment_mode=payload.deployment_mode,
                    account_connect_mode=payload.account_connect_mode,
                    actor=payload.actor,
                    worker_pool_strategy=payload.worker_pool_strategy,
                    ha_profile=payload.ha_profile,
                    data_residency=payload.data_residency,
                    identity_controls=payload.identity_controls,
                    governance_controls=payload.governance_controls,
                    audit_controls=payload.audit_controls,
                    private_worker_pools=payload.private_worker_pools,
                    compliance_packs=payload.compliance_packs,
                    support_tier=payload.support_tier,
                    support_sla=payload.support_sla,
                    status=payload.status,
                    metadata=payload.metadata,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            return enterprise_cluster_summary_from_service(cluster)

    @app.get(
        "/v1/enterprise/cluster-validation-runs",
        response_model=EnterpriseClusterValidationRunListResponse,
    )
    def list_enterprise_cluster_validation_runs(
        project_id: str,
        enterprise_cluster_id: Optional[str] = None,
        limit: int = 50,
    ) -> EnterpriseClusterValidationRunListResponse:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        with begin(engine) as connection:
            items = enterprise.list_cluster_validation_runs(
                connection,
                project_id,
                enterprise_cluster_id=enterprise_cluster_id,
                limit=limit,
            )
            return EnterpriseClusterValidationRunListResponse(
                project_id=project_id,
                items=[
                    enterprise_cluster_validation_run_summary_from_service(item)
                    for item in items
                ],
            )

    @app.post(
        "/v1/enterprise/cluster-validation-runs",
        response_model=EnterpriseClusterValidationRunSummary,
        status_code=201,
    )
    def create_enterprise_cluster_validation_run(
        payload: EnterpriseClusterValidationRunCreateRequest,
    ) -> EnterpriseClusterValidationRunSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "enterprise_manage",
            )
            try:
                run = enterprise.record_cluster_validation_run(
                    connection,
                    payload.project_id,
                    enterprise_cluster_id=payload.enterprise_cluster_id,
                    validation_kind=payload.validation_kind,
                    status=payload.status,
                    checks=payload.checks,
                    evidence_refs=payload.evidence_refs,
                    actor=payload.actor,
                    metadata=payload.metadata,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            return enterprise_cluster_validation_run_summary_from_service(run)

    @app.get(
        "/v1/enterprise/support-cases",
        response_model=EnterpriseSupportCaseListResponse,
    )
    def list_enterprise_support_cases(
        project_id: str,
        limit: int = 50,
    ) -> EnterpriseSupportCaseListResponse:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
        with begin(engine) as connection:
            items = enterprise.list_support_cases(connection, project_id, limit)
            return EnterpriseSupportCaseListResponse(
                project_id=project_id,
                items=[
                    enterprise_support_case_summary_from_service(item) for item in items
                ],
            )

    @app.post(
        "/v1/enterprise/support-cases",
        response_model=EnterpriseSupportCaseSummary,
        status_code=201,
    )
    def create_enterprise_support_case(
        payload: EnterpriseSupportCaseCreateRequest,
    ) -> EnterpriseSupportCaseSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "enterprise_manage",
            )
            try:
                item = enterprise.create_support_case(
                    connection,
                    payload.project_id,
                    title=payload.title,
                    description=payload.description,
                    actor=payload.actor,
                    severity=payload.severity,
                    category=payload.category,
                    enterprise_cluster_id=payload.enterprise_cluster_id,
                    escalate_to_hosted=payload.escalate_to_hosted,
                    metadata=payload.metadata,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            return enterprise_support_case_summary_from_service(item)

    @app.post(
        "/v1/enterprise/support-cases/{case_id}",
        response_model=EnterpriseSupportCaseSummary,
    )
    def update_enterprise_support_case(
        case_id: str,
        payload: EnterpriseSupportCaseUpdateRequest,
    ) -> EnterpriseSupportCaseSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "enterprise_manage",
            )
            try:
                item = enterprise.update_support_case(
                    connection,
                    payload.project_id,
                    case_id,
                    actor=payload.actor,
                    status=payload.status,
                    assigned_to=payload.assigned_to,
                    metadata=payload.metadata,
                )
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            return enterprise_support_case_summary_from_service(item)
