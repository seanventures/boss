#!/usr/bin/env python3
"""Generate static OpenAPI contracts from mounted FastAPI edition routes.

The private contract retains existing curated operation documentation while
adding newly mounted operations and current Pydantic schemas. The Community
contract is a fresh document from the Community composition, so paid routes
and their model schemas cannot leak through a path-only filter.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from copy import deepcopy
from pathlib import Path

import yaml
from sync_openapi_from_routes import splice_new_methods, splice_new_paths

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from boss_engine.services.api_operation_ids import (  # noqa: E402
    HTTP_METHODS,
    stable_operation_ids,
)

PRIVATE_SCHEMA = ROOT / "api/openapi.yaml"
COMMUNITY_SCHEMA = ROOT / "api/openapi.community.yaml"

# FastAPI cannot infer bodies read from a raw Request. These existing curated
# contracts document real protocol inputs, so keep them when the live schema
# has no requestBody. Every other existing input follows the mounted route.
RAW_REQUEST_BODY_OPERATIONS = frozenset(
    {
        ("post", "/saml/v2/{project_id}/acs"),
        ("post", "/v1/reliability/alert"),
        ("post", "/scim/v2/{project_id}/Groups"),
        ("put", "/scim/v2/{project_id}/Groups/{group_id}"),
        ("post", "/scim/v2/{project_id}/Users"),
        ("put", "/scim/v2/{project_id}/Users/{user_id}"),
        ("post", "/v1/webhooks/inbound/{source}"),
        ("post", "/v1/webhooks/github"),
        ("post", "/v1/webhooks/bitbucket"),
        ("post", "/v1/webhooks/plane"),
    }
)
RAW_REQUEST_HEADER_OPERATIONS = frozenset(
    {
        ("post", "/v1/webhooks/inbound/{source}"),
        ("post", "/v1/webhooks/github"),
    }
)


class _NoAliasDumper(yaml.SafeDumper):
    """A spliced field must not reuse anchor names elsewhere in the document."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def mounted_schema(edition: str) -> dict:
    os.environ["BOSS_EDITION"] = edition
    sys.path.insert(0, str(ROOT / "apps/api"))
    sys.path.insert(0, str(ROOT / "engine"))
    from app.main import app

    return app.openapi()


def operations(document: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, path_item in document["paths"].items()
        for method in path_item
        if method in HTTP_METHODS
    }


def add_missing_private_operation_ids(rendered: str, mounted: dict) -> str:
    """Complete curated operations without rewriting their existing IDs or prose."""
    existing = yaml.safe_load(rendered)
    missing = {
        (path, method): mounted["paths"][path][method]["operationId"]
        for path, path_item in existing["paths"].items()
        for method, operation in path_item.items()
        if method in HTTP_METHODS and not operation.get("operationId")
    }
    if not missing:
        return rendered

    out: list[str] = []
    current_path: str | None = None
    for line in rendered.splitlines(keepends=True):
        path_match = re.fullmatch(r"  (/[^\n:]+):\s*\n", line)
        if path_match:
            current_path = path_match.group(1)
        elif line and not line[0].isspace() and not line.startswith("paths:"):
            current_path = None
        out.append(line)
        method_match = re.fullmatch(r"    (get|post|put|patch|delete|head|options):\s*\n", line)
        if current_path and method_match:
            operation_id = missing.get((current_path, method_match.group(1)))
            if operation_id:
                out.append(f"      operationId: {operation_id}\n")
    updated = "".join(out)
    if any(
        not operation.get("operationId")
        for path_item in yaml.safe_load(updated)["paths"].values()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    ):
        raise ValueError("private contract still has unnamed operations")
    return updated


def private_operation_inputs(
    method: str, path: str, static: dict, runtime: dict
) -> dict[str, object]:
    """Use declared runtime inputs, plus documented raw-Request protocol inputs."""
    result: dict[str, object] = {}
    runtime_parameters = runtime.get("parameters") or []
    static_parameters = static.get("parameters") or []
    static_by_identity = {
        (parameter.get("in"), parameter.get("name")): parameter
        for parameter in static_parameters
    }
    parameters = []
    for runtime_parameter in runtime_parameters:
        parameter = runtime_parameter.copy()
        old = static_by_identity.get((parameter.get("in"), parameter.get("name")))
        if old and "description" not in parameter and old.get("description"):
            parameter["description"] = old["description"]
        parameters.append(parameter)
    if (method, path) in RAW_REQUEST_HEADER_OPERATIONS:
        parameters.extend(
            parameter
            for parameter in static_parameters
            if parameter.get("in") == "header"
            and (parameter.get("in"), parameter.get("name"))
            not in {(item.get("in"), item.get("name")) for item in parameters}
        )
    if parameters:
        result["parameters"] = parameters

    if runtime.get("requestBody"):
        body = runtime["requestBody"]
        runtime_content = body.get("content", {})
        runtime_json = runtime_content.get("application/json", {}).get("schema", {})
        static_body = static.get("requestBody") or {}
        static_json = (
            static_body.get("content", {}).get("application/json", {}).get("schema", {})
        )
        # A `payload: dict` handler has no field schema for FastAPI to emit.
        # Preserve its more useful curated field map, while inheriting the
        # runtime's body-required flag and open-object acceptance.
        if (
            len(runtime_content) == 1
            and runtime_json.get("type") == "object"
            and runtime_json.get("additionalProperties") is True
            and not runtime_json.get("properties")
            and static_json.get("type") == "object"
            and static_json.get("properties")
        ):
            body = deepcopy(static_body)
            body["required"] = runtime["requestBody"].get("required", False)
            body["content"]["application/json"]["schema"]["additionalProperties"] = True
        result["requestBody"] = body
    elif (method, path) in RAW_REQUEST_BODY_OPERATIONS:
        if not static.get("requestBody"):
            raise ValueError(f"Missing raw-Request body contract: {method.upper()} {path}")
        body = static["requestBody"].copy()
        body["required"] = True
        result["requestBody"] = body
    return result


def _field_span(block: str, field: str) -> tuple[int, int] | None:
    """Locate one operation-level YAML field without touching nested fields."""
    match = re.search(rf"(?m)^      {re.escape(field)}:(?:[^\n]*)\n", block)
    if not match:
        return None
    next_field = re.search(r"(?m)^      [A-Za-z][A-Za-z0-9_-]*:(?:[^\n]*)\n", block[match.end() :])
    end = match.end() + next_field.start() if next_field else len(block)
    return match.start(), end


def _replace_operation_input(block: str, field: str, value: object | None) -> str:
    span = _field_span(block, field)
    replacement = (
        "".join(
            "      " + line + "\n"
            for line in yaml.dump(
                {field: value}, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, width=100
            ).splitlines()
        )
        if value is not None
        else ""
    )
    if span:
        return block[: span[0]] + replacement + block[span[1] :]
    if value is None:
        return block
    before = ("requestBody", "responses") if field == "parameters" else ("responses",)
    insertion = next(
        (location[0] for name in before if (location := _field_span(block, name))),
        len(block),
    )
    return block[:insertion] + replacement + block[insertion:]


def sync_private_operation_inputs(rendered: str, mounted: dict) -> str:
    """Splice request inputs without reformatting curated operations or responses."""
    static_paths = yaml.safe_load(rendered)["paths"]
    spans: list[tuple[int, int, str, str]] = []
    current_path: str | None = None
    active: tuple[int, str, str] | None = None
    offset = 0
    for line in rendered.splitlines(keepends=True):
        path_match = re.fullmatch(r"  (/[^\n:]+):\s*\n", line)
        method_match = re.fullmatch(r"    (get|post|put|patch|delete|head|options):\s*\n", line)
        if path_match or method_match or line == "components:\n":
            if active:
                start, path, method = active
                spans.append((start, offset, path, method))
                active = None
        if path_match:
            current_path = path_match.group(1)
        elif line == "components:\n":
            current_path = None
        elif method_match and current_path:
            active = (offset, current_path, method_match.group(1))
        offset += len(line)
    if active:
        start, path, method = active
        spans.append((start, len(rendered), path, method))

    for start, end, path, method in reversed(spans):
        static = static_paths[path][method]
        runtime = mounted["paths"][path][method]
        desired = private_operation_inputs(method, path, static, runtime)
        if all(static.get(field) == desired.get(field) for field in ("parameters", "requestBody")):
            continue
        block = rendered[start:end]
        for field in ("parameters", "requestBody"):
            if static.get(field) != desired.get(field):
                block = _replace_operation_input(block, field, desired.get(field))
        rendered = rendered[:start] + block + rendered[end:]
    return rendered


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
        next_schema = re.search(r"(?m)^    [^ :]+:\n", rendered[match.end() : schema_end])
        stop = match.end() + next_schema.start() if next_schema else schema_end + 1
        block = "".join(
            "    " + line + "\n"
            for line in yaml.safe_dump(
                {name: schema}, sort_keys=False, allow_unicode=True, width=100
            ).splitlines()
        )
        rendered = rendered[: match.start()] + block + rendered[stop:]
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
        "BOSS Control Plane API for the current 1.0.0-rc.3 product release.",
    )
    rendered = add_missing_private_operation_ids(rendered, mounted)
    rendered = sync_private_operation_inputs(rendered, mounted)
    return "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"


def build_community(mounted: dict, product_version: str) -> dict:
    mounted["info"]["version"] = product_version
    mounted["info"]["description"] = (
        "BOSS Community self-hosted HTTP API. Generated from the routes mounted "
        "with BOSS_EDITION=community. API-key transport can be enabled by the "
        "self-hosted operator."
    )
    forbidden = ("/v1/account-connect", "/v1/marketplace", "/v1/self-improvement")
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


def preserve_documented_path_order(mounted: dict, previous: dict) -> None:
    """Keep unchanged paths in place when routes move between Python modules."""

    paths = mounted["paths"]
    previous_paths = previous.get("paths", {})
    mounted["paths"] = {
        **{path: paths[path] for path in previous_paths if path in paths},
        **{path: item for path, item in paths.items() if path not in previous_paths},
    }


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
        if destination.exists():
            preserve_documented_path_order(
                document, yaml.safe_load(destination.read_text(encoding="utf-8"))
            )
        rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
    operation_count = len(operations(yaml.safe_load(rendered)))
    if args.check:
        if not destination.exists() or destination.read_text(encoding="utf-8") != rendered:
            print(f"OpenAPI contract stale: {destination}", file=sys.stderr)
            return 1
        print(f"OpenAPI contract current: {destination} ({operation_count} operations)")
        return 0
    destination.write_text(rendered, encoding="utf-8")
    print(f"Wrote {destination} ({operation_count} operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
