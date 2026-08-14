"""`python -m boss_engine` — run the BOSS engine standalone, no API or console.

Subcommands:
  bootstrap                   create the schema + seed defaults in DATABASE_URL
  tools                       list the engine tool layer (name, kind, description)
  status  [--project P]       quick project snapshot (what's running)
  call NAME [--args JSON]     invoke one tool directly and print its JSON result
  ask "MESSAGE"               one-shot BOSS Agent turn (read-only unless --allow-writes)
  chat                        interactive BOSS Agent session
  serve-mcp [--stdio|--http]  expose the tool layer over MCP for external agents

This is the Community/engine entrypoint: it constructs an EngineContext
(settings + DB + the service singletons) with no FastAPI in sight, and drives
the engine through the same tool layer the agent and MCP use.

Private design-partner onboarding (monorepo): docs/operations/DESIGN_PARTNER_ONBOARDING.md
Engine-only pack: engine/DESIGN_PARTNER_ONBOARDING.md. Not a public-release flip.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_PROJECT = os.environ.get("BOSS_PROJECT_ID", "boss-dogfood")


def _postgres_ports(ctx):
    """Postgres-backed ports for a CLI/MCP process, mirroring what the API builds at startup.

    Falls back to the OSS defaults for a community edition, which is exactly what
    apps/api/app/bootstrap_ports.build_ports does — the two must agree, or the same tool
    behaves differently depending on which process invoked it.
    """
    from .ports import EnginePorts

    edition = str(getattr(ctx.settings, "boss_edition", "community")).strip().lower()
    if edition not in ("enterprise", "enterprise_plus", "ent"):
        return EnginePorts.oss_defaults(ctx.settings)
    try:
        from apps.api.app.adapters_postgres import (
            PostgresBudgetPort, PostgresDecisionSink, PostgresEntitlementPort,
            PostgresEventSink, PostgresPersistence,
        )
    except Exception:  # noqa: BLE001 — the engine must not hard-depend on the api package
        return EnginePorts.oss_defaults(ctx.settings)
    return EnginePorts(
        budget=PostgresBudgetPort(ctx.settings),
        entitlement=PostgresEntitlementPort(ctx.settings),
        persistence=PostgresPersistence(ctx.engine),
        events=PostgresEventSink(),
        decisions=PostgresDecisionSink(),
    )


def _ctx():
    from .context import EngineContext

    ctx = EngineContext.create()
    # Standalone composition: the CLI is an application boundary, so when no
    # DATABASE_URL is configured we deliberately install the OSS default ports
    # (in-memory persistence, no-op budget, allow-all entitlements) instead of
    # letting port consumers pick them up as a silent per-service fallback.
    # Ports must be configured on BOTH paths. This guard previously fired only when
    # DATABASE_URL was ABSENT, so the real path — an MCP server against Postgres — ran with
    # no ports at all, and every tool routing through code_task_service died with
    # "engine ports are not configured". That is why extend_code_task and retry_code_task
    # had never worked from MCP: they were reachable, listed, and structurally unable to run.
    from .ports import EnginePorts, configure_engine_ports, get_engine_ports

    if get_engine_ports() is None:
        if os.environ.get("DATABASE_URL"):
            configure_engine_ports(_postgres_ports(ctx))
        else:
            configure_engine_ports(EnginePorts.oss_defaults(ctx.settings))
    if not os.environ.get("DATABASE_URL"):
        print(
            "boss-engine: DATABASE_URL is not set — running in catalog-only mode. "
            "BOSS workflow commands require a self-hosted Postgres workspace so "
            "tasks, Receipts, approvals, and leases are durable. Set DATABASE_URL "
            "and run `python -m boss_engine bootstrap` before invoking workflow tools.",
            file=sys.stderr,
        )
    elif not ctx.database_reachable():
        print(
            "boss-engine: warning — database is unreachable; DB-backed tools will "
            "fail. Check DATABASE_URL and run `python -m boss_engine bootstrap` "
            "against a fresh database (see engine/README.md).",
            file=sys.stderr,
        )
    return ctx


def _cmd_bootstrap(args) -> int:
    from .bootstrap import main as bootstrap_main

    bootstrap_main()
    return 0


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


def _serve_mcp_read_only(args) -> bool:
    """HTTP has no transport auth, so it defaults to read-only unless
    ``--allow-writes`` is set. Stdio stays write-capable by default."""
    if args.read_only:
        return True
    if args.http and not args.allow_writes:
        return True
    return False


def _cmd_serve_mcp(args) -> int:
    from .mcp import MCPServer

    ctx = _ctx()
    # A no-database process is intentionally an inspection/catalog endpoint;
    # advertising write tools there would invite a caller to take an action
    # that cannot create the required durable audit trail.
    read_only = _serve_mcp_read_only(args) or not ctx.durable_workspace_configured
    if (
        args.http
        and args.allow_writes
        and not read_only
        and args.host not in ("127.0.0.1", "localhost", "::1")
    ):
        print(
            "boss-engine: WARNING — serving WRITE tools over unauthenticated "
            f"HTTP on {args.host}:{args.port}. Prefer stdio MCP, or bind "
            "loopback, or pass --read-only.",
            file=sys.stderr,
        )

    server = MCPServer(ctx, project_id=args.project, read_only=read_only)
    if args.http:
        import uvicorn  # lazy: only for HTTP transport

        mode = "read-only" if read_only else "writes ENABLED"
        print(f"Mission Control ({mode}): http://{args.host}:{args.port}/",
              file=sys.stderr)
        uvicorn.run(server.asgi_app(), host=args.host, port=args.port)
        return 0
    # default: stdio (the standard MCP transport)
    server.serve_stdio()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="boss_engine", description="Run the BOSS engine standalone.")
    p.add_argument("--project", default=DEFAULT_PROJECT, help=f"project id (default {DEFAULT_PROJECT})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "bootstrap", help="create the schema + seed defaults in DATABASE_URL"
    ).set_defaults(func=_cmd_bootstrap)
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
    m.add_argument(
        "--allow-writes",
        action="store_true",
        help="HTTP only: expose write tools (off by default; HTTP has no auth)",
    )
    m.set_defaults(func=_cmd_serve_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
