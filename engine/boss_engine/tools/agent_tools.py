"""Giving an agent a skill or an MCP server, by asking BOSS.

An operator says "give the editor the trim skill" or "let the publisher use our Mattermost
server". BOSS could author a skill (draft_skill) and place it in a step (draft_custom_step) but
could not hand one to an agent — the one act Agent Studio exists for — so the conversation
ended in "open the agent and press Give this agent a skill". These write through the same
functions the console and POST /v1/role-skills | /v1/role-mcp-servers use
(services/agents.py), so the chat, the API and the page cannot disagree about what a grant is.

Both are reversible and spend nothing: a grant adds a row, and taking it away (from the agent's
page, or the same tool with `remove: true`) deletes it and records the removal so no seed puts
it back.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from .._compat import services_module
from .registry import Exposure, ToolRegistry


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
        reversibility="safe", permission="agent_ability:assign",
        exposure=Exposure(tags=("skills", "agents")))
    def assign_skill_to_agent(ctx, *, project_id, actor, args) -> dict[str, Any]:
        skills = services_module("skill_definitions")
        agents = services_module("agents")
        pid = args.get("project_id") or project_id
        role = str(args.get("agent") or "").strip().lower()
        skill = str(args.get("skill") or "").strip()
        with ctx.begin() as c:
            try:
                if args.get("remove"):
                    return skills.unassign_skill_from_role(
                        c, project_id=pid, actor=actor, role_key=role, skill_id=skill)
                # A SKILL THAT CALLS AN MCP SERVER IS AN MCP GRANT. Review of
                # milestone/integration: this tool is `safe`, so the model ran it itself, and
                # an `mcp_tool` skill handed the agent a server that assign_mcp_to_agent (which
                # only an operator's click runs) had been made to protect. When the agent does
                # not already hold the server, nothing is written: the model is told to OFFER
                # the server grant, which carries this skill with it on the same click.
                known = c.execute(text("SELECT 1 FROM roles WHERE project_id = :p "
                                       "AND role_key = :r"), {"p": pid, "r": role}).first()
                gap = agents.mcp_reach_gap(c, pid, role, skill) if known else None
                if gap is not None:
                    # WIDEN, NEVER NARROW: an agent that holds the server for other tools keeps
                    # them (a grant's tool list is replaced, not merged, by grant_mcp).
                    held = c.execute(text(
                        "SELECT tools FROM role_mcp_servers WHERE project_id = :p "
                        "AND role_key = :r AND mcp_server_id = :s"),
                        {"p": pid, "r": role, "s": gap["mcp_server_id"]}).scalar()
                    tools = sorted({*(held or []), gap["tool"]} - {""})
                    proposed = {"agent": role, "server": gap["mcp_server_id"],
                                "tools": tools, "skill": skill}
                    return {
                        "ok": False, "status": "confirmation_required",
                        "requires_confirmation": True,
                        "error": "the skill reaches an MCP server the agent does not hold",
                        "reversibility": "grants_authority",
                        "message": (
                            f"{skill} calls '{gap['tool']}' on the MCP server '{gap['server']}', "
                            f"which {role} does not hold. Giving it this skill gives it that "
                            "server's reach on somebody's account, so a person decides. Nothing "
                            "was assigned."),
                        "do_this": (
                            "call assign_mcp_to_agent with these exact args. It is offered to "
                            "the operator as a button; one click gives the agent the server "
                            "(this tool only) and the skill together."),
                        "proposed_tool": "assign_mcp_to_agent",
                        "proposed_args": proposed,
                    }
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
            "skill": {"type": "string", "description": (
                "also give the agent this skill, which calls a tool on the server — what "
                "assign_skill_to_agent asks for when a skill needs the server too")},
            "project_id": {"type": "string"},
        }, ["agent", "server"]),
        kind="write",
        # OFFERED, NOT PRESSED. An MCP server carries its credentials and every action it
        # exposes, so granting one hands an agent reach on somebody's account. Shipped as `safe`
        # at first, which let the chat model give any agent any registered server on its own;
        # now the model proposes it and an operator's click runs it. Taking a server back goes
        # through the same button, which costs one click and keeps the tool one tool.
        reversibility="grants_authority", permission="agent_ability:assign",
        exposure=Exposure(tags=("mcp", "agents")))
    def assign_mcp_to_agent(ctx, *, project_id, actor, args) -> dict[str, Any]:
        agents = services_module("agents")
        pid = args.get("project_id") or project_id
        role = str(args.get("agent") or "").strip().lower()
        server = str(args.get("server") or "").strip()
        skills = services_module("skill_definitions")
        skill = str(args.get("skill") or "").strip()
        try:
            with ctx.begin() as c:
                if args.get("remove"):
                    return agents.revoke_mcp(c, pid, actor=actor, role_key=role,
                                             mcp_server_id=server)
                granted = agents.grant_mcp(c, pid, actor=actor, role_key=role,
                                           mcp_server_id=server,
                                           tools=list(args.get("tools") or []))
                if skill:
                    # The same click carries the skill that needed the server, so the
                    # operator's one decision leaves the agent able to do what was asked. A
                    # refused skill raises, so the transaction takes the server grant back too.
                    granted["skill"] = skills.assign_skill_to_role(
                        c, project_id=pid, actor=actor, role_key=role, skill_id=skill)
                return granted
        except (agents.AgentError, skills.SkillDefinitionError) as error:
            return {"ok": False, "error": str(error)}
