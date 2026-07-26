#!/usr/bin/env python3
"""Verification harness for the engine split.

Run before/after any structural change to prove nothing broke. Exits non-zero on
the first failure. Checks:

  1. The engine imports STANDALONE (only boss_engine on the path — no apps/).
  2. The tool registry builds and both schema shapes are valid.
  3. (optional, --with-api) the full internal API graph still imports and yields
     the expected route count — the strongest regression signal for the move.
  4. (optional, --live) read tools return real data against $DATABASE_URL.

Usage:
    PYTHONPATH=engine python engine/scripts/verify_engine.py
    PYTHONPATH=repo_root:engine python engine/scripts/verify_engine.py --with-api 397
    DATABASE_URL=... PYTHONPATH=engine python engine/scripts/verify_engine.py --live
"""
from __future__ import annotations

import argparse
import sys


def _ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}")
    sys.exit(1)


def check_engine_imports() -> None:
    try:
        import boss_engine  # noqa: F401
        import boss_engine.agent  # noqa: F401
        import boss_engine.mcp  # noqa: F401
        import boss_engine.services.core  # noqa: F401
        import boss_engine.tools  # noqa: F401
    except Exception as exc:  # pragma: no cover
        _fail(f"engine import: {type(exc).__name__}: {exc}")
    _ok("engine imports standalone")


def check_registry() -> None:
    from boss_engine.tools import build_default_registry

    reg = build_default_registry()
    if not reg.names():
        _fail("registry is empty")
    for s in reg.openai_schema():
        if s.get("type") != "function":
            _fail("bad openai schema")
    for s in reg.mcp_schema():
        if "inputSchema" not in s:
            _fail("bad mcp schema")
    _ok(f"registry: {len(reg.names())} tools "
        f"({len(reg.specs(kind='read'))} read, {len(reg.specs(kind='write'))} write)")


def check_api(expected_routes: int) -> None:
    import importlib

    try:
        m = importlib.import_module("apps.api.app.main")
    except Exception as exc:
        _fail(f"apps.api.app.main import: {type(exc).__name__}: {exc}")
    n = len(getattr(m.app, "routes", []))
    if expected_routes and n != expected_routes:
        _fail(f"route count {n} != expected {expected_routes}")
    # single module identity across the shim
    old = importlib.import_module("apps.api.app.services.code_task_service")
    new = importlib.import_module("boss_engine.services.code_task_service")
    if old is not new:
        _fail("module identity mismatch through the services shim")
    _ok(f"api graph imports: {n} routes; shim identity intact")


def check_live() -> None:
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    ctx = EngineContext.create()
    if not ctx.database_reachable():
        _fail("database not reachable")
    reg = build_default_registry()
    r = reg.invoke("project_status", ctx, project_id="boss-dogfood", actor="verify", args={})
    if not r.get("ok", True):
        _fail(f"project_status failed: {r}")
    _ok(f"live read tool ok (total_running={r.get('total_running')})")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--with-api", type=int, metavar="EXPECTED_ROUTES", default=0,
                   help="also import apps.api.app.main and assert route count")
    p.add_argument("--live", action="store_true", help="hit $DATABASE_URL with a read tool")
    args = p.parse_args()

    print("verify_engine:")
    check_engine_imports()
    check_registry()
    if args.with_api:
        check_api(args.with_api)
    if args.live:
        check_live()
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
