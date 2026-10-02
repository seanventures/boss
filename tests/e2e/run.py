"""The one entry point for the end-to-end harness. scripts/e2e.sh calls this.

  up          boot a disposable stack (Postgres in Docker, real API + worker + console, stubs)
  web         add the console to a stack started with --no-web
  down        stop it and remove its database
  status      print where the running stack is
  workflows   drive every catalogue template and opt-in example to succeeded or its gate
  console     sign in and walk every console area and primary action in a browser
  all         up, workflows, console, down — the default; exits non-zero on any failure

  --target live   point at an existing install (boss-1) instead; see tests/e2e/README.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from e2e.harness.stack import LocalStack, StackState, stack_info, stop_state  # noqa: E402
from e2e.harness.target import Target, TargetError, live_target_from_env  # noqa: E402


def _target(args: argparse.Namespace) -> Target:
    if args.target == "live":
        return live_target_from_env()
    return StackState.load().target()


def _report_dir(args: argparse.Namespace, target: Target) -> Path:
    if target.kind == "local":
        return Path(StackState.load().run_dir)
    from e2e.harness import REPO_ROOT
    import time

    path = REPO_ROOT / "artifacts" / "e2e" / f"live-{time.strftime('%Y%m%d-%H%M%S')}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_up(args: argparse.Namespace) -> int:
    stack = LocalStack(with_web=not args.no_web, web_mode=args.web_mode, edition=args.edition)
    target = stack.up()
    print(json.dumps(stack_info(stack.state), indent=2))
    print(target.describe())
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Add the console to a stack started with --no-web."""
    stack = LocalStack.attach(StackState.load(), web_mode=args.web_mode)
    stack.start_web()
    stack.state.save()
    print(json.dumps(stack_info(stack.state), indent=2))
    return 0


def _report_leftovers(state: StackState, leftovers: list[str]) -> int:
    if not leftovers:
        print(f"stopped the stack in {state.run_dir}; nothing it started is still running")
        return 0
    print(f"e2e: the stack in {state.run_dir} was stopped but left these behind:",
          file=sys.stderr)
    for line in leftovers:
        print(f"  • {line}", file=sys.stderr)
    return 1


def cmd_down(args: argparse.Namespace) -> int:
    if args.all:
        # Every stack ever recorded here, stopped or not: what a crashed or killed `all` left.
        # Safe to repeat — recorded PIDs are only signalled while they are still the stack's.
        code = 0
        for state in StackState.everything_recorded():
            code = max(code, _report_leftovers(state, stop_state(state)))
        return code
    try:
        state = StackState.load()
    except TargetError as exc:
        print(exc)
        return 0
    return _report_leftovers(state, stop_state(state))


def cmd_status(_: argparse.Namespace) -> int:
    print(json.dumps(stack_info(StackState.load()), indent=2))
    return 0


def cmd_workflows(args: argparse.Namespace) -> int:
    from e2e.harness.drive import run_all

    target = _target(args)
    return run_all(target, _report_dir(args, target), only=args.only,
                   real_models_ok=args.real_models, with_examples=not args.no_examples,
                   install_catalogue=not args.no_install)


def cmd_console(args: argparse.Namespace) -> int:
    from e2e.harness.console import walk

    target = _target(args)
    return walk(target, _report_dir(args, target), only=args.only, headed=args.headed,
                allow_writes=target.kind == "local" or args.allow_writes,
                capture_bodies=args.capture_bodies)


def cmd_all(args: argparse.Namespace) -> int:
    if args.target == "live":
        return max(cmd_workflows(args), cmd_console(args))
    stack = LocalStack(with_web=not args.no_web, web_mode=args.web_mode, edition=args.edition)
    stack.up()
    code = 1
    try:
        print(stack.state.target().describe())
        code = cmd_workflows(args)
        if not args.no_web:
            code = max(code, cmd_console(args))
    finally:
        if args.keep:
            print(f"--keep: the stack is still up; `scripts/e2e.sh down` stops it. "
                  f"{json.dumps(stack_info(stack.state))}")
        else:
            # A run that leaves containers or processes on the developer's machine has not
            # passed, whatever the screens said (teardown.py names the incident).
            code = max(code, _report_leftovers(stack.state, stack.down()))
    return code


def main(argv: list[str] | None = None) -> int:
    # `kill -USR1 <pid>` prints where a long walk is: a harness that can hang must say where.
    import faulthandler
    import signal

    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    parser = argparse.ArgumentParser(prog="scripts/e2e.sh", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="all",
                        choices=["up", "web", "down", "status", "workflows", "console", "all"])
    parser.add_argument("--target", choices=["local", "live"], default="local")
    parser.add_argument("--only", action="append", default=[],
                        help="limit to these template/example keys or console areas")
    parser.add_argument("--no-web", action="store_true", help="do not start or walk the console")
    parser.add_argument("--web-mode", choices=["prod", "dev"], default="prod")
    parser.add_argument("--edition", default="community",
                        help="BOSS_EDITION of the local stack. community (the default) is what "
                             "boss-1 runs; Community refuses research and host deploys by "
                             "policy, so a paid edition shows how far the rest of a template gets")
    parser.add_argument("--keep", action="store_true", help="leave the stack up after `all`")
    parser.add_argument("--all", action="store_true",
                        help="with `down`: stop every stack recorded under artifacts/e2e, and "
                             "everything each one started")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--capture-bodies", action="store_true",
                        help="console walk: re-fetch /api and /v1 calls to record failure bodies "
                             "(findings are marked 'under interception')")
    parser.add_argument("--allow-writes", action="store_true",
                        help="on a live target, let the console walk press write actions")
    parser.add_argument("--no-examples", action="store_true",
                        help="do not run examples/*/setup.py (on a live install it publishes "
                             "workflows and grants skills to roles) or drive the examples")
    parser.add_argument("--no-install", action="store_true",
                        help="do not POST catalogue/install; drive only what is installed")
    parser.add_argument("--real-models", action="store_true",
                        help="acknowledge a live target spends real model budget")
    args = parser.parse_args(argv)
    handlers = {"up": cmd_up, "web": cmd_web, "down": cmd_down, "status": cmd_status,
                "workflows": cmd_workflows, "console": cmd_console, "all": cmd_all}
    try:
        return handlers[args.command](args)
    except TargetError as exc:
        print(f"e2e: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("e2e: interrupted. Whatever was collected is in the run's workflows.json / "
              "console.json; `scripts/e2e.sh down` stops a local stack.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — a sentence, and the traceback in a file
        import traceback

        from e2e.harness import REPO_ROOT

        log = REPO_ROOT / "artifacts" / "e2e" / "harness-crash.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc())
        print(f"e2e: the harness itself failed during `{args.command}`: "
              f"{type(exc).__name__}: {exc}. This is a harness bug, not a product finding; the "
              f"traceback is in {log}. Results collected before it are in the run directory.",
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
