#!/usr/bin/env python3
"""Compare the static contract with routes mounted by the selected edition.

The old source-text scan counted unmounted routers and missed dynamically
registered methods. Importing the FastAPI app checks the actual composition.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition", choices=("community", "enterprise"), default="enterprise")
    parser.add_argument("--schema", type=Path, default=ROOT / "api/openapi.yaml")
    parser.add_argument("--prefix", action="append", default=[])
    return parser.parse_args()


def mounted_routes(edition: str) -> set[tuple[str, str]]:
    os.environ["BOSS_EDITION"] = edition
    sys.path.insert(0, str(ROOT / "apps/api"))
    sys.path.insert(0, str(ROOT / "engine"))
    from app.main import app

    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.lower() in HTTP_METHODS
    }


def documented_routes(schema: Path) -> set[tuple[str, str]]:
    document = yaml.safe_load(schema.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, operations in (document.get("paths") or {}).items()
        for method, operation in operations.items()
        if method.lower() in HTTP_METHODS and isinstance(operation, dict)
    }


def within_prefixes(
    routes: set[tuple[str, str]], prefixes: list[str]
) -> set[tuple[str, str]]:
    if not prefixes:
        return routes
    return {
        route
        for route in routes
        if any(route[1].startswith(prefix.rstrip("/")) for prefix in prefixes)
    }


def main() -> int:
    args = parse_args()
    implemented = within_prefixes(mounted_routes(args.edition), args.prefix)
    documented = within_prefixes(documented_routes(args.schema), args.prefix)
    missing = sorted(implemented - documented)
    extra = sorted(documented - implemented)
    if missing:
        print("Mounted routes missing from OpenAPI:")
        for method, path in missing:
            print(f"  {method} {path}")
    if extra:
        print("OpenAPI routes not mounted in selected edition:")
        for method, path in extra:
            print(f"  {method} {path}")
    if missing or extra:
        print(f"Parity failed: {len(missing)} missing, {len(extra)} extra")
        return 1
    print(f"OpenAPI route parity OK ({len(implemented)} {args.edition} operations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
