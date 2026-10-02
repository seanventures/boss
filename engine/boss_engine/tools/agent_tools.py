"""Giving an agent a skill or an MCP server, by asking BOSS.

An operator says "give the editor the trim skill" or "let the publisher use our Mattermost
server". BOSS could author a skill (draft_skill) and place it in a step (draft_custom_step) but
could not hand one to an agent — the one act the Agents page exists for — so the conversation
ended in "open the agent and press Give this agent a skill". These write through the same
functions the console and POST /v1/role-skills | /v1/role-mcp-servers use
(services/agents.py), so the chat, the API and the page cannot disagree about what a grant is.

Both are reversible and spend nothing: a grant adds a row, and taking it away (from the agent's
page, or the same tool with `remove: true`) deletes it and records the removal so no seed puts
it back.
"""
from __future__ import annotations

from typing import Any

from .._compat import services_module
from .registry import ToolRegistry


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def register_agent_tools(reg: ToolRegistry) -> None:
    @reg.tool(
        "assign_skill_to_agent",
        "Give one skill to one agent (or take it away with remove=true). Holding a skill is "
        "what lets an agent use it in its own work and makes it a candidate when a workflow "
        "step leaves the agent to BOSS. Use list_skills for skill ids; the agent is its role "
        "key (e.g. 'video_editor'). The answer names what exists when either is wrong.",
        _obj({
            "agent": {"type": "string", "description": "the agent's role key"},
            "skill": {"type": "string", "description": "the skill id to give"},
            "remove": {"type": "boolean", "description": "take it away instead"},
            "project_id": {"type": "string"},
        }, ["agent", "skill"]),
        kind="write",
        reversibility="safe",
    )
    def assign_skill_to_agent(ctx, *, project_id, actor, args) -> dict[str, Any]:
        skills = services_module("skill_definitions")
        pid = args.get("project_id") or project_id
        role = str(args.get("agent") or "").strip().lower()
        skill = str(args.get("skill") or "").strip()
        with ctx.begin() as c:
            try:
                if args.get("remove"):
                    return skills.unassign_skill_from_role(
                        c, project_id=pid, actor=actor, role_key=role, skill_id=skill)
                return skills.assign_skill_to_role(
                    c, project_id=pid, actor=actor, role_key=role, skill_id=skill)
            except skills.SkillDefinitionError as error:
                # THE MESSAGE IS THE REPAIR: it names the agents or skills that do exist.
                return {"ok": False, "error": str(error)}

    @reg.tool(
        "assign_mcp_to_agent",
        "Give one registered MCP server to one agent — every tool it offers, or only the tools "
        "named — or take it away with remove=true. A workflow step the agent works then offers "
        "exactly those tools; the server's risk tier still decides when a call waits for a "
        "person. The server may be named by id or by its unique name.",
        _obj({
            "agent": {"type": "string", "description": "the agent's role key"},
            "server": {"type": "string", "description": "MCP server id or unique name"},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "only these tools; leave out for all of them"},
            "remove": {"type": "boolean", "description": "take it away instead"},
            "project_id": {"type": "string"},
        }, ["agent", "server"]),
        kind="write",
        reversibility="safe",
    )
    def assign_mcp_to_agent(ctx, *, project_id, actor, args) -> dict[str, Any]:
        agents = services_module("agents")
        pid = args.get("project_id") or project_id
        role = str(args.get("agent") or "").strip().lower()
        server = str(args.get("server") or "").strip()
        with ctx.begin() as c:
            try:
                if args.get("remove"):
                    return agents.revoke_mcp(c, pid, actor=actor, role_key=role,
                                             mcp_server_id=server)
                return agents.grant_mcp(c, pid, actor=actor, role_key=role,
                                        mcp_server_id=server,
                                        tools=list(args.get("tools") or []))
            except agents.AgentError as error:
                return {"ok": False, "error": str(error)}
