"""The agent resource: create, read, edit and delete one agent, and grant it MCP servers.

ONE AGENT MODEL (engine/boss_engine/services/agents.py): a role, what it holds (skills and MCP
servers), and optionally a Studio profile. Before this an agent was spread over POST /v1/roles
(which, used as an update, reset autonomy and reporting line), POST /v1/role-skills one skill at a
time, Agent Studio's own create/version/publish, and nothing at all for MCP — so no create or
update could carry an agent's skills, and the console's create wizard left an agent holding
nothing.

THE ACTOR IS THE SIGNED-IN PRINCIPAL. Every write binds through `require_write_actor`
(main.require_project_write_actor): on an authed install the API key's principal wins over any
`actor` in the body. The console's wizard posted the literal 'vp_agents', which a 'ceo' key may not
act as, and 403'd on every authed install after having already registered the role.

Mounted from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.db import begin


class AgentCreateBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    name: str
    purpose: str = ""
    role_key: Optional[str] = None
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[Any] = Field(default_factory=list)
    reports_to: Optional[str] = None
    boundaries: Optional[dict[str, Any]] = None


class AgentUpdateBody(BaseModel):
    """Every field optional. A field left out is left alone; `reports_to: null` clears it."""
    project_id: str
    actor: Optional[str] = None
    display_name: Optional[str] = None
    mission: Optional[str] = None
    boundaries: Optional[dict[str, Any]] = None
    reports_to: Optional[str] = None
    skills: Optional[list[str]] = None
    mcp_servers: Optional[list[Any]] = None
    grant_skills: list[str] = Field(default_factory=list)
    grant_mcp_servers: list[Any] = Field(default_factory=list)


class RoleMcpBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    role_key: str
    mcp_server_id: str
    tools: list[str] = Field(default_factory=list)


def build_agents_router(
    *,
    engine,
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import agents

    router = APIRouter(tags=["agents"])

    def _fail(error: Exception):
        if isinstance(error, agents.AgentError):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        raise error

    @router.get("/v1/agents")
    def list_agents(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    "items": agents.list_agents(connection, project_id)}

    @router.get("/v1/agents/grantable")
    def grantable(project_id: str) -> dict[str, Any]:
        """Everything an agent could be given here: skills (authored and built-in) and servers.

        One read for the create form and the wizard, so neither offers a skill the grant would
        then refuse.
        """
        from boss_engine.services.mcp_registry import list_mcp_servers
        from boss_engine.services.skill_definitions import assignable_catalogue

        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {
                "project_id": project_id,
                "skills": assignable_catalogue(connection, project_id),
                "mcp_servers": [
                    {"mcp_server_id": s["id"], "server": s["name"], "url": s["url"],
                     "enabled": s["enabled"], "risk_tier": s["risk_tier"],
                     "description": s.get("description")}
                    for s in list_mcp_servers(connection, project_id)
                ],
                "agents": agents.list_agents(connection, project_id),
            }

    @router.get("/v1/agents/{role_key}")
    def get_agent(role_key: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agents.get_agent(connection, project_id, role_key)
            except Exception as error:  # noqa: BLE001 — typed mapping in _fail
                _fail(error)

    @router.post("/v1/agents", status_code=201)
    def create_agent(body: AgentCreateBody) -> dict[str, Any]:
        """Quick create: name, purpose, skills, MCP servers, reports-to — in one transaction."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "role_review")
            if body.skills or body.mcp_servers:
                # Granting is the same permission everywhere it happens (POST /v1/role-skills).
                require_write_actor(connection, body.project_id, actor, "workflow_manage")
            try:
                return agents.create_agent(
                    connection, body.project_id, actor=actor, name=body.name,
                    purpose=body.purpose, role_key=body.role_key, skills=body.skills,
                    mcp_servers=body.mcp_servers, reports_to=body.reports_to,
                    boundaries=body.boundaries)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.patch("/v1/agents/{role_key}")
    def update_agent(role_key: str, body: AgentUpdateBody) -> dict[str, Any]:
        given = body.model_fields_set
        changes: dict[str, Any] = {
            name: getattr(body, name)
            for name in ("display_name", "mission", "boundaries", "reports_to", "skills",
                         "mcp_servers")
            if name in given
        }
        grants = bool(body.grant_skills or body.grant_mcp_servers
                      or {"skills", "mcp_servers"} & changes.keys())
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "role_review")
            if grants:
                require_write_actor(connection, body.project_id, actor, "workflow_manage")
            try:
                return agents.update_agent(
                    connection, body.project_id, role_key, actor=actor, **changes,
                    grant_skills=body.grant_skills, grant_mcp_servers=body.grant_mcp_servers)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.delete("/v1/agents/{role_key}")
    def delete_agent(role_key: str, project_id: str, actor: Optional[str] = None,
                     retire_older_versions: bool = False) -> dict[str, Any]:
        """`retire_older_versions=true`: an agent named only in older workflow versions that
        nothing starts by default is deleted and those versions retired with it (see
        agents.delete_agent). Without it, the 409 says that is the way through."""
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, "role_review")
            require_write_actor(connection, project_id, who, "workflow_manage")
            try:
                return agents.delete_agent(connection, project_id, role_key, actor=who,
                                           retire_older_versions=retire_older_versions)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/role-mcp-servers")
    def list_role_mcp(project_id: str, role_key: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    "assignments": agents.list_role_mcp(connection, project_id, role_key)}

    @router.post("/v1/role-mcp-servers", status_code=201)
    def grant_role_mcp(body: RoleMcpBody) -> dict[str, Any]:
        """Give one MCP server — all its tools, or only those named — to one agent."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor,
                                        "workflow_manage")
            try:
                return agents.grant_mcp(connection, body.project_id, actor=actor,
                                        role_key=body.role_key,
                                        mcp_server_id=body.mcp_server_id, tools=body.tools)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.delete("/v1/role-mcp-servers")
    def revoke_role_mcp(project_id: str, role_key: str, mcp_server_id: str,
                        actor: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, "workflow_manage")
            try:
                return agents.revoke_mcp(connection, project_id, actor=who,
                                         role_key=role_key, mcp_server_id=mcp_server_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    return router
