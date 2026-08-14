"""Community local / OpenAI-compatible entitlement source configuration.

Stores ``base_url`` (e.g. http://127.0.0.1:11434/v1) plus an optional SecretRef
on ``approved_local_model_host``. The raw credential is never returned.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.entitlements import (
    EntitlementBrokerService,
    EntitlementSourceNotFoundError,
)
from boss_engine.services.openai_adapter import adapter_for_entitlement_source
from boss_engine.services.secrets_service import put_secret


router = APIRouter(tags=["local-model-source"])


class ConfigureLocalEndpointRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    base_url: str = Field(..., min_length=8)
    secret_ref_id: Optional[str] = None
    credential: Optional[str] = Field(
        default=None,
        description="Optional dummy API key for local hosts that require a header.",
    )


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


def _public_payload(result: dict[str, Any]) -> dict[str, Any]:
    blob = json_safe(result)
    return blob


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: json_safe(item)
            for key, item in value.items()
            if not any(
                term in str(key).lower()
                for term in ("secret_value", "api_key", "password", "token_value")
            )
        }
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


@router.post("/v1/entitlements/sources/{source_id}/openai-compatible")
def configure_openai_compatible_source(
    source_id: str, payload: ConfigureLocalEndpointRequest
) -> dict[str, Any]:
    settings = get_settings()
    entitlements = EntitlementBrokerService(settings)
    with begin(get_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "entitlement_manage")
        secret_ref_id = payload.secret_ref_id
        if payload.credential and not secret_ref_id:
            stored = put_secret(
                connection,
                settings,
                project_id=payload.project_id,
                name=f"model_{source_id}_local_credential",
                value=payload.credential,
                secret_kind="api_token",
                backend="local_vault",
                actor=payload.actor,
            )
            secret_ref_id = stored["secret_ref_id"]
            connection.execute(
                text("UPDATE secret_refs SET provider='local' WHERE id=:id"),
                {"id": secret_ref_id},
            )
        try:
            result = entitlements.configure_openai_compatible_source(
                connection,
                project_id=payload.project_id,
                source_id=source_id,
                base_url=payload.base_url,
                actor=payload.actor,
                secret_ref_id=secret_ref_id,
            )
        except EntitlementSourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        source = entitlements.get_source(connection, payload.project_id, source_id)
        adapter = adapter_for_entitlement_source(settings, source, api_key="local")
        result["adapter"] = {
            "base_url": adapter._resolved_base_url(),
            "receives_source_base_url": adapter._resolved_base_url()
            == result["endpoint"]["base_url"],
        }
        return _public_payload(result)
