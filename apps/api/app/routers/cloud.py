"""Cloud client routes, separate from the self-hosted API composition.

The service boundary decides which actions are available in each edition.
Every write still passes the project actor and receipt path.
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException
from sqlalchemy.engine import Connection, Engine

from boss_engine.services.cloud_product import CloudLinkRequiredError
from boss_engine.services.commercial import (
    AccountConnectAuthSessionNotFoundError,
    AccountConnectAuthSessionStateError,
    AccountConnectConfigurationError,
    AccountConnectLinkNotFoundError,
    AccountConnectSyncError,
)

from ..db import begin


def build_cloud_router(
    engine: Engine,
    cloud_product,
    require_project_actor: Callable[[Connection, str, str, str], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/cloud/catalog")
    def get_cloud_catalog(project_id: str) -> dict:
        """Community-callable Cloud/Enterprise feature catalog and independence contract."""
        with begin(engine) as connection:
            return cloud_product.catalog(connection, project_id)


    @router.get("/v1/cloud/status")
    def get_cloud_status(project_id: str) -> dict:
        """Independence + Account Connect link state for this project."""
        with begin(engine) as connection:
            return cloud_product.status(connection, project_id)


    @router.get("/v1/cloud/comparison")
    def get_cloud_comparison(project_id: str) -> dict:
        """Community vs Cloud vs Enterprise packaging matrix (marketing parity)."""
        with begin(engine) as connection:
            return cloud_product.comparison(connection, project_id)


    @router.get("/v1/cloud/capabilities")
    def get_cloud_capabilities(project_id: str) -> dict:
        """Local vs Cloud-locked capabilities for this project."""
        with begin(engine) as connection:
            return cloud_product.capabilities(connection, project_id)


    @router.get("/v1/cloud/workers")
    def get_cloud_managed_workers(project_id: str) -> dict:
        """Managed worker fabric status (honest planned until fleets ship).

        Includes local hybrid enrollments when present. Never claims
        managed_workers.available_now=true.
        """
        with begin(engine) as connection:
            return cloud_product.managed_workers_status(connection, project_id)


    @router.post("/v1/cloud/workers/enroll", status_code=201)
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
                    capabilities=list(capabilities)
                    if isinstance(capabilities, list)
                    else None,
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


    @router.post("/v1/cloud/sync")
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


    @router.get("/v1/cloud/entitlements")
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


    @router.post("/v1/cloud/team-invites", status_code=201)
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


    @router.get("/v1/cloud/team-invites")
    def cloud_list_team_invites(project_id: str, limit: int = 50) -> dict:
        """List local team invite records when Cloud-linked."""
        with begin(engine) as connection:
            try:
                return cloud_product.list_team_invites(
                    connection, project_id, limit=limit
                )
            except CloudLinkRequiredError as error:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": str(error),
                        "blocked_reason": error.blocked_reason,
                    },
                ) from error


    @router.get("/v1/cloud/marketplace")
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


    @router.post("/v1/cloud/easy-connect", status_code=201)
    def start_cloud_easy_connect(payload: dict) -> dict:
        """Start Easy Connect from self-hosted Community (or any edition) to BOSS Cloud.

        Community remains fully independent when Cloud is unconfigured. Browser or device
        methods never accept raw Cloud passwords in this control plane — hosted Account
        Connect completes auth and returns an org link.
        """
        project_id = str(payload.get("project_id") or "").strip()
        method = str(payload.get("method") or "browser").strip().lower()
        actor = str(payload.get("actor") or "ceo").strip()
        return_url = payload.get("return_url")
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.easy_connect(
                    connection,
                    project_id=project_id,
                    method=method,
                    actor=actor,
                    return_url=return_url if isinstance(return_url, str) else None,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            except AccountConnectAuthSessionStateError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error


    @router.get("/v1/cloud/easy-connect/{session_id}")
    def poll_cloud_easy_connect(
        session_id: str, project_id: str, actor: str = "ceo"
    ) -> dict:
        """Poll Easy Connect session (device flow) until completed or expired."""
        with begin(engine) as connection:
            try:
                return cloud_product.poll_easy_connect(
                    connection,
                    project_id=project_id,
                    session_id=session_id,
                    actor=actor,
                )
            except AccountConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                AccountConnectAuthSessionStateError,
                AccountConnectLinkNotFoundError,
                AccountConnectSyncError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error


    @router.post("/v1/cloud/easy-connect/{session_id}/complete")
    def complete_cloud_easy_connect(session_id: str, payload: dict) -> dict:
        """Complete browser Easy Connect after hosted Account Connect returns code/state."""
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        code = payload.get("code")
        state = payload.get("state")
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.complete_easy_connect(
                    connection,
                    project_id=project_id,
                    session_id=session_id,
                    actor=actor,
                    code=str(code) if code is not None else None,
                    state=str(state) if state is not None else None,
                )
            except AccountConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                AccountConnectAuthSessionStateError,
                AccountConnectSyncError,
                AccountConnectConfigurationError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error


    @router.post("/v1/cloud/easy-connect/complete")
    def complete_cloud_easy_connect_by_state(payload: dict) -> dict:
        """Complete browser Easy Connect using code+state from the hosted redirect callback."""
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        code = payload.get("code")
        state = payload.get("state")
        session_id = payload.get("session_id")
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.complete_easy_connect(
                    connection,
                    project_id=project_id,
                    session_id=str(session_id) if session_id else None,
                    actor=actor,
                    code=str(code) if code is not None else None,
                    state=str(state) if state is not None else None,
                )
            except AccountConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                AccountConnectAuthSessionStateError,
                AccountConnectSyncError,
                AccountConnectConfigurationError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error


    @router.post("/v1/cloud/unlink")
    def unlink_cloud(payload: dict) -> dict:
        """Disconnect this project from BOSS Cloud; Community stays fully usable."""
        project_id = str(payload.get("project_id") or "").strip()
        actor = str(payload.get("actor") or "ceo").strip()
        reason = payload.get("reason")
        if not project_id:
            raise HTTPException(status_code=422, detail="project_id is required")
        with begin(engine) as connection:
            require_project_actor(connection, project_id, actor, "providerops_manage")
            try:
                return cloud_product.unlink(
                    connection,
                    project_id=project_id,
                    actor=actor,
                    reason=str(reason) if reason else None,
                )
            except AccountConnectLinkNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error


    @router.get("/v1/cloud/retention")
    def get_cloud_retention(project_id: str) -> dict:
        """Evidence retention policy for this project (local receipts + Cloud SLA when linked)."""
        with begin(engine) as connection:
            return cloud_product.get_retention_policy(connection, project_id)


    @router.put("/v1/cloud/retention")
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

    return router
