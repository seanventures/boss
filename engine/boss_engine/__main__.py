"""`python -m boss_engine` — run the BOSS engine standalone, no API or console.

Subcommands:
  bootstrap                   create the schema + seed defaults in DATABASE_URL
  tools                       list the engine tool layer (name, kind, description)
  status  [--project P]       quick project snapshot (what's running)
  call NAME [--args JSON] [--confirm]  invoke a tool; confirm destructive actions
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
import time

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
    ctx = _ctx()
    if args.name in {"query_db", "write_db"}:
        ctx.raw_sql_admin_authorized = True
    result = reg.invoke(
        args.name, ctx, project_id=args.project, actor="cli", args=tool_args,
        operator_confirmed=bool(getattr(args, "confirm", False)),
    )
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
    """HTTP defaults to read-only unless ``--allow-writes`` is set.
    Stdio stays write-capable by default. Bearer auth is a separate guard."""
    if args.read_only:
        return True
    if args.http and not args.allow_writes:
        return True
    return False


def _cmd_serve_mcp(args) -> int:
    from .mcp import MCPServer

    ctx = _ctx()
    # The server hides native writes without a local Postgres workspace. A
    # separately authenticated API bridge may still write through the API's
    # own durable control plane and policy gates.
    read_only = _serve_mcp_read_only(args)
    http_token = os.environ.get("BOSS_MCP_HTTP_TOKEN") if args.http else None
    if args.http and args.host not in ("127.0.0.1", "localhost", "::1") and not http_token:
        print(
            "boss-engine: non-loopback HTTP MCP requires BOSS_MCP_HTTP_TOKEN; "
            "read tools can expose project data.",
            file=sys.stderr,
        )
        return 2
    if args.http and not read_only and not http_token:
        print(
            "boss-engine: --allow-writes requires BOSS_MCP_HTTP_TOKEN "
            "for HTTP MCP authentication.",
            file=sys.stderr,
        )
        return 2

    api_surface = None
    api_url = args.api_url or os.environ.get("BOSS_MCP_API_URL", "")
    if api_url:
        from .mcp.api_surface import ApiSurface

        try:
            # Deliberately never fall back to BOSS_API_KEY: the API container's
            # ambient owner credential is not authority for an MCP client.
            api_surface = ApiSurface.connect(
                api_url, api_key=os.environ.get("BOSS_MCP_API_KEY"),
                project_id=args.project,
            )
        except Exception:
            print("boss-engine: MCP API catalog unavailable; check API origin and auth",
                  file=sys.stderr)
            return 2
        if os.environ.get("BOSS_MCP_API_KEY") and not api_surface.principal:
            print(f"boss-engine: MCP API credential is not bound to the selected project "
                  f"({api_surface.scope_reason or 'unverified'})", file=sys.stderr)
            return 2
    elif args.api_domain:
        print("boss-engine: --api-domain requires --api-url or BOSS_MCP_API_URL",
              file=sys.stderr)
        return 2
    # WHO THE STDIO SESSION IS: the BOSS API key it was launched with (BOSS_MCP_API_KEY, a
    # boss_pat_/boss_sak_ key), verified by identity on EVERY tool call, so a revoked key stops at
    # once. Without one, tool calls carry no identity and are refused under BOSS_AUTHZ_MODE=enforce.
    # Over HTTP each request authenticates its own bearer instead (MCPServer.asgi_app).
    identity = None
    session_key = (os.environ.get("BOSS_MCP_API_KEY") or "").strip()
    if not args.http and session_key:
        from .mcp.server import authenticate_transport

        def identity():  # noqa: E306 — re-verified per call, never cached
            return authenticate_transport({"authorization": f"Bearer {session_key}"})
    server = MCPServer(
        ctx, project_id=args.project, read_only=read_only,
        http_token=http_token, api_surface=api_surface,
        api_domains=set(args.api_domain), identity=identity,
    )
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


#: Terminal states a code-task run can reach. Anything else means still working.
_CODE_TERMINAL = {"done", "failed", "cancelled", "blocked", "completed_no_pr", "inconclusive"}
#: Which of those the shell should treat as success.
_CODE_OK = {"done", "completed_no_pr"}


def _emit(stream: bool, payload: dict) -> None:
    """One JSON object per line, flushed.

    Line-delimited rather than a JSON array so a consumer can act on the first event without
    waiting for the run to end — which is the entire point of a stream.
    """
    if not stream:
        return
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _cmd_code(args) -> int:
    """Start a governed code task and, unless told otherwise, follow it to a verdict.

    This is the headless entry point. Factory ships `droid exec -o json|stream-json` with
    real exit codes and it is how their work reaches CI; BOSS had no equivalent, so running
    a code task from a pipeline meant `call create_code_task` plus hand-rolled polling.

    Governed by design: this goes through create_code_task, so the sandbox, the gates, the
    receipts and the budget reservation all apply. There is deliberately no ungoverned
    fast path here — an ungoverned BOSS is just a worse coding agent.
    """
    from .tools import build_default_registry

    out = args.output
    stream = out == "stream-json"
    reg = build_default_registry()
    ctx = _ctx()

    payload = {"title": args.title or args.instructions[:80],
               "instructions": args.instructions}
    if args.repo:
        payload["repository"] = args.repo
    if args.base_branch:
        payload["base_branch"] = args.base_branch
    if args.profile:
        payload["profile"] = args.profile
    if args.criteria:
        payload["acceptance_criteria"] = list(args.criteria)
    if args.test_command:
        payload["test_commands"] = list(args.test_command)

    started = reg.invoke("create_code_task", ctx, project_id=args.project,
                         actor="cli", args=payload)
    if not started.get("ok", True):
        if out == "text":
            print(f"could not start: {started.get('error')}", file=sys.stderr)
        else:
            print(json.dumps(started, default=str))
        return 1

    run_id = (started.get("run_id") or started.get("run", {}).get("id")
              or started.get("code_task", {}).get("run_id"))
    _emit(stream, {"event": "started", "run_id": run_id, "result": started})
    if out == "text":
        print(f"started {run_id}")
    if not args.wait:
        if out == "json":
            print(json.dumps(started, indent=2, default=str))
        return 0

    deadline = time.monotonic() + args.timeout
    last_phase = None
    run: dict = {}
    while time.monotonic() < deadline:
        got = reg.invoke("get_code_task", ctx, project_id=args.project, actor="cli",
                         args={"run_id": run_id})
        run = got.get("run") or {}
        phase, state = run.get("phase"), run.get("status") or run.get("state")
        if isinstance(state, dict):
            state = state.get("status") or state.get("state")
        if phase != last_phase:
            last_phase = phase
            _emit(stream, {"event": "phase", "run_id": run_id, "phase": phase,
                           "status": state})
            if out == "text":
                print(f"  {phase}")
        if str(state or "").lower() in _CODE_TERMINAL:
            break
        time.sleep(args.poll_interval)
    else:
        _emit(stream, {"event": "timeout", "run_id": run_id, "after_seconds": args.timeout})
        if out == "text":
            print(f"timed out after {args.timeout}s (run {run_id} is still going)",
                  file=sys.stderr)
        elif out == "json":
            print(json.dumps({"run_id": run_id, "timed_out": True, "run": run},
                             indent=2, default=str))
        return 1

    status = str(run.get("status") or "").lower()
    _emit(stream, {"event": "finished", "run_id": run_id, "status": status, "run": run})
    if out == "json":
        print(json.dumps({"run_id": run_id, "status": status, "run": run},
                         indent=2, default=str))
    elif out == "text":
        print(f"{status} ({run_id})")
    return 0 if status in _CODE_OK else 1


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
    c.add_argument("--confirm", action="store_true",
                   help="confirm a destructive local CLI action")
    c.set_defaults(func=_cmd_call)

    a = sub.add_parser("ask", help="one-shot agent turn")
    a.add_argument("message")
    a.add_argument("--allow-writes", action="store_true")
    a.set_defaults(func=_cmd_ask)

    ch = sub.add_parser("chat", help="interactive agent session")
    ch.add_argument("--allow-writes", action="store_true")
    ch.set_defaults(func=_cmd_chat)

    cd = sub.add_parser(
        "code", help="run a governed code task headlessly (CI-friendly)")
    cd.add_argument("instructions", help="what to build or change")
    cd.add_argument("--title", default="", help="short title (default: the instructions)")
    cd.add_argument("--repo", default="", help="registered repo: owner/name, name, or binding id")
    cd.add_argument("--base-branch", default="", help="branch to build on (default main)")
    cd.add_argument("--profile", default="",
                    choices=["", "lean", "balanced", "strict", "vag"],
                    help="cost/rigor dial")
    cd.add_argument("--criteria", action="append", default=[],
                    help="acceptance criterion (repeatable)")
    cd.add_argument("--test-command", action="append", default=[],
                    help="verification command BOSS re-runs itself (repeatable)")
    cd.add_argument("-o", "--output", default="text",
                    choices=["text", "json", "stream-json"],
                    help="stream-json emits one JSON object per line as the run progresses")
    cd.add_argument("--no-wait", dest="wait", action="store_false", default=True,
                    help="start the task and exit without following it")
    cd.add_argument("--timeout", type=int, default=3600,
                    help="seconds to follow the run before giving up (default 3600)")
    cd.add_argument("--poll-interval", type=float, default=5.0)
    cd.set_defaults(func=_cmd_code)

    m = sub.add_parser("serve-mcp", help="expose tools over MCP")
    m.add_argument("--http", action="store_true", help="HTTP transport (default: stdio)")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8791)
    m.add_argument("--read-only", action="store_true", help="expose read tools only")
    m.add_argument(
        "--api-url", default="",
        help="mounted BOSS API origin for operation discovery (or BOSS_MCP_API_URL)",
    )
    m.add_argument(
        "--api-domain", action="append", default=[],
        help="advertise typed API tools from one domain (repeat, or use all); catalog works without this",
    )
    m.add_argument(
        "--allow-writes",
        action="store_true",
        help="HTTP only: expose write tools; requires BOSS_MCP_HTTP_TOKEN",
    )
    m.set_defaults(func=_cmd_serve_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
