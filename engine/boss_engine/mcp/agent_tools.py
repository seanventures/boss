"""EVERY DEPLOYED AGENT IS AN MCP TOOL: `agent_<key>` (AP-P3, BOSSAPP-1054; AGENT_PLATFORM §4.0
principle 6 "Everything external is a protocol view over the same run"; D10: the current MCP
protocol, P5 adds 2026-07-28).

A client (Claude Code, Codex, any MCP client) sees one tool per agent deployed to prod with MCP
exposure. Calling it starts a run of that agent on a new thread (or the `thread_id` it names)
and answers when the run ends or stops for a person — the same run the REST API starts
(POST /v1/agents/{key}/runs/wait), executed by a worker, pinned to the deployed version, under
the caller's own identity:

  * API-backed sessions call the route with the CALLER's credential (the route's `agent:invoke`
    decides, M8: no confused deputy);
  * a native session runs it through the registry's identity gate (`permission="agent:invoke"`).

The list is read when tools are listed, so a deploy or a retirement shows on the next
tools/list.
"""
from __future__ import annotations

from typing import Any, Callable

from ..tools.registry import ToolRegistry, ToolSpec

PREFIX = "agent_"
PARAMETERS = {
    "type": "object",
    "properties": {
        "input": {"type": "string", "description": "What the agent should do."},
        "thread_id": {"type": "string",
                      "description": "Continue this thread (from an earlier answer); omit for "
                                     "a new conversation."},
        "timeout": {"type": "number", "minimum": 0, "maximum": 600,
                    "description": "Seconds to wait for the run (default 120)."},
    },
    "required": ["input"],
}


def _timeout(args: dict) -> float:
    """Seconds to wait (default 120). 0 is honoured: start the run and answer at once (the MCP
    tasks extension starts agent runs this way and the client polls the task)."""
    value = args.get("timeout")
    return 120.0 if value is None or value == "" else max(0.0, min(float(value), 600.0))


def _caller_traceparent() -> "str | None":
    """The MCP caller's trace context (mcp/server.py sets it from `_meta.traceparent`)."""
    from ..kernel.trace import CURRENT

    return CURRENT.get()


def _describe(agent: dict) -> str:
    return (f"Run the deployed BOSS agent '{agent.get('name') or agent['agent_key']}' "
            f"(v{agent.get('version')}, prod). Answers when the run ends, or with the decisions "
            "a person must approve (approve them through the Decision API).")


def _native_handler(agent_key: str) -> Callable[..., dict]:
    def handler(ctx: Any, *, project_id: str, actor: str, args: dict) -> dict:
        from ..db import begin
        from ..services import agent_protocol

        engine = getattr(ctx, "engine", None)
        if engine is None:
            return {"ok": False, "error": "durable workspace required"}
        try:
            with begin(engine) as c:
                thread_id = args.get("thread_id") or agent_protocol.create_thread(
                    c, project_id=project_id, agent_key=agent_key, actor=actor)["thread_id"]
                run = agent_protocol.start_run(c, project_id=project_id, thread_id=thread_id,
                                               actor=actor, input=args.get("input"),
                                               traceparent=_caller_traceparent())
        except agent_protocol.ProtocolError as exc:
            return {"ok": False, "error": str(exc)}
        out = agent_protocol.join(engine, project_id, run["run_id"],
                                  timeout=_timeout(args))
        return {"ok": out["status"] != "error", **out}

    return handler


def _api_handler(surface: Any, agent_key: str) -> Callable[..., dict]:
    from .api_surface import operation_alias

    alias = operation_alias("POST", "/v1/agents/{role_key}/runs/wait")

    def handler(ctx: Any, *, project_id: str, actor: str, args: dict) -> dict:
        if args.get("thread_id"):
            wait = operation_alias("POST", "/v1/threads/{thread_id}/runs/wait")
            return surface.invoke(wait, project_id=project_id,
                                  path={"thread_id": str(args["thread_id"])},
                                  body={"input": args.get("input"),
                                        "timeout": _timeout(args)})
        return surface.invoke(alias, project_id=project_id, path={"role_key": agent_key},
                              body={"input": args.get("input"),
                                    "timeout": _timeout(args)})

    return handler


def listed(*, project_id: str, engine: Any = None, surface: Any = None) -> list[dict]:
    """The deployed agents this session may see as tools (never raises: no list is no tools)."""
    try:
        if surface is not None:
            from .api_surface import operation_alias

            got = surface.invoke(operation_alias("GET", "/v1/agent-platform/mcp-tools"),
                                 project_id=project_id)
            return list(((got or {}).get("result") or {}).get("tools") or []) \
                if (got or {}).get("ok") else []
        if engine is not None:
            from ..db import begin
            from ..services import agent_protocol

            with begin(engine) as c:
                return agent_protocol.mcp_agents(c, project_id)
    except Exception:  # noqa: BLE001
        return []
    return []


def sync(registry: ToolRegistry, *, project_id: str, engine: Any = None, surface: Any = None
         ) -> set:
    """Register a tool per deployed agent and drop the ones no longer deployed. Returns the
    names registered now."""
    agents = listed(project_id=project_id, engine=engine, surface=surface)
    wanted = {str(a["tool"]): a for a in agents if str(a.get("tool") or "").startswith(PREFIX)}
    tools = getattr(registry, "_tools", {})
    for name in [n for n, spec in tools.items() if n.startswith(PREFIX)
                 and getattr(spec, "description", "").startswith("Run the deployed BOSS agent")
                 and n not in wanted]:
        tools.pop(name, None)
    for name, agent in wanted.items():
        if name in tools:
            continue
        key = str(agent["agent_key"])
        if surface is not None:
            spec = ToolSpec(name=name, description=_describe(agent), parameters=PARAMETERS,
                            handler=_api_handler(surface, key), kind="write",
                            reversibility="costs_money", requires_durable_workspace=False,
                            inherits_route_permission=True)
        else:
            spec = ToolSpec(name=name, description=_describe(agent), parameters=PARAMETERS,
                            handler=_native_handler(key), kind="write",
                            reversibility="costs_money", permission="agent:invoke")
        registry.register(spec)
    return set(wanted)


def tool_name(agent_key: str) -> str:
    return PREFIX + agent_key


__all__ = ["PARAMETERS", "PREFIX", "listed", "sync", "tool_name"]
