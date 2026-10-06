#!/usr/bin/env python3
"""Write the identity console's route fixture from the real API's route table.

    PYTHONPATH=.:engine python3 scripts/dump-identity-api-routes.py           # rewrite it
    PYTHONPATH=.:engine python3 scripts/dump-identity-api-routes.py --check   # exit 1 if stale

The identity console was built against an in-process stub (apps/web/lib/identity/stub) while
S1–S5 built the API, and the two drifted: paths the console called did not exist. The vitest
apps/web/tests/identity-console-calls-real-routes.test.ts now pins every console call's method
and path to THIS list, taken from FastAPI's own routing table — and
apps/api/tests/test_the_identity_route_fixture_is_fresh.py refuses a stale copy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "apps/web/tests/fixtures/identity-api-routes.json"
#: The API surfaces the identity console (and its preview proxy) calls.
PREFIXES = ("/v1/identity/", "/v1/identity", "/v1/auth/", "/v1/previews/")


def routes() -> list[str]:
    from fastapi.routing import APIRoute

    from apps.api.app.main import app

    out: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not any(route.path == p or route.path.startswith(p) for p in PREFIXES):
            continue
        for method in route.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            out.add(f"{method} {route.path}")
    return sorted(out, key=lambda r: (r.split(" ", 1)[1], r.split(" ", 1)[0]))


def rendered() -> str:
    return json.dumps({"generated_by": "scripts/dump-identity-api-routes.py", "routes": routes()}, indent=1) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = rendered()
    if args.check:
        current = FIXTURE.read_text() if FIXTURE.exists() else ""
        if current != text:
            print(f"{FIXTURE.relative_to(ROOT)} is stale: run scripts/dump-identity-api-routes.py", file=sys.stderr)
            return 1
        return 0
    FIXTURE.write_text(text)
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(routes())} routes)")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "engine")]
    raise SystemExit(main())
