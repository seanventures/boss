"""Paid hosted Account Connect and marketplace API routes.

Loaded only by the paid API composition root. Community may still opt in to
Cloud through its separate /v1/cloud/* Easy Connect client endpoints.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from boss_engine.services.commercial import (
    AccountConnectAuthSessionNotFoundError,
    AccountConnectAuthSessionStateError,
    AccountConnectConfigurationError,
    AccountConnectLinkNotFoundError,
    AccountConnectSyncError,
    CommercialService,
)
from boss_engine.services.core import emit_event

from ..db import begin
from ..schemas import (
    AccountConnectAuthSessionCompleteRequest, AccountConnectAuthSessionCreateRequest,
    AccountConnectAuthSessionListResponse, AccountConnectAuthSessionSummary,
    AccountConnectBillingPortalSessionCreateRequest, AccountConnectBillingPortalSessionListResponse,
    AccountConnectBillingPortalSessionSummary, AccountConnectHealthCheckListResponse,
    AccountConnectHealthCheckRunRequest, AccountConnectHealthCheckSummary,
    AccountConnectLinkRequest, AccountConnectMutationResponse, AccountConnectSyncRequest,
    AccountConnectSyncResponse, AccountConnectTeamInviteCreateRequest,
    AccountConnectTeamInviteListResponse, AccountConnectTeamInviteRevokeRequest,
    AccountConnectTeamInviteSummary, AccountConnectUnlinkRequest, EditionStatusResponse,
    HostedMarketplaceListingListResponse, HostedMarketplaceListingSummary,
    HostedMarketplaceSyncRequest, HostedMarketplaceSyncResponse,
    HostedMarketplaceSyncRunListResponse, HostedMarketplaceSyncRunSummary
)

LOGGER = logging.getLogger("boss.api")


def register_hosted_routes(
    app: FastAPI,
    engine: Engine,
    commercial: CommercialService,
    require_project_actor: Callable[[Connection, str, str, str], None],
    edition_status_from_service: Callable[[Any], EditionStatusResponse],
) -> None:
    """Register paid hosted routes before edition route partitioning."""

    def record_account_connect_sync_denial(project_id: str, actor: str, reason: str) -> None:
        try:
            with begin(engine) as audit_connection:
                project_exists = audit_connection.execute(
                    text("SELECT 1 FROM projects WHERE id = :project_id"),
                    {"project_id": project_id},
                ).first()
                if not project_exists:
                    return
                emit_event(
                    audit_connection,
                    project_id,
                    "account_connect.sync_denied",
                    actor,
                    "account_connect_org",
                    project_id,
                    {"reason": reason},
                )
        except Exception as audit_error:
            LOGGER.exception(
                "account_connect_sync_denial_audit_write_failed project_id=%s actor=%s reason=%s",
                project_id,
                actor,
                reason,
                exc_info=audit_error,
            )
            return

    def record_account_connect_denial(
        project_id: str,
        actor: str,
        event_type: str,
        reason: str,
        entity_type: str,
    ) -> None:
        try:
            with begin(engine) as audit_connection:
                project_exists = audit_connection.execute(
                    text("SELECT 1 FROM projects WHERE id = :project_id"),
                    {"project_id": project_id},
                ).first()
                if not project_exists:
                    return
                emit_event(
                    audit_connection,
                    project_id,
                    event_type,
                    actor,
                    entity_type,
                    project_id,
                    {"reason": reason},
                )
        except Exception as audit_error:
            LOGGER.exception(
                "account_connect_denial_audit_write_failed project_id=%s actor=%s event_type=%s reason=%s",
                project_id,
                actor,
                event_type,
                reason,
                exc_info=audit_error,
            )
            return

    def account_connect_auth_session_from_service(
        payload,
    ) -> AccountConnectAuthSessionSummary:
        return AccountConnectAuthSessionSummary(**payload)

    def account_connect_health_check_from_service(
        payload,
    ) -> AccountConnectHealthCheckSummary:
        return AccountConnectHealthCheckSummary(**payload)

    def account_connect_team_invite_from_service(
        payload,
    ) -> AccountConnectTeamInviteSummary:
        return AccountConnectTeamInviteSummary(**payload)

    def account_connect_billing_session_from_service(
        payload,
    ) -> AccountConnectBillingPortalSessionSummary:
        return AccountConnectBillingPortalSessionSummary(**payload)

    def hosted_marketplace_listing_from_service(
        payload,
    ) -> HostedMarketplaceListingSummary:
        return HostedMarketplaceListingSummary(**payload)

    def hosted_marketplace_sync_run_from_service(
        payload,
    ) -> HostedMarketplaceSyncRunSummary:
        return HostedMarketplaceSyncRunSummary(**payload)

    @app.post("/v1/account-connect/link", response_model=AccountConnectMutationResponse)
    def link_account_connect(
        payload: AccountConnectLinkRequest,
    ) -> AccountConnectMutationResponse:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            edition = commercial.link_account_connect_org(
                connection,
                payload.project_id,
                hosted_org_id=payload.hosted_org_id,
                actor=payload.actor,
                connection_ref=payload.connection_ref,
                marketplace_identity=payload.marketplace_identity,
                billing_portal_ref=payload.billing_portal_ref,
                connector_authorization_refs=payload.connector_authorization_refs,
                optional_telemetry_enabled=payload.optional_telemetry_enabled,
                metadata=payload.metadata,
            )
            return AccountConnectMutationResponse(
                project_id=payload.project_id,
                edition=edition_status_from_service(edition),
            )

    @app.post("/v1/account-connect/unlink", response_model=AccountConnectMutationResponse)
    def unlink_account_connect(
        payload: AccountConnectUnlinkRequest,
    ) -> AccountConnectMutationResponse:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                edition = commercial.unlink_account_connect_org(
                    connection,
                    payload.project_id,
                    actor=payload.actor,
                    hosted_org_id=payload.hosted_org_id,
                    reason=payload.reason,
                )
            except AccountConnectLinkNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            return AccountConnectMutationResponse(
                project_id=payload.project_id,
                edition=edition_status_from_service(edition),
            )

    @app.post("/v1/account-connect/sync", response_model=AccountConnectSyncResponse)
    def sync_account_connect(
        payload: AccountConnectSyncRequest,
    ) -> AccountConnectSyncResponse:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                result = commercial.sync_account_connect_entitlements(
                    connection,
                    payload.project_id,
                    actor=payload.actor,
                    hosted_org_id=payload.hosted_org_id,
                )
            except AccountConnectLinkNotFoundError as error:
                record_account_connect_sync_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect_not_linked",
                )
                raise HTTPException(status_code=404, detail=str(error)) from error
            except AccountConnectConfigurationError as error:
                record_account_connect_sync_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect_url_missing",
                )
                raise HTTPException(status_code=503, detail=str(error)) from error
            except AccountConnectSyncError as error:
                record_account_connect_sync_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect_sync_failed",
                )
                raise HTTPException(status_code=502, detail=str(error)) from error
            return AccountConnectSyncResponse(
                project_id=result["project_id"],
                edition=edition_status_from_service(result["edition"]),
                synced_entitlement_count=result["synced_entitlement_count"],
            )

    @app.get(
        "/v1/account-connect/health-checks",
        response_model=AccountConnectHealthCheckListResponse,
    )
    def list_account_connect_health_checks(
        project_id: str,
        limit: int = 50,
    ) -> AccountConnectHealthCheckListResponse:
        with begin(engine) as connection:
            items = commercial.list_health_checks(connection, project_id, limit=limit)
            return AccountConnectHealthCheckListResponse(
                project_id=project_id,
                items=[
                    account_connect_health_check_from_service(item)
                    for item in items
                ],
            )

    @app.post(
        "/v1/account-connect/health-checks",
        response_model=AccountConnectHealthCheckSummary,
        status_code=201,
    )
    def run_account_connect_health_check(
        payload: AccountConnectHealthCheckRunRequest,
    ) -> AccountConnectHealthCheckSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                check = commercial.run_health_check(
                    connection,
                    payload.project_id,
                    actor=payload.actor,
                    hosted_org_id=payload.hosted_org_id,
                    check_kind=payload.check_kind,
                    metadata=payload.metadata,
                )
            except AccountConnectAuthSessionStateError as error:
                record_account_connect_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect.health_check_denied",
                    "account_connect_hosted_org_mismatch",
                    "account_connect_health_check",
                )
                raise HTTPException(status_code=409, detail=str(error)) from error
            return account_connect_health_check_from_service(check)

    @app.get(
        "/v1/account-connect/auth-sessions",
        response_model=AccountConnectAuthSessionListResponse,
    )
    def list_account_connect_auth_sessions(
        project_id: str,
        limit: int = 50,
    ) -> AccountConnectAuthSessionListResponse:
        with begin(engine) as connection:
            items = commercial.list_auth_sessions(connection, project_id, limit=limit)
            return AccountConnectAuthSessionListResponse(
                project_id=project_id,
                items=[
                    account_connect_auth_session_from_service(item)
                    for item in items
                ],
            )

    @app.post(
        "/v1/account-connect/auth-sessions",
        response_model=AccountConnectAuthSessionSummary,
        status_code=201,
    )
    def create_account_connect_auth_session(
        payload: AccountConnectAuthSessionCreateRequest,
    ) -> AccountConnectAuthSessionSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                session = commercial.create_auth_session(
                    connection,
                    payload.project_id,
                    actor=payload.actor,
                    return_url=payload.return_url,
                    hosted_org_id=payload.hosted_org_id,
                    metadata=payload.metadata,
                )
            except AccountConnectAuthSessionStateError as error:
                record_account_connect_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect.auth_session_denied",
                    "account_connect_hosted_org_mismatch",
                    "account_connect_auth_session",
                )
                raise HTTPException(status_code=409, detail=str(error)) from error
            return account_connect_auth_session_from_service(session)

    @app.post(
        "/v1/account-connect/auth-sessions/{session_id}/complete",
        response_model=AccountConnectAuthSessionSummary,
    )
    def complete_account_connect_auth_session(
        session_id: str,
        payload: AccountConnectAuthSessionCompleteRequest,
    ) -> AccountConnectAuthSessionSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                session = commercial.complete_auth_session(
                    connection,
                    session_id,
                    project_id=payload.project_id,
                    actor=payload.actor,
                    code=payload.code,
                    state=payload.state,
                    metadata=payload.metadata,
                )
            except AccountConnectAuthSessionNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except AccountConnectAuthSessionStateError as error:
                record_account_connect_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect.auth_session_denied",
                    "state_mismatch",
                    "account_connect_auth_session",
                )
                raise HTTPException(status_code=409, detail=str(error)) from error
            return account_connect_auth_session_from_service(session)

    @app.get(
        "/v1/account-connect/team-invites",
        response_model=AccountConnectTeamInviteListResponse,
    )
    def list_account_connect_team_invites(
        project_id: str,
        limit: int = 50,
    ) -> AccountConnectTeamInviteListResponse:
        with begin(engine) as connection:
            items = commercial.list_team_invites(connection, project_id, limit=limit)
            return AccountConnectTeamInviteListResponse(
                project_id=project_id,
                items=[
                    account_connect_team_invite_from_service(item)
                    for item in items
                ],
            )

    @app.post(
        "/v1/account-connect/team-invites",
        response_model=AccountConnectTeamInviteSummary,
        status_code=201,
    )
    def create_account_connect_team_invite(
        payload: AccountConnectTeamInviteCreateRequest,
    ) -> AccountConnectTeamInviteSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                invite = commercial.create_team_invite(
                    connection,
                    payload.project_id,
                    email=payload.email,
                    display_name=payload.display_name,
                    role_keys=payload.role_keys,
                    permissions=payload.permissions,
                    metadata=payload.metadata,
                    actor=payload.actor,
                )
            except AccountConnectLinkNotFoundError as error:
                record_account_connect_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect.team_invite_denied",
                    "account_connect_not_linked",
                    "account_connect_team_invite",
                )
                raise HTTPException(status_code=404, detail=str(error)) from error
            return account_connect_team_invite_from_service(invite)

    @app.post(
        "/v1/account-connect/team-invites/{invite_id}/revoke",
        response_model=AccountConnectTeamInviteSummary,
    )
    def revoke_account_connect_team_invite(
        invite_id: str,
        payload: AccountConnectTeamInviteRevokeRequest,
    ) -> AccountConnectTeamInviteSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                invite = commercial.revoke_team_invite(
                    connection,
                    invite_id,
                    project_id=payload.project_id,
                    actor=payload.actor,
                    reason=payload.reason,
                )
            except AccountConnectLinkNotFoundError as error:
                record_account_connect_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect.team_invite_revoke_denied",
                    "account_connect_team_invite_not_found",
                    "account_connect_team_invite",
                )
                raise HTTPException(status_code=404, detail=str(error)) from error
            return account_connect_team_invite_from_service(invite)

    @app.get(
        "/v1/account-connect/billing-portal-sessions",
        response_model=AccountConnectBillingPortalSessionListResponse,
    )
    def list_account_connect_billing_portal_sessions(
        project_id: str,
        limit: int = 50,
    ) -> AccountConnectBillingPortalSessionListResponse:
        with begin(engine) as connection:
            items = commercial.list_billing_portal_sessions(
                connection,
                project_id,
                limit=limit,
            )
            return AccountConnectBillingPortalSessionListResponse(
                project_id=project_id,
                items=[
                    account_connect_billing_session_from_service(item)
                    for item in items
                ],
            )

    @app.post(
        "/v1/account-connect/billing-portal-sessions",
        response_model=AccountConnectBillingPortalSessionSummary,
        status_code=201,
    )
    def create_account_connect_billing_portal_session(
        payload: AccountConnectBillingPortalSessionCreateRequest,
    ) -> AccountConnectBillingPortalSessionSummary:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            try:
                session = commercial.create_billing_portal_session(
                    connection,
                    payload.project_id,
                    actor=payload.actor,
                    return_url=payload.return_url,
                    metadata=payload.metadata,
                )
            except AccountConnectLinkNotFoundError as error:
                record_account_connect_denial(
                    payload.project_id,
                    payload.actor,
                    "account_connect.billing_portal_denied",
                    "account_connect_not_linked",
                    "account_connect_billing_portal_session",
                )
                raise HTTPException(status_code=404, detail=str(error)) from error
            return account_connect_billing_session_from_service(session)

    @app.get(
        "/v1/marketplace/listings",
        response_model=HostedMarketplaceListingListResponse,
    )
    def list_hosted_marketplace_listings(
        project_id: str,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> HostedMarketplaceListingListResponse:
        with begin(engine) as connection:
            listings = commercial.list_marketplace_pack_listings(
                connection,
                project_id,
                include_inactive=include_inactive,
                limit=limit,
            )
            return HostedMarketplaceListingListResponse(
                project_id=project_id,
                items=[
                    hosted_marketplace_listing_from_service(listing)
                    for listing in listings
                ],
            )

    @app.get(
        "/v1/marketplace/sync-runs",
        response_model=HostedMarketplaceSyncRunListResponse,
    )
    def list_hosted_marketplace_sync_runs(
        project_id: str,
        limit: int = 50,
    ) -> HostedMarketplaceSyncRunListResponse:
        with begin(engine) as connection:
            runs = commercial.list_marketplace_sync_runs(
                connection,
                project_id,
                limit=limit,
            )
            return HostedMarketplaceSyncRunListResponse(
                project_id=project_id,
                items=[
                    hosted_marketplace_sync_run_from_service(run)
                    for run in runs
                ],
            )

    @app.post(
        "/v1/marketplace/sync",
        response_model=HostedMarketplaceSyncResponse,
        status_code=201,
    )
    def sync_hosted_marketplace(
        payload: HostedMarketplaceSyncRequest,
    ) -> HostedMarketplaceSyncResponse:
        with begin(engine) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "module_manage",
            )
            try:
                result = commercial.sync_marketplace_pack_listings(
                    connection,
                    payload.project_id,
                    actor=payload.actor,
                    hosted_org_id=payload.hosted_org_id,
                    metadata=payload.metadata,
                )
            except AccountConnectLinkNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except AccountConnectAuthSessionStateError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return HostedMarketplaceSyncResponse(
                project_id=result["project_id"],
                sync_run=hosted_marketplace_sync_run_from_service(result["sync_run"]),
                listing_count=result["listing_count"],
            )
