"""Paid Cloud operations, registered only by the paid API composition root."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException
from sqlalchemy.engine import Connection, Engine

from boss_engine.services.cloud_product import CloudLinkRequiredError, CloudProductService
from boss_engine.services.commercial import (
    AccountConnectConfigurationError,
    AccountConnectLinkNotFoundError,
    AccountConnectSyncError,
)

from ..db import begin


def register_paid_cloud_routes(
    app: FastAPI,
    engine: Engine,
    cloud_product: CloudProductService,
    require_project_actor: Callable[[Connection, str, str, str], None],
) -> None:
    """Register hosted operations after paid services are selected."""

    @app.post("/v1/cloud/workers/enroll", status_code=201)
    def enroll_cloud_worker(payload: dict) -> dict:
        """Enroll a local worker node for hybrid attach after Cloud link (receipted).

        Does not provision elastic managed fleets. Community remains independent when
        unlinked (returns 404/409 with blocked_reason).
        """
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        worker_node_id = str(payload.get("worker_node_id") or "").strip()
        display_name = payload.get("display_name")
        capabilities = payload.get("capabilities")
        metadata = payload.get("metadata")
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        if not worker_node_id:
            raise HTTPException(status_code=422, detail="worker_node_id is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.enroll_worker(
                    connection,
                    project_id,
                    worker_node_id=worker_node_id,
                    actor=actor,
                    display_name=str(display_name) if display_name else None,
                    capabilities=list(capabilities) if isinstance(capabilities, list) else None,
                    metadata=metadata if isinstance(metadata, dict) else None,
                )
            except CloudLinkRequiredError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": error.blocked_reason,
                    },
                ) from error
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/cloud/sync")
    def cloud_sync_entitlements(payload: dict) -> dict:
        """Sync hosted Account Connect entitlements after Easy Connect link."""
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        hosted_org_id = payload.get("hosted_org_id")
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.sync_entitlements(
                    connection,
                    project_id,
                    actor=actor,
                    hosted_org_id=str(hosted_org_id) if hosted_org_id else None,
                )
            except CloudLinkRequiredError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": error.blocked_reason,
                    },
                ) from error
            except AccountConnectLinkNotFoundError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": "cloud_not_linked",
                    },
                ) from error
            except AccountConnectConfigurationError as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "blocked_reason": "account_connect_url_missing",
                    },
                ) from error
            except AccountConnectSyncError as error:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": str(error),
                        "blocked_reason": "account_connect_sync_failed",
                    },
                ) from error

    @app.get("/v1/cloud/entitlements")
    def cloud_list_entitlements(
        project_id: str,
        include_inactive: bool = False,
    ) -> dict:
        """List effective entitlements (local edition + hosted when linked)."""
        with begin(engine) as connection:
            return cloud_product.list_entitlements(
                connection,
                project_id,
                include_inactive=include_inactive,
            )

    @app.post("/v1/cloud/team-invites", status_code=201)
    def cloud_create_team_invite(payload: dict) -> dict:
        """Create a hosted team invite via Account Connect when Cloud-linked."""
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        email = str(payload.get("email") or "").strip()
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        if not email:
            raise HTTPException(status_code=422, detail="email is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.create_team_invite(
                    connection,
                    project_id,
                    email=email,
                    actor=actor,
                    display_name=(
                        str(payload["display_name"])
                        if payload.get("display_name") is not None
                        else None
                    ),
                    role_keys=list(payload["role_keys"])
                    if isinstance(payload.get("role_keys"), list)
                    else None,
                    permissions=list(payload["permissions"])
                    if isinstance(payload.get("permissions"), list)
                    else None,
                    metadata=payload.get("metadata")
                    if isinstance(payload.get("metadata"), dict)
                    else None,
                )
            except CloudLinkRequiredError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": error.blocked_reason,
                    },
                ) from error
            except AccountConnectLinkNotFoundError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": "cloud_not_linked",
                    },
                ) from error

    @app.get("/v1/cloud/team-invites")
    def cloud_list_team_invites(project_id: str, limit: int = 50) -> dict:
        """List local team invite records when Cloud-linked."""
        with begin(engine) as connection:
            try:
                return cloud_product.list_team_invites(connection, project_id, limit=limit)
            except CloudLinkRequiredError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": error.blocked_reason,
                    },
                ) from error

    @app.get("/v1/cloud/marketplace")
    def cloud_list_marketplace(
        project_id: str,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> dict:
        """List hosted marketplace packs when linked; empty + blocked_reason when not."""
        with begin(engine) as connection:
            return cloud_product.list_marketplace(
                connection,
                project_id,
                include_inactive=include_inactive,
                limit=limit,
            )

    @app.put("/v1/cloud/retention")
    def put_cloud_retention(payload: dict) -> dict:
        """Set Cloud-linked evidence retention_days (30–2555). Receipted; requires active link."""
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        if "retention_days" not in payload:
            raise HTTPException(status_code=422, detail="retention_days is required")
        reason = payload.get("reason")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.update_retention_policy(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    retention_days=payload["retention_days"],
                    reason=str(reason) if reason is not None else None,
                )
            except AccountConnectLinkNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
