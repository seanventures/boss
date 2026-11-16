"""A person edits one run's outbound allowlist and sends a call through the proxy (BOSSAPP-1265).

    PUT  /v1/runs/{run_id}/outbound-allowlist
    GET  /v1/runs/{run_id}/outbound-allowlist
    PUT  /v1/runs/{run_id}/egress-secret
    POST /v1/runs/{run_id}/egress

The proxy refuses a host that is not on the list. The secret named for the run
is injected on the outbound request and is not returned or written into the receipt.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit
from boss_engine.services import run_egress

router = APIRouter(tags=["run-egress"])


class AllowlistBody(BaseModel):
    project_id: str
    actor: str
    hosts: list[str] = Field(default_factory=list)


class SecretBody(BaseModel):
    project_id: str
    actor: str
    value: str


class EgressBody(BaseModel):
    project_id: str
    host: str
    method: str = "GET"
    path: str = "/"


def _fail(error: Exception) -> None:
    if isinstance(error, run_egress.RunEgressError):
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    raise error


@router.put("/v1/runs/{run_id}/outbound-allowlist")
@permit(P.SECRET_MANAGE, scope=Scope.project(body="project_id"))
def put_allowlist(run_id: str, body: AllowlistBody) -> dict[str, Any]:
    with begin() as connection:
        try:
            return run_egress.set_allowlist(
                connection, project_id=body.project_id, run_id=run_id,
                hosts=body.hosts, actor=body.actor,
            )
        except run_egress.RunEgressError as error:
            _fail(error)
            raise


@router.get("/v1/runs/{run_id}/outbound-allowlist")
@permit(P.SECRET_READ_METADATA, scope=Scope.project(query="project_id"))
def read_allowlist(run_id: str, project_id: str) -> dict[str, Any]:
    with begin() as connection:
        try:
            return run_egress.get_allowlist(
                connection, project_id=project_id, run_id=run_id,
            )
        except run_egress.RunEgressError as error:
            _fail(error)
            raise


@router.put("/v1/runs/{run_id}/egress-secret")
@permit(P.SECRET_MANAGE, scope=Scope.project(body="project_id"))
def put_secret(run_id: str, body: SecretBody) -> dict[str, Any]:
    with begin() as connection:
        try:
            return run_egress.put_run_secret(
                connection, project_id=body.project_id, run_id=run_id,
                value=body.value, actor=body.actor,
            )
        except run_egress.RunEgressError as error:
            _fail(error)
            raise


@router.post("/v1/runs/{run_id}/egress")
@permit(P.SECRET_MANAGE, scope=Scope.project(body="project_id"))
def send(run_id: str, body: EgressBody) -> dict[str, Any]:
    with begin() as connection:
        try:
            return run_egress.proxy(
                connection, project_id=body.project_id, run_id=run_id,
                host=body.host, method=body.method, path=body.path,
                upstream=run_egress.echo_upstream,
            )
        except run_egress.RunEgressError as error:
            _fail(error)
            raise
