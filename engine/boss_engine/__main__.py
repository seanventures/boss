"""`python -m boss_engine` — run the BOSS engine standalone, no API or console.

Subcommands:
  tools                       list the engine tool layer (name, kind, description)
  status  [--project P]       quick project snapshot (what's running)
  call NAME [--args JSON]     invoke one tool directly and print its JSON result
  ask "MESSAGE"               one-shot BOSS Agent turn (read-only unless --allow-writes)
  chat                        interactive BOSS Agent session
  serve-mcp [--stdio|--http]  expose the tool layer over MCP for external agents

This is the OSS entrypoint: it constructs an EngineContext (settings + DB + the
service singletons) with no FastAPI in sight, and drives the engine through the
same tool layer the agent and MCP use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_PROJECT = os.environ.get("BOSS_PROJECT_ID", "boss-dogfood")


def _ctx():
    from .context import EngineContext

    return EngineContext.create()


def _cmd_tools(args) -> int:
    from .tools import build_default_registry

    reg = build_default_registry()
    for spec in reg.specs():
        print(f"[{spec.kind:5}] {spec.name}\n        {spec.description}")
    print(f"\n{len(reg.names())} tools "
          f"({len(reg.specs(kind='read'))} read, {len(reg.specs(kind='write'))} write)")
    return 0


def _cmd_status(args) -> int:
    from .tools import build_default_registry

    reg = build_default_registry()
    result = reg.invoke("project_status", _ctx(), project_id=args.project, actor="cli", args={})
    print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_call(args) -> int:
    from .tools import build_default_registry

    try:
        tool_args = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as exc:
        print(f"--args must be valid JSON: {exc}", file=sys.stderr)
        return 2
    reg = build_default_registry()
    result = reg.invoke(args.name, _ctx(), project_id=args.project, actor="cli", args=tool_args)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok", True) else 1


def _cmd_ask(args) -> int:
    from .agent import EngineAgent

    turn = EngineAgent(_ctx()).chat(
        project_id=args.project, actor="cli", message=args.message,
        allow_writes=args.allow_writes,
    )
    print(turn.reply)
    if turn.pending_writes:
        print("\n-- held for confirmation --", file=sys.stderr)
        print(json.dumps(turn.pending_writes, indent=2), file=sys.stderr)
    return 0


def _cmd_chat(args) -> int:
    from .agent import EngineAgent

    agent = EngineAgent(_ctx())
    history: list[dict] = []
    print("BOSS Agent (engine-native). Type 'exit' to quit. "
          f"Writes are {'ENABLED' if args.allow_writes else 'confirmation-gated'}.")
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if message.lower() in ("exit", "quit"):
            return 0
        if not message:
            continue
        turn = agent.chat(
            project_id=args.project, actor="cli", message=message,
            allow_writes=args.allow_writes, history=history,
        )
        print(f"boss> {turn.reply}")
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": turn.reply})
        # Confirmation gate: the agent proposes writes but doesn't run them unless
        # allow_writes is on. Here the y/N prompt IS the authorization — on 'yes'
        # we execute the exact proposed tool call and feed the result back.
        for w in (turn.pending_writes or []):
            preview = json.dumps(w.get("args") or {})
            if len(preview) > 140:
                preview = preview[:140] + "…"
            ans = input(f"  ⏵ run write '{w['tool']}' {preview} ? [y/N] ").strip().lower()
            if ans in ("y", "yes"):
                result = agent.registry.invoke(
                    w["tool"], agent.ctx,
                    project_id=args.project, actor="cli", args=w.get("args") or {},
                )
                print(f"  ✓ {w['tool']} → {json.dumps(result, default=str)[:400]}")
                history.append({
                    "role": "user",
                    "content": f"[confirmed] {w['tool']} was executed; result: "
                               f"{json.dumps(result, default=str)[:600]}",
                })
            else:
                print("  ✗ skipped")
    return 0


def _cmd_serve_mcp(args) -> int:
    from .mcp import MCPServer

    server = MCPServer(_ctx(), project_id=args.project, read_only=args.read_only)
    if args.http:
        import uvicorn  # lazy: only for HTTP transport

        uvicorn.run(server.asgi_app(), host=args.host, port=args.port)
        return 0
    # default: stdio (the standard MCP transport)
    server.serve_stdio()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="boss_engine", description="Run the BOSS engine standalone.")
    p.add_argument("--project", default=DEFAULT_PROJECT, help=f"project id (default {DEFAULT_PROJECT})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help="list the engine tool layer").set_defaults(func=_cmd_tools)
    sub.add_parser("status", help="project snapshot").set_defaults(func=_cmd_status)

    c = sub.add_parser("call", help="invoke one tool")
    c.add_argument("name")
    c.add_argument("--args", help="tool arguments as JSON", default="")
    c.set_defaults(func=_cmd_call)

    a = sub.add_parser("ask", help="one-shot agent turn")
    a.add_argument("message")
    a.add_argument("--allow-writes", action="store_true")
    a.set_defaults(func=_cmd_ask)

    ch = sub.add_parser("chat", help="interactive agent session")
    ch.add_argument("--allow-writes", action="store_true")
    ch.set_defaults(func=_cmd_chat)

    m = sub.add_parser("serve-mcp", help="expose tools over MCP")
    m.add_argument("--http", action="store_true", help="HTTP transport (default: stdio)")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8791)
    m.add_argument("--read-only", action="store_true", help="expose read tools only")
    m.set_defaults(func=_cmd_serve_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
