#!/usr/bin/env python3
"""Generate static OpenAPI contracts from mounted FastAPI edition routes.

The private contract retains existing curated operation documentation while
adding newly mounted operations and current Pydantic schemas. The Community
contract is a fresh document from the Community composition, so paid routes
and their model schemas cannot leak through a path-only filter.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import yaml
from sync_openapi_from_routes import splice_new_methods, splice_new_paths

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SCHEMA = ROOT / "api/openapi.yaml"
COMMUNITY_SCHEMA = ROOT / "api/openapi.community.yaml"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def mounted_schema(edition: str) -> dict:
    os.environ["BOSS_EDITION"] = edition
    sys.path.insert(0, str(ROOT / "apps/api"))
    sys.path.insert(0, str(ROOT / "engine"))
    from app.main import app

    return app.openapi()


def stable_operation_ids(document: dict) -> None:
    """Avoid FastAPI's unstable set-method order for multi-method routes."""
    for path, operations in document["paths"].items():
        # FastAPI iterates a set for multi-method routes. Canonical ordering
        # makes repeated generation byte-for-byte stable across processes.
        document["paths"][path] = dict(sorted(operations.items()))
        operations = document["paths"][path]
        for method, operation in operations.items():
            if method not in HTTP_METHODS:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", f"{method}_{path}".lower()).strip("_")
            digest = hashlib.sha256(f"{method} {path}".encode()).hexdigest()[:8]
            operation["operationId"] = f"{slug[:68]}_{digest}"


def operations(document: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, path_item in document["paths"].items()
        for method in path_item
        if method in HTTP_METHODS
    }


def render_private(original: str, existing: dict, mounted: dict) -> str:
    stable_operation_ids(mounted)
    extra = operations(existing) - operations(mounted)
    if extra:
        sample = ", ".join(f"{m.upper()} {p}" for m, p in sorted(extra)[:5])
        raise ValueError(f"private contract has unmounted operations: {sample}")
    new_paths: dict = {}
    new_methods: dict = {}
    for path, path_item in mounted["paths"].items():
        for method, operation in path_item.items():
            if method in HTTP_METHODS and method not in existing["paths"].get(path, {}):
                target = new_methods if path in existing["paths"] else new_paths
                target.setdefault(path, {})[method] = operation
    rendered = splice_new_paths(original, new_paths, yaml)
    rendered = splice_new_methods(rendered, new_methods, yaml)
    # Existing hand-written schemas may lag changed response/request models.
    # Keep historical names needed by curated operations, but make each live
    # schema match the runtime type boundary exactly.
    static_schemas = existing.setdefault("components", {}).setdefault("schemas", {})
    runtime_schemas = mounted.get("components", {}).get("schemas", {})
    changed = {
        name: schema
        for name, schema in runtime_schemas.items()
        if static_schemas.get(name) != schema
    }
    schema_end = rendered.find("\ntags:\n")
    if schema_end < 0:
        raise ValueError("Cannot find end of components.schemas in static contract")
    # Replace only the changed schema blocks; a PyYAML dump of the whole
    # contract would churn tens of thousands of unrelated curated lines.
    for name, schema in changed.items():
        if name not in static_schemas:
            continue
        pattern = re.compile(rf"(?m)^    {re.escape(name)}:\n")
        match = pattern.search(rendered, rendered.find("\n  schemas:\n"), schema_end)
        if match is None:
            raise ValueError(f"Cannot find schema block: {name}")
        next_schema = re.search(r"(?m)^    [^ :]+:\n", rendered[match.end():schema_end])
        stop = match.end() + next_schema.start() if next_schema else schema_end + 1
        block = "".join(
            "    " + line + "\n"
            for line in yaml.safe_dump(
                {name: schema}, sort_keys=False, allow_unicode=True, width=100
            ).splitlines()
        )
        rendered = rendered[:match.start()] + block + rendered[stop:]
        schema_end = rendered.find("\ntags:\n")
    added = {name: schema for name, schema in changed.items() if name not in static_schemas}
    if added:
        block = "".join(
            "    " + line + "\n"
            for line in yaml.safe_dump(
                added, sort_keys=False, allow_unicode=True, width=100
            ).splitlines()
        )
        rendered = rendered[:schema_end] + "\n" + block + rendered[schema_end:]
    rendered = rendered.replace(
        "OpenAPI document version aligned to product 0.13.0 (was 0.2.1).",
        "BOSS Control Plane API for the current 0.14.0 product release.",
    )
    return "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"


def build_community(mounted: dict, product_version: str) -> dict:
    mounted["info"]["version"] = product_version
    mounted["info"]["description"] = (
        "BOSS Community self-hosted HTTP API. Generated from the routes mounted "
        "with BOSS_EDITION=community. API-key transport can be enabled by the "
        "self-hosted operator."
    )
    forbidden = (
        "/v1/account-connect", "/v1/marketplace", "/v1/self-improvement"
    )
    shared_identity = (
        "/v1/enterprise/identity-providers",
        "/v1/enterprise/identity-rollout-policies",
        "/v1/enterprise/scim/",
    )
    leaks = [
        path
        for path in mounted["paths"]
        if path.startswith(forbidden)
        or (path.startswith("/v1/enterprise/") and not path.startswith(shared_identity))
        or (path.startswith("/v1/projects/") and "terraform-" in path)
    ]
    if leaks:
        raise ValueError(f"paid routes mounted in Community: {leaks[:5]}")
    stable_operation_ids(mounted)
    return mounted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition", choices=("enterprise", "community"), required=True)
    parser.add_argument("--check", action="store_true", help="Fail if generated file differs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    original = PRIVATE_SCHEMA.read_text(encoding="utf-8")
    existing = yaml.safe_load(original)
    mounted = mounted_schema(args.edition)
    if args.edition == "enterprise":
        destination = PRIVATE_SCHEMA
        rendered = render_private(original, existing, mounted)
    else:
        destination = COMMUNITY_SCHEMA
        document = build_community(mounted, existing["info"]["version"])
        rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
    if args.check:
        if not destination.exists() or destination.read_text(encoding="utf-8") != rendered:
            print(f"OpenAPI contract stale: {destination}", file=sys.stderr)
            return 1
        operation_count = len(operations(yaml.safe_load(rendered)))
        print(f"OpenAPI contract current: {destination} ({operation_count} operations)")
        return 0
    destination.write_text(rendered, encoding="utf-8")
    operation_count = len(operations(yaml.safe_load(rendered)))
    print(f"Wrote {destination} ({operation_count} operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
