from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from .config import Settings, get_settings
from .db import begin, ensure_schema, ensure_seed_org, get_engine

app = FastAPI(title="BOSS Account Connect", version="0.1.0")


class HealthResponse(BaseModel):
    status: str
    service: str = "boss-account-connect"


class EntitlementItem(BaseModel):
    entitlement_key: str
    status: str = "active"
    edition: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntitlementsResponse(BaseModel):
    entitlements: list[EntitlementItem]


class MarketplacePackItem(BaseModel):
    pack_id: str
    display_name: str
    description: str = ""
    publisher: str = "BOSS"
    price_model: str = "free"
    price_usd_micros: int = 0
    required_entitlement: Optional[str] = None
    visibility: str = "catalog"
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class MarketplaceResponse(BaseModel):
    items: list[MarketplacePackItem]
    provider_ref: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuthSessionCreateRequest(BaseModel):
    project_id: str
    hosted_org_id: Optional[str] = None
    return_url: Optional[str] = None
    state: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuthSessionResponse(BaseModel):
    id: str
    authorization_url: str
    status: str
    project_id: str
    hosted_org_id: Optional[str] = None


class AuthSessionCompleteRequest(BaseModel):
    project_id: str
    code: str
    state: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuthSessionCompleteResponse(BaseModel):
    hosted_org_id: str
    status: str = "completed"
    project_id: str


class TeamInviteCreateRequest(BaseModel):
    project_id: str
    email: str
    display_name: Optional[str] = None
    role_keys: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TeamInviteResponse(BaseModel):
    id: str
    invite_id: str
    status: str
    email: str


class TeamInviteRevokeRequest(BaseModel):
    project_id: str
    reason: Optional[str] = None


class BillingPortalSessionRequest(BaseModel):
    project_id: str
    return_url: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BillingPortalSessionResponse(BaseModel):
    id: str
    portal_url: str
    status: str = "created"


def _require_service_auth(request: Request, settings: Settings) -> None:
    if not settings.service_token:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ").strip() != settings.service_token:
        raise HTTPException(status_code=401, detail="Invalid Account Connect service token.")


def _org_id(org_id: Optional[str], settings: Settings) -> str:
    return org_id or settings.default_org_id


@app.on_event("startup")
def startup() -> None:
    settings = get_settings()
    engine = get_engine(settings)
    schema_path = str(
        __import__("pathlib").Path(__file__).resolve().parents[1] / "db" / "schema.sql"
    )
    with begin(engine) as connection:
        ensure_schema(connection, schema_path)
        ensure_seed_org(connection, settings.default_org_id, settings.default_org_name)
        connection.execute(
            text(
                """
                INSERT INTO ac_entitlements (
                    id, org_id, project_id, entitlement_key, status, edition, capabilities
                )
                VALUES (
                    :id, :org_id, NULL, 'cloud_control_plane', 'active', 'cloud',
                    CAST(:capabilities AS JSONB)
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": f"ent_{settings.default_org_id}_cloud",
                "org_id": settings.default_org_id,
                "capabilities": json.dumps(
                    ["hosted_auth", "marketplace", "team_invites", "billing_portal"]
                ),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO ac_marketplace_packs (
                    id, org_id, pack_id, display_name, description, publisher,
                    price_model, price_usd_micros, required_entitlement, visibility, status
                )
                VALUES (
                    :id, :org_id, 'core_v1', 'Core Pack v1', 'Default BOSS company pack.',
                    'BOSS', 'free', 0, 'cloud_control_plane', 'catalog', 'active'
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": f"pack_{settings.default_org_id}_core_v1", "org_id": settings.default_org_id},
        )


@app.get("/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="healthy")


class DeviceAuthorizeRequest(BaseModel):
    """User completes device Easy Connect by entering the short user_code."""

    user_code: str
    hosted_org_id: Optional[str] = None


class DeviceAuthorizeResponse(BaseModel):
    status: str
    user_code: str
    hosted_org_id: Optional[str] = None
    message: str


@app.post("/v1/device/authorize", response_model=DeviceAuthorizeResponse)
def device_authorize(
    payload: DeviceAuthorizeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> DeviceAuthorizeResponse:
    """Mark a device Easy Connect user_code as authorized for a hosted org.

    Control planes poll their local auth session; once metadata carries
    authorized_hosted_org_id they complete the Account Connect link.
    """
    _require_service_auth(request, settings)
    code = payload.user_code.strip().upper().replace(" ", "")
    if len(code) < 6:
        raise HTTPException(status_code=422, detail="user_code is required")
    hosted = (payload.hosted_org_id or settings.default_org_id).strip()
    engine = get_engine(settings)
    with begin(engine) as connection:
        # Persist authorization so control-plane adapters can fetch it
        connection.execute(
            text(
                """
                INSERT INTO ac_device_authorizations (
                    id, user_code, hosted_org_id, status, created_at
                )
                VALUES (
                    :id, :user_code, :hosted_org_id, 'authorized', now()
                )
                ON CONFLICT (user_code) DO UPDATE
                SET hosted_org_id = EXCLUDED.hosted_org_id,
                    status = 'authorized',
                    created_at = now()
                """
            ),
            {
                "id": f"devauth_{uuid.uuid4().hex[:12]}",
                "user_code": code,
                "hosted_org_id": hosted,
            },
        )
    return DeviceAuthorizeResponse(
        status="authorized",
        user_code=code,
        hosted_org_id=hosted,
        message="Device authorized. Return to Mission Control; linking will complete on poll.",
    )


@app.get("/v1/device/authorizations/{user_code}")
def get_device_authorization(
    user_code: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _require_service_auth(request, settings)
    code = user_code.strip().upper().replace(" ", "")
    engine = get_engine(settings)
    with begin(engine) as connection:
        row = connection.execute(
            text(
                """
                SELECT user_code, hosted_org_id, status, created_at
                FROM ac_device_authorizations
                WHERE user_code = :user_code
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_code": code},
        ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="user_code not authorized yet")
    return {
        "user_code": row["user_code"],
        "hosted_org_id": row["hosted_org_id"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else row["created_at"],
    }


@app.get("/v1/orgs/{org_id}/health", response_model=HealthResponse)
def org_health(org_id: str) -> HealthResponse:
    return HealthResponse(status="healthy")


@app.get("/v1/orgs/{org_id}/entitlements", response_model=EntitlementsResponse)
def list_entitlements(
    org_id: str,
    project_id: Optional[str] = None,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> EntitlementsResponse:
    _require_service_auth(request, settings)
    engine = get_engine(settings)
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT entitlement_key, status, edition, capabilities, metadata
                FROM ac_entitlements
                WHERE org_id = :org_id
                  AND (project_id IS NULL OR project_id = :project_id OR :project_id IS NULL)
                ORDER BY entitlement_key
                """
            ),
            {"org_id": org_id, "project_id": project_id},
        ).mappings().all()
    return EntitlementsResponse(
        entitlements=[
            EntitlementItem(
                entitlement_key=row["entitlement_key"],
                status=row["status"],
                edition=row["edition"],
                capabilities=row["capabilities"] or [],
                metadata=row["metadata"] or {},
            )
            for row in rows
        ]
    )


@app.get("/v1/orgs/{org_id}/marketplace/packs", response_model=MarketplaceResponse)
def list_marketplace_packs(
    org_id: str,
    project_id: Optional[str] = None,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> MarketplaceResponse:
    _require_service_auth(request, settings)
    engine = get_engine(settings)
    with begin(engine) as connection:
        rows = connection.execute(
            text(
                """
                SELECT pack_id, display_name, description, publisher, price_model,
                       price_usd_micros, required_entitlement, visibility, status, metadata
                FROM ac_marketplace_packs
                WHERE org_id = :org_id AND status = 'active'
                ORDER BY pack_id
                """
            ),
            {"org_id": org_id},
        ).mappings().all()
    sync_id = f"sync_{uuid.uuid4().hex[:12]}"
    return MarketplaceResponse(
        items=[
            MarketplacePackItem(
                pack_id=row["pack_id"],
                display_name=row["display_name"],
                description=row["description"],
                publisher=row["publisher"],
                price_model=row["price_model"],
                price_usd_micros=row["price_usd_micros"],
                required_entitlement=row["required_entitlement"],
                visibility=row["visibility"],
                status=row["status"],
                metadata=row["metadata"] or {},
            )
            for row in rows
        ],
        provider_ref=sync_id,
        metadata={"project_id": project_id} if project_id else {},
    )


@app.post("/v1/orgs/{org_id}/auth-sessions", response_model=AuthSessionResponse, status_code=201)
@app.post("/v1/auth-sessions", response_model=AuthSessionResponse, status_code=201)
def create_auth_session(
    payload: AuthSessionCreateRequest,
    org_id: Optional[str] = None,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> AuthSessionResponse:
    _require_service_auth(request, settings)
    resolved_org = _org_id(org_id or payload.hosted_org_id, settings)
    session_id = f"ac_auth_{uuid.uuid4().hex[:16]}"
    authorization_url = (
        f"{settings.public_base_url.rstrip('/')}/v1/demo/authorize"
        f"?session_id={session_id}&state={payload.state}"
    )
    engine = get_engine(settings)
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO ac_auth_sessions (
                    id, org_id, project_id, state, status, authorization_url, return_url, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :state, 'pending', :authorization_url,
                    :return_url, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": session_id,
                "org_id": resolved_org,
                "project_id": payload.project_id,
                "state": payload.state,
                "authorization_url": authorization_url,
                "return_url": payload.return_url,
                "metadata": json.dumps(payload.metadata),
            },
        )
    return AuthSessionResponse(
        id=session_id,
        authorization_url=authorization_url,
        status="pending",
        project_id=payload.project_id,
        hosted_org_id=resolved_org,
    )


@app.post(
    "/v1/orgs/{org_id}/auth-sessions/{session_id}/complete",
    response_model=AuthSessionCompleteResponse,
)
@app.post(
    "/v1/auth-sessions/{session_id}/complete",
    response_model=AuthSessionCompleteResponse,
)
def complete_auth_session(
    session_id: str,
    payload: AuthSessionCompleteRequest,
    org_id: Optional[str] = None,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> AuthSessionCompleteResponse:
    _require_service_auth(request, settings)
    engine = get_engine(settings)
    with begin(engine) as connection:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, state, status
                FROM ac_auth_sessions
                WHERE id = :session_id
                """
            ),
            {"session_id": session_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Auth session not found.")
        if row["state"] != payload.state:
            raise HTTPException(status_code=409, detail="Auth session state mismatch.")
        if row["project_id"] != payload.project_id:
            raise HTTPException(status_code=422, detail="Auth session project mismatch.")
        resolved_org = _org_id(org_id or row["org_id"], settings)
        connection.execute(
            text(
                """
                UPDATE ac_auth_sessions
                SET status = 'completed', updated_at = now()
                WHERE id = :session_id
                """
            ),
            {"session_id": session_id},
        )
    return AuthSessionCompleteResponse(
        hosted_org_id=resolved_org,
        status="completed",
        project_id=payload.project_id,
    )


@app.post("/v1/orgs/{org_id}/team-invites", response_model=TeamInviteResponse, status_code=201)
def create_team_invite(
    org_id: str,
    payload: TeamInviteCreateRequest,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> TeamInviteResponse:
    _require_service_auth(request, settings)
    invite_id = f"invite_{uuid.uuid4().hex[:12]}"
    engine = get_engine(settings)
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO ac_team_invites (
                    id, org_id, project_id, email, display_name, role_keys, permissions, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :email, :display_name,
                    CAST(:role_keys AS JSONB), CAST(:permissions AS JSONB), CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": invite_id,
                "org_id": org_id,
                "project_id": payload.project_id,
                "email": payload.email,
                "display_name": payload.display_name,
                "role_keys": json.dumps(payload.role_keys),
                "permissions": json.dumps(payload.permissions),
                "metadata": json.dumps(payload.metadata),
            },
        )
    return TeamInviteResponse(
        id=invite_id,
        invite_id=invite_id,
        status="pending",
        email=payload.email,
    )


@app.post("/v1/orgs/{org_id}/team-invites/{invite_id}/revoke")
def revoke_team_invite(
    org_id: str,
    invite_id: str,
    payload: TeamInviteRevokeRequest,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    _require_service_auth(request, settings)
    engine = get_engine(settings)
    with begin(engine) as connection:
        result = connection.execute(
            text(
                """
                UPDATE ac_team_invites
                SET status = 'revoked', updated_at = now()
                WHERE id = :invite_id AND org_id = :org_id AND project_id = :project_id
                """
            ),
            {
                "invite_id": invite_id,
                "org_id": org_id,
                "project_id": payload.project_id,
            },
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Team invite not found.")
    return {"id": invite_id, "status": "revoked"}


@app.post(
    "/v1/orgs/{org_id}/billing-portal-sessions",
    response_model=BillingPortalSessionResponse,
    status_code=201,
)
def create_billing_portal_session(
    org_id: str,
    payload: BillingPortalSessionRequest,
    request: Request = None,
    settings: Settings = Depends(get_settings),
) -> BillingPortalSessionResponse:
    _require_service_auth(request, settings)
    session_id = f"bill_{uuid.uuid4().hex[:12]}"
    portal_url = (
        f"{settings.public_base_url.rstrip('/')}/v1/demo/billing"
        f"?session_id={session_id}&org_id={org_id}"
    )
    engine = get_engine(settings)
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO ac_billing_portal_sessions (
                    id, org_id, project_id, portal_url, return_url, metadata
                )
                VALUES (
                    :id, :org_id, :project_id, :portal_url, :return_url, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": session_id,
                "org_id": org_id,
                "project_id": payload.project_id,
                "portal_url": portal_url,
                "return_url": payload.return_url,
                "metadata": json.dumps(payload.metadata),
            },
        )
    return BillingPortalSessionResponse(id=session_id, portal_url=portal_url)
