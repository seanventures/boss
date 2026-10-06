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

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
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
    #: How it speaks (agent_persona; linted, never widens what it may do).
    persona: Optional[dict[str, Any]] = None
    #: {kind: blank|template|archetype, template_key?|archetype?} — what it starts from.
    born_from: Optional[dict[str, Any]] = None
    #: Which team it sits in (a shipped key or the operator's own name); empty = Yours.
    team: Optional[str] = None


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
    persona: Optional[dict[str, Any]] = None
    #: Organization's `Move to…`: a shipped team key, the operator's own team, or null (Yours).
    team: Optional[str] = None
    # IDENTITY FIELDS ARE FACTS, NOT SETTINGS. Accepted here only so a body carrying one is
    # refused by name (422 "Identity fields are fixed") instead of being silently ignored.
    agent_id: Optional[Any] = None
    born_by: Optional[Any] = None
    born_from: Optional[Any] = None
    sponsor: Optional[Any] = None
    lifecycle: Optional[Any] = None
    identity_version: Optional[Any] = None
    fingerprint: Optional[Any] = None


class InstructionAddBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    name: Any = None
    body: Any = None
    source: str = "pasted"
    modes: Optional[list[str]] = None
    dry_run: bool = False


class InstructionUpdateBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    name: Any = None
    body: Any = None
    modes: Optional[list[str]] = None
    position: Optional[int] = None
    dry_run: bool = False


class SkillFromFileBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    filename: str = ""
    text: Any = None
    label: Optional[str] = None
    summary: Optional[str] = None
    grant_to: Optional[str] = None


#: The prompt drawer's words for each layer (agent-loadout spec §3.3, "See its prompt").
PROMPT_LABELS = {"L0": "Rules", "L1": "Identity", "L2": "Remit", "L2i": "Instructions",
                 "L3": "Voice", "L4": "Memory", "L5": "Situation"}


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
    actor_can: Optional[Callable[..., bool]] = None,
) -> APIRouter:
    from boss_engine.services import agents

    router = APIRouter(tags=["agents"])

    def _fail(error: Exception):
        from boss_engine.services.agent_identity import IdentityError

        if isinstance(error, agents.PersonaInvalid):
            # The field travels with the sentence, so the editor can put it on its control.
            raise HTTPException(status_code=422, detail={"field": error.field,
                                                         "message": str(error)}) from error
        if isinstance(error, (agents.AgentError, IdentityError)):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        raise error

    @router.get("/v1/agents")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_agents(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    "items": agents.list_agents(connection, project_id)}

    @router.get("/v1/agents/grantable")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
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
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def get_agent(role_key: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agents.get_agent(connection, project_id, role_key)
            except Exception as error:  # noqa: BLE001 — typed mapping in _fail
                _fail(error)

    @router.post("/v1/agents", status_code=201)
    @permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
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
                    boundaries=body.boundaries, persona=body.persona,
                    born_from=body.born_from, team=body.team)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.patch("/v1/agents/{role_key}")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def update_agent(role_key: str, body: AgentUpdateBody) -> dict[str, Any]:
        from boss_engine.services.agent_identity import IDENTITY_FIELDS, IDENTITY_FIXED

        given = body.model_fields_set
        fixed = sorted(set(IDENTITY_FIELDS) & given)
        if fixed:
            raise HTTPException(status_code=422, detail={"field": fixed[0],
                                                         "message": IDENTITY_FIXED})
        changes: dict[str, Any] = {
            name: getattr(body, name)
            for name in ("display_name", "mission", "boundaries", "reports_to", "skills",
                         "mcp_servers", "persona", "team")
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
    @permit(P.AGENT_LIFECYCLE, scope=Scope.project(query="project_id"))
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

    # ── Instructions (agent-loadout spec §4.7) ─────────────────────────────────────────────
    # Writes are a PERSON's (agent_instructions.ONLY_A_PERSON) with role_review, the permission
    # a voice edit needs; the actor is the signed-in principal, as for PATCH /v1/agents.

    def _ins_fail(error: Exception, *, dry_run: bool = False):
        """A refusal. A DRY RUN's refusal is its answer, not an error: 200 {ok: false, …} — the
        sheet asks on every keystroke and a 422 each time is a console full of red."""
        from fastapi.responses import JSONResponse

        from boss_engine.services.agent_instructions import InstructionError

        if dry_run and isinstance(error, InstructionError) and error.status == 422:
            return JSONResponse({"ok": False, **error.detail()}, status_code=200)
        if isinstance(error, InstructionError):
            raise HTTPException(status_code=error.status, detail=error.detail()) from error
        raise error

    @router.get("/v1/agents/{role_key}/instructions")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_instructions(role_key: str, project_id: str,
                          actor: Optional[str] = None) -> dict[str, Any]:
        """The documents, the per-mode budget and `can_edit` — whether this viewer may write
        them (role_review), so a reader sees the rows and the preview, and no Add."""
        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            require_reader(connection, project_id)
            out = agent_instructions.listing(connection, project_id, role_key)
            out["can_edit"] = bool(actor_can and actor_can(connection, project_id, actor,
                                                           "role_review"))
            return out

    @router.get("/v1/agents/{role_key}/instructions/{ins_id}")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def get_instruction(role_key: str, ins_id: str, project_id: str) -> dict[str, Any]:
        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_instructions.one(connection, project_id, role_key, ins_id)
            except Exception as error:  # noqa: BLE001
                _ins_fail(error)

    @router.post("/v1/agents/{role_key}/instructions", status_code=201)
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def add_instruction(role_key: str, body: InstructionAddBody):
        from fastapi.responses import JSONResponse

        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "role_review")
            try:
                out = agent_instructions.add(
                    connection, body.project_id, role_key, actor=actor, name=body.name,
                    body=body.body, source=body.source, modes=body.modes, dry_run=body.dry_run)
            except Exception as error:  # noqa: BLE001
                return _ins_fail(error, dry_run=body.dry_run)
        return JSONResponse(out, status_code=200 if body.dry_run else 201)

    @router.patch("/v1/agents/{role_key}/instructions/{ins_id}")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def update_instruction(role_key: str, ins_id: str, body: InstructionUpdateBody):
        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "role_review")
            try:
                return agent_instructions.update(
                    connection, body.project_id, role_key, ins_id, actor=actor, body=body.body,
                    name=body.name, modes=body.modes, position=body.position,
                    dry_run=body.dry_run)
            except Exception as error:  # noqa: BLE001
                return _ins_fail(error, dry_run=body.dry_run)

    @router.delete("/v1/agents/{role_key}/instructions/{ins_id}")
    @permit(P.AGENT_UPDATE, scope=Scope.project(query="project_id"))
    def remove_instruction(role_key: str, ins_id: str, project_id: str,
                           actor: Optional[str] = None) -> dict[str, Any]:
        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, "role_review")
            try:
                return agent_instructions.remove(connection, project_id, role_key, ins_id,
                                                 actor=who)
            except Exception as error:  # noqa: BLE001
                _ins_fail(error)

    @router.get("/v1/agents/{role_key}/versions/{version}/instructions")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def instructions_at_version(role_key: str, version: int, project_id: str) -> dict[str, Any]:
        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            require_reader(connection, project_id)
            return agent_instructions.at_version(connection, project_id, role_key, version)

    @router.get("/v1/agents/{role_key}/prompt")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def agent_prompt(role_key: str, project_id: str, mode: str = "work",
                     actor: Optional[str] = None) -> dict[str, Any]:
        """`See its prompt`: the composed prompt by layer, without a task. It shows remit text,
        so it needs role_review rather than plain reading."""
        from sqlalchemy import text as sql_text

        from boss_engine.services.agent_instructions import for_prompt
        from boss_engine.services.agent_persona import compose_agent_prompt

        if mode not in ("work", "chat", "panel"):
            raise HTTPException(status_code=422, detail={"field": "mode",
                                                         "message": "mode is work, chat or panel"})
        with begin(engine) as connection:
            require_write_actor(connection, project_id, actor, "role_review")
            row = connection.execute(
                sql_text("SELECT role_key, display_name, spec, agent_id, identity_version "
                         "FROM roles WHERE project_id = :p AND role_key = :r"),
                {"p": project_id, "r": role_key.strip()},
            ).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"no agent {role_key!r} on this "
                                                            "project.")
            blocks = compose_agent_prompt(
                dict(row), mode=mode,
                instructions=for_prompt(connection, project_id, row["role_key"], mode))
        return {"role_key": row["role_key"], "mode": mode,
                "blocks": [{"layer": b["layer"], "label": PROMPT_LABELS.get(b["layer"], b["layer"]),
                            "text": b["text"]} for b in blocks if b.get("text")]}

    @router.post("/v1/skills/from-file", status_code=201)
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def skill_from_file(body: SkillFromFileBody) -> dict[str, Any]:
        """A SKILL.md-style file → a `guide` skill, given to `grant_to` in the same step."""
        from boss_engine.services import agent_instructions

        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "role_review")
            if body.grant_to:
                # Granting is the same permission everywhere it happens.
                require_write_actor(connection, body.project_id, actor, "workflow_manage")
            try:
                return agent_instructions.skill_from_file(
                    connection, body.project_id, actor=actor, filename=body.filename,
                    text_=body.text, label=body.label, summary=body.summary,
                    grant_to=body.grant_to)
            except Exception as error:  # noqa: BLE001
                _ins_fail(error)

    @router.get("/v1/role-mcp-servers")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_role_mcp(project_id: str, role_key: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    "assignments": agents.list_role_mcp(connection, project_id, role_key)}

    @router.post("/v1/role-mcp-servers", status_code=201)
    @permit(P.AGENT_ABILITY_ASSIGN, scope=Scope.project(body="project_id"))
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
    @permit(P.AGENT_ABILITY_ASSIGN, scope=Scope.project(query="project_id"))
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
