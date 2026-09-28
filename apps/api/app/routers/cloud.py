"""Community Cloud status and optional Easy Connect routes."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException
from sqlalchemy.engine import Connection, Engine

from boss_engine.services.community_cloud import CommunityCloudService
from boss_engine.services.community_edition import (
    CloudConnectAuthSessionNotFoundError,
    CloudConnectAuthSessionStateError,
    CloudConnectConfigurationError,
    CloudConnectLinkNotFoundError,
    CloudConnectSyncError,
)

from .. import request_bodies as open_bodies
from ..db import begin


def register_community_cloud_routes(
    app: FastAPI,
    engine: Engine,
    cloud_product: CommunityCloudService,
    require_project_actor: Callable[[Connection, str, str, str], None],
) -> None:
    """Register local reads and authorized opt-in Cloud link flows."""

    @app.get("/v1/cloud/catalog")
    def get_cloud_catalog(project_id: str) -> dict:
        """Community-callable Cloud/Enterprise feature catalog and independence contract."""
        with begin(engine) as connection:
            return cloud_product.catalog(connection, project_id)

    @app.get("/v1/cloud/status")
    def get_cloud_status(project_id: str) -> dict:
        """Independence + Account Connect link state for this project."""
        with begin(engine) as connection:
            return cloud_product.status(connection, project_id)

    @app.get("/v1/cloud/comparison")
    def get_cloud_comparison(project_id: str) -> dict:
        """Community vs Cloud vs Enterprise packaging matrix (marketing parity)."""
        with begin(engine) as connection:
            return cloud_product.comparison(connection, project_id)

    @app.get("/v1/cloud/capabilities")
    def get_cloud_capabilities(project_id: str) -> dict:
        """Local vs Cloud-locked capabilities for this project."""
        with begin(engine) as connection:
            return cloud_product.capabilities(connection, project_id)

    @app.get("/v1/cloud/workers")
    def get_cloud_managed_workers(project_id: str) -> dict:
        """Managed worker fabric status (honest planned until fleets ship).

        Includes local hybrid enrollments when present. Never claims
        managed_workers.available_now=true.
        """
        with begin(engine) as connection:
            return cloud_product.managed_workers_status(connection, project_id)

    @app.post("/v1/cloud/easy-connect", status_code=201)
    def start_cloud_easy_connect(payload: open_bodies.CloudEasyConnectBody) -> dict:
        """Start Easy Connect from self-hosted Community (or any edition) to BOSS Cloud.

        Community remains fully independent when Cloud is unconfigured. Browser or device
        methods never accept raw Cloud passwords in this control plane — hosted Account
        Connect completes auth and returns an org link.
        """
        payload = payload.payload()
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
            except CloudConnectAuthSessionStateError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/cloud/easy-connect/{session_id}")
    def poll_cloud_easy_connect(session_id: str, project_id: str, actor: str = "ceo") -> dict:
        """Poll Easy Connect session (device flow) until completed or expired."""
        with begin(engine) as connection:
            try:
                return cloud_product.poll_easy_connect(
                    connection,
                    project_id=project_id,
                    session_id=session_id,
                    actor=actor,
                )
            except CloudConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                CloudConnectAuthSessionStateError,
                CloudConnectLinkNotFoundError,
                CloudConnectSyncError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/cloud/easy-connect/{session_id}/complete")
    def complete_cloud_easy_connect(session_id: str, payload: open_bodies.CloudEasyCompleteBody) -> dict:
        """Complete browser Easy Connect after hosted Account Connect returns code/state."""
        payload = payload.payload()
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
            except CloudConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                CloudConnectAuthSessionStateError,
                CloudConnectSyncError,
                CloudConnectConfigurationError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/cloud/easy-connect/complete")
    def complete_cloud_easy_connect_by_state(payload: open_bodies.CloudEasyCompleteByStateBody) -> dict:
        """Complete browser Easy Connect using code+state from the hosted redirect callback."""
        payload = payload.payload()
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
            except CloudConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (
                CloudConnectAuthSessionStateError,
                CloudConnectSyncError,
                CloudConnectConfigurationError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/cloud/unlink")
    def unlink_cloud(payload: open_bodies.CloudUnlinkBody) -> dict:
        """Disconnect this project from BOSS Cloud; Community stays fully usable."""
        payload = payload.payload()
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
            except CloudConnectLinkNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/v1/cloud/retention")
    def get_cloud_retention(project_id: str) -> dict:
        """Evidence retention policy for this project (local receipts + Cloud SLA when linked)."""
        with begin(engine) as connection:
            return cloud_product.get_retention_policy(connection, project_id)
