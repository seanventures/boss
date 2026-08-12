"""HTTP surface for BOSS-wide agent key management (KMS).

Prefix: ``/v1/kms``

Agents store keys for themselves, grant access to other agents, and obtain
short-TTL leases to use material. List endpoints never return secret values.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services import agent_kms as kms
from boss_engine.services.secret_backends import SecretUnavailable

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(prefix="/v1/kms", tags=["kms"])


class StoreKeyRequest(BaseModel):
    project_id: str
    name: str
    value: str
    owner_agent: str
    actor: str = "operator"
    purpose: str = "general"
    secret_kind: str = "api_token"
    backend: str = "local_vault"
    grant_to: list[dict[str, Any]] = Field(default_factory=list)
    labels: dict[str, Any] = Field(default_factory=dict)


class GrantRequest(BaseModel):
    project_id: str
    granter_agent: str
    grantee_agent: str
    permissions: list[str] = Field(default_factory=lambda: ["use", "read_meta"])
    actor: str = "operator"
    expires_at: Optional[str] = None


class LeaseRequest(BaseModel):
    project_id: str
    agent_id: str
    actor: str = "system"
    purpose: str = ""
    ttl_seconds: int = Field(default=120, ge=15, le=900)
    reveal: bool = False
    max_reveals: int = Field(default=1, ge=1, le=5)


class ResolveLeaseRequest(BaseModel):
    project_id: str
    agent_id: str
    actor: str = "system"


class RevokeGrantRequest(BaseModel):
    project_id: str
    actor_agent: str
    actor: str = "operator"


@router.get("/backends")
def list_backends():
    return {"items": kms.backend_catalog(), "count": len(kms.backend_catalog())}


@router.get("/health")
def kms_health():
    return kms.health(get_settings())


@router.post("/keys", status_code=201)
def store_key(payload: StoreKeyRequest):
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            return kms.store_key(
                connection,
                settings,
                project_id=payload.project_id,
                name=payload.name,
                value=payload.value,
                owner_agent=payload.owner_agent,
                actor=payload.actor,
                purpose=payload.purpose,
                secret_kind=payload.secret_kind,
                backend=payload.backend,
                grant_to=payload.grant_to,
                labels=payload.labels,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SecretUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/keys")
def list_keys(
    project_id: str = Query(...),
    agent_id: str = Query(...),
):
    try:
        with begin(get_engine()) as connection:
            items = kms.list_keys_for_agent(
                connection, project_id=project_id, agent_id=agent_id
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "project_id": project_id,
        "agent_id": agent_id,
        "items": items,
        "count": len(items),
    }


@router.get("/keys/{key_entry_id}")
def get_key(
    key_entry_id: str,
    project_id: str = Query(...),
    agent_id: str = Query(...),
):
    try:
        with begin(get_engine()) as connection:
            items = kms.list_keys_for_agent(
                connection, project_id=project_id, agent_id=agent_id
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    for item in items:
        if item["id"] == key_entry_id:
            return item
    raise HTTPException(status_code=404, detail="key not found or not granted")


@router.post("/keys/{key_entry_id}/grant", status_code=201)
def grant_key(key_entry_id: str, payload: GrantRequest):
    try:
        with begin(get_engine()) as connection:
            return kms.grant_key(
                connection,
                project_id=payload.project_id,
                key_entry_id=key_entry_id,
                granter_agent=payload.granter_agent,
                grantee_agent=payload.grantee_agent,
                permissions=payload.permissions,
                actor=payload.actor,
                expires_at=payload.expires_at,
            )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/keys/{key_entry_id}/grants/{grantee_agent}")
def revoke_grant(key_entry_id: str, grantee_agent: str, payload: RevokeGrantRequest):
    try:
        with begin(get_engine()) as connection:
            return kms.revoke_grant(
                connection,
                project_id=payload.project_id,
                key_entry_id=key_entry_id,
                grantee_agent=grantee_agent,
                actor_agent=payload.actor_agent,
                actor=payload.actor,
            )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/keys/{key_entry_id}/grants")
def list_grants(
    key_entry_id: str,
    project_id: str = Query(...),
    agent_id: str = Query(...),
):
    try:
        with begin(get_engine()) as connection:
            items = kms.list_grants(
                connection,
                project_id=project_id,
                key_entry_id=key_entry_id,
                agent_id=agent_id,
            )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"key_entry_id": key_entry_id, "items": items, "count": len(items)}


@router.post("/keys/{key_entry_id}/lease", status_code=201)
def lease_key(key_entry_id: str, payload: LeaseRequest):
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            return kms.lease_key(
                connection,
                settings,
                project_id=payload.project_id,
                key_entry_id=key_entry_id,
                agent_id=payload.agent_id,
                actor=payload.actor,
                purpose=payload.purpose,
                ttl_seconds=payload.ttl_seconds,
                reveal=payload.reveal,
                max_reveals=payload.max_reveals,
            )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, SecretUnavailable) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/leases/{lease_id}/resolve")
def resolve_lease(lease_id: str, payload: ResolveLeaseRequest):
    """Return secret material for an authorized active lease (counts against reveal limit)."""
    settings = get_settings()
    try:
        with begin(get_engine()) as connection:
            return kms.resolve_lease(
                connection,
                settings,
                project_id=payload.project_id,
                lease_id=lease_id,
                agent_id=payload.agent_id,
                actor=payload.actor,
            )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SecretUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/leases/{lease_id}/revoke")
def revoke_lease(
    lease_id: str,
    project_id: str = Query(...),
    actor: str = Query("operator"),
):
    with begin(get_engine()) as connection:
        return kms.revoke_lease(
            connection, project_id=project_id, lease_id=lease_id, actor=actor
        )
