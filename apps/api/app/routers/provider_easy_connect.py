"""Community BYOK paste-and-connect for model and infra provider keys.

``POST /v1/providers/easy-connect`` writes the pasted value through to a
secret backend (default ``local_vault``) and binds a pointer-only provider
account. The raw secret is never stored in Postgres and is never returned.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError

from boss_engine.services.resource_plane import ResourcePlaneService
from boss_engine.services.secret_backends import SecretUnavailable

from ..db import begin, get_engine


router = APIRouter(tags=["providers"])

_MIN_SECRET = int(getattr(ResourcePlaneService, "MIN_EASY_CONNECT_SECRET_LENGTH", 8))
_ECHO_FORBIDDEN_KEYS = frozenset(
    {
        "secret_value",
        "raw_secret",
        "plaintext",
        "api_key_value",
        "token_value",
        "password",
    }
)


class ProviderEasyConnectRequest(BaseModel):
    project_id: str
    actor: str
    provider: str
    account_label: str
    secret_value: str = Field(min_length=1)
    slot: Optional[str] = None
    auth_method: Optional[str] = None
    base_url: Optional[str] = None
    backend: str = "local_vault"
    read_only: bool = True
    test_after: bool = False


class ProviderEasyConnectResponse(BaseModel):
    provider_account: dict[str, Any]
    secret: dict[str, Any]
    secret_ref_id: str
    raw_secret_stored: bool = False
    replaced: bool = False
    rotated: bool = False
    test: Optional[dict[str, Any]] = None


def _validate_secret_value(secret_value: str) -> str:
    cleaned = str(secret_value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="secret_value is required")
    if len(cleaned) < _MIN_SECRET:
        raise HTTPException(
            status_code=422,
            detail=f"secret_value is too short (minimum {_MIN_SECRET} characters)",
        )
    return cleaned


def _redact_secret_echo(payload: Any, secret_value: str) -> Any:
    """Drop raw-secret fields and scrub the pasted value if it leaks into a string."""
    if isinstance(payload, dict):
        return {
            key: _redact_secret_echo(value, secret_value)
            for key, value in payload.items()
            if str(key).lower() not in _ECHO_FORBIDDEN_KEYS
        }
    if isinstance(payload, list):
        return [_redact_secret_echo(item, secret_value) for item in payload]
    if isinstance(payload, str) and secret_value and secret_value in payload:
        return payload.replace(secret_value, "[redacted]")
    return payload


def _unavailable_http_error(error: BaseException) -> HTTPException:
    from apps.api.app.main import undefined_table_response

    schema = undefined_table_response(error)
    if schema is not None:
        return HTTPException(
            status_code=503,
            detail="Required database relation is not applied on this runtime.",
        )
    if isinstance(error, SecretUnavailable):
        return HTTPException(
            status_code=503,
            detail=str(error).strip() or "Secret backend is unavailable.",
        )
    if isinstance(error, OSError):
        return HTTPException(status_code=503, detail="local_vault is unavailable.")
    return HTTPException(
        status_code=503,
        detail="Secret backend or schema is unavailable.",
    )


@router.post(
    "/v1/providers/easy-connect",
    status_code=201,
    response_model=ProviderEasyConnectResponse,
    responses={
        422: {"description": "Validation error"},
        503: {"description": "Vault or schema unavailable"},
    },
)
def easy_connect_provider(payload: ProviderEasyConnectRequest) -> dict[str, Any]:
    from apps.api.app.main import require_project_actor, resource_plane

    cleaned_secret = _validate_secret_value(payload.secret_value)
    try:
        with begin(get_engine()) as connection:
            require_project_actor(
                connection,
                payload.project_id,
                payload.actor,
                "providerops_manage",
            )
            result = resource_plane.easy_connect(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
                provider=payload.provider,
                account_label=payload.account_label,
                secret_value=cleaned_secret,
                slot=payload.slot,
                auth_method=payload.auth_method,
                base_url=payload.base_url,
                backend=payload.backend or "local_vault",
                read_only=payload.read_only,
                test_after=payload.test_after,
            )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (SecretUnavailable, OSError, ProgrammingError) as error:
        raise _unavailable_http_error(error) from error
    return _redact_secret_echo(result, cleaned_secret)


__all__ = [
    "router",
    "ProviderEasyConnectRequest",
    "ProviderEasyConnectResponse",
]
