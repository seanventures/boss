"""Private TCP through a customer connector (BOSSAPP-1258).

A build dials only a host and port a person approved. This router is not mounted
here; the reconciler includes ``router``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services.private_tcp import (
    PrivateTcpFailed,
    PrivateTcpRefused,
    approve_saved,
    dial_through,
    open_connector,
)

router = APIRouter(tags=["private-tcp"])


class ConnectorBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class ApprovalBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)


class DialBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=200)
    connector_id: str = Field(min_length=1, max_length=200)
    build_id: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    payload: str = Field(default="", max_length=4096)
    dedupe_key: str = Field(min_length=1, max_length=200)


def _http(exc: PrivateTcpRefused | PrivateTcpFailed) -> HTTPException:
    status = 403 if isinstance(exc, PrivateTcpRefused) else 502
    return HTTPException(status_code=status, detail={"reason": exc.reason, "message": str(exc)})


@router.post("/v1/private-tcp/connectors")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def create_connector(body: ConnectorBody) -> dict[str, Any]:
    try:
        return open_connector(project_id=body.project_id, name=body.name)
    except (PrivateTcpRefused, PrivateTcpFailed) as exc:
        raise _http(exc) from exc


@router.post("/v1/private-tcp/connectors/{connector_id}/approvals")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def approve_service(connector_id: str, body: ApprovalBody) -> dict[str, Any]:
    try:
        return approve_saved(
            project_id=body.project_id,
            connector_id=connector_id,
            host=body.host,
            port=body.port,
        )
    except (PrivateTcpRefused, PrivateTcpFailed) as exc:
        raise _http(exc) from exc


@router.post("/v1/private-tcp/dial")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def dial_service(body: DialBody) -> dict[str, Any]:
    try:
        return dial_through(
            project_id=body.project_id,
            connector_id=body.connector_id,
            build_id=body.build_id,
            host=body.host,
            port=body.port,
            payload=body.payload,
            dedupe_key=body.dedupe_key,
        )
    except (PrivateTcpRefused, PrivateTcpFailed) as exc:
        raise _http(exc) from exc
