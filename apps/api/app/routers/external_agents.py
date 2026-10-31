"""KG P6b: the HTTP API over P6a's external-agent registration (engine, no console).

Register, list, get, test, suspend and remove a bank's own HTTP-hosted agent. Every route
calls through to ``services.knowledge.external_agents``/``external_agent_tokens`` — this module
adds no new storage and resolves the auth secret only inside ``test`` (never returns it).

Test is BINARY ON PURPOSE (PRD FR1/NFR3): the caller sees `{"reachable": bool}` only, never a
reason that would let a prober tell "wrong credential" apart from "no network path." The
detailed exception is logged server-side for the admin's own audit trail.

Suspend/remove revoke every live KG token the agent holds (``external_agent_tokens.
revoke_all_for_agent``) so a run already in flight loses access at its very next call, not at
the end of its token's TTL — the fail-closed guarantee the PRD's open question Q2 calls for.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge import external_agent_tokens, external_agents
from boss_engine.services.secret_backends import SecretUnavailable, resolve_secret_ref

log = logging.getLogger(__name__)

router = APIRouter(tags=["external-agents"])

_TEST_TIMEOUT_SECONDS = 3.0


class RegisterExternalAgentRequest(BaseModel):
    project_id: str
    display_name: str
    endpoint_url: str
    secret_ref_id: str
    sponsor_principal_id: str
    actor: str


class ActorRequest(BaseModel):
    project_id: str
    actor: str


def _require_actor(connection, project_id: str, actor: str, permission: str) -> str:
    from apps.api.app.main import require_project_write_actor

    return require_project_write_actor(connection, project_id, actor, permission)


def _get_or_404(connection, project_id: str, agent_id: str) -> dict[str, Any]:
    agent = external_agents.get(connection, project_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="external agent not found")
    return agent


def _public(agent: dict[str, Any]) -> dict[str, Any]:
    """Everything a console surface may show: never the secret's value, only its reference id."""
    return {
        "id": agent["id"],
        "project_id": agent["project_id"],
        "display_name": agent["display_name"],
        "endpoint_url": agent["endpoint_url"],
        "secret_ref_id": agent["secret_ref"],
        "sponsor_principal_id": agent["sponsor_principal_id"],
        "status": agent["status"],
        "created_by": agent["created_by"],
        "created_at": str(agent["created_at"]),
        "updated_at": str(agent["updated_at"]),
    }


@router.get("/v1/external-agents")
@permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
def list_external_agents(project_id: str) -> dict[str, Any]:
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        rows = connection.execute(
            __import__("sqlalchemy").text(
                "SELECT id FROM external_agents WHERE project_id = :p ORDER BY created_at"
            ), {"p": project_id},
        ).scalars().all()
        items = [_public(_get_or_404(connection, project_id, row)) for row in rows]
    return {"project_id": project_id, "items": items}


@router.post("/v1/external-agents", status_code=201)
@permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
def register_external_agent(body: RegisterExternalAgentRequest) -> dict[str, Any]:
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        actor = _require_actor(connection, body.project_id, body.actor, "role_review")
        try:
            agent = external_agents.register(
                connection, body.project_id,
                display_name=body.display_name, endpoint_url=body.endpoint_url,
                secret_ref=body.secret_ref_id, sponsor_principal_id=body.sponsor_principal_id,
                created_by=actor,
            )
        except external_agents.ExternalAgentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    return _public(agent)


@router.get("/v1/external-agents/{agent_id}")
@permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
def get_external_agent(agent_id: str, project_id: str) -> dict[str, Any]:
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        agent = _get_or_404(connection, project_id, agent_id)
    return _public(agent)


@router.post("/v1/external-agents/{agent_id}/test")
@permit(P.AGENT_TEST, scope=Scope.project(body="project_id"))
def test_external_agent(agent_id: str, body: ActorRequest) -> dict[str, bool]:
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        agent = _get_or_404(connection, body.project_id, agent_id)
        try:
            # Resolve+validate once and pin the real call below to that exact address — a
            # second, independent resolution at connect time is a DNS-rebinding SSRF bypass of
            # this very check (the hostname can answer differently a moment later).
            pinned_ip = external_agents.resolve_validated_address(agent["endpoint_url"])
            secret_value = resolve_secret_ref(
                connection, agent["secret_ref"], settings, project_id=body.project_id)
        except (external_agents.ExternalAgentError, SecretUnavailable) as error:
            log.info("external agent %s test failed before dialing: %s", agent_id, error)
            return {"reachable": False}

    request_url = agent["endpoint_url"]
    headers = {"Authorization": f"Bearer {secret_value}"}
    extensions: dict[str, Any] = {}
    if pinned_ip is not None:
        parsed = urlparse(request_url)
        host_literal = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_url = parsed._replace(netloc=f"{host_literal}:{port}").geturl()
        headers["Host"] = parsed.hostname or ""
        extensions["sni_hostname"] = parsed.hostname or ""

    try:
        with httpx.Client(timeout=_TEST_TIMEOUT_SECONDS) as http_client:
            response = http_client.post(
                request_url, json={"ping": True}, headers=headers, extensions=extensions,
            )
        reachable = 200 <= response.status_code < 300
        if not reachable:
            log.info("external agent %s test: endpoint returned %s", agent_id, response.status_code)
    except httpx.HTTPError as error:
        log.info("external agent %s test: %s", agent_id,
                  external_agents.scrub(str(error), secret_value))
        reachable = False
    return {"reachable": reachable}


@router.post("/v1/external-agents/{agent_id}/suspend")
@permit(P.AGENT_LIFECYCLE, scope=Scope.project(body="project_id"))
def suspend_external_agent(agent_id: str, body: ActorRequest) -> dict[str, Any]:
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        actor = _require_actor(connection, body.project_id, body.actor, "workflow_manage")
        _get_or_404(connection, body.project_id, agent_id)
        agent = external_agents.set_status(
            connection, body.project_id, agent_id, "suspended",
            actor_id=actor, reason="suspended from console")
        external_agent_tokens.revoke_all_for_agent(connection, body.project_id, agent_id)
    return _public(agent)


@router.post("/v1/external-agents/{agent_id}/remove")
@permit(P.AGENT_LIFECYCLE, scope=Scope.project(body="project_id"))
def remove_external_agent(agent_id: str, body: ActorRequest) -> dict[str, Any]:
    settings = get_settings()
    with begin(get_engine(settings)) as connection:
        actor = _require_actor(connection, body.project_id, body.actor, "workflow_manage")
        _get_or_404(connection, body.project_id, agent_id)
        agent = external_agents.set_status(
            connection, body.project_id, agent_id, "suspended",
            actor_id=actor, reason="removed from console")
        external_agent_tokens.revoke_all_for_agent(connection, body.project_id, agent_id)
    return _public(agent)


def build_external_agents_router() -> APIRouter:
    return router
