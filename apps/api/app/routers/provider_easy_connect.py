"""Community BYOK paste-and-connect for model and infra provider keys.

``POST /v1/providers/easy-connect`` writes the pasted value through to a
secret backend (default ``local_vault``) and binds a pointer-only provider
account. The raw secret is never stored in Postgres and is never returned.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.services.secret_backends import SecretUnavailable

from ..db import begin, get_engine


router = APIRouter(tags=["providers"])


class ProviderEasyConnectRequest(BaseModel):
    project_id: str
    actor: str
    provider: str
    account_label: str
    secret_value: str
    slot: Optional[str] = None
    auth_method: Optional[str] = None
    base_url: Optional[str] = None
    backend: str = "local_vault"
    read_only: bool = True
    test_after: bool = False


class ProviderEasyConnectResponse(BaseModel):
    provider_account: dict[str, Any]
    secret: dict[str, Any]
    raw_secret_stored: bool = False
    test: Optional[dict[str, Any]] = None


@router.post(
    "/v1/providers/easy-connect",
    status_code=201,
    response_model=ProviderEasyConnectResponse,
)
def easy_connect_provider(payload: ProviderEasyConnectRequest) -> dict[str, Any]:
    from apps.api.app.main import require_project_actor, resource_plane

    if not str(payload.secret_value or "").strip():
        raise HTTPException(status_code=422, detail="secret_value is required")
    try:
        with begin(get_engine()) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            return resource_plane.easy_connect(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                provider=payload.provider,
                account_label=payload.account_label,
                secret_value=payload.secret_value,
                slot=payload.slot,
                auth_method=payload.auth_method,
                base_url=payload.base_url,
                backend=payload.backend or "local_vault",
                read_only=payload.read_only,
                test_after=payload.test_after,
            )
    except SecretUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


__all__ = [
    "router",
    "ProviderEasyConnectRequest",
    "ProviderEasyConnectResponse",
]
