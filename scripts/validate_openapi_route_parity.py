#!/usr/bin/env python3
"""Compare FastAPI routes with api/openapi.yaml paths and methods."""

from __future__ import annotations

import ast
import argparse
import importlib.util
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "apps/api/app/main.py"
OPENAPI_PATH = ROOT / "api/openapi.yaml"
SYNC_SCRIPT = ROOT / "scripts/sync_openapi_from_routes.py"

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def normalize_openapi_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_openapi_from_routes", SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SYNC_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fastapi_routes() -> set[tuple[str, str]]:
    """Collect FastAPI routes from main.py plus isolated routers."""
    routes: set[tuple[str, str]] = set()
    sync = _load_sync_module()
    for path, methods in sync.collect_impl().items():
        if not path.startswith("/"):
            continue
        normalized = normalize_openapi_path(sync.normalize_fastapi_path(path))
        for method in methods:
            routes.add((method.upper(), normalized))
    # Keep the original AST walk for app.api_route() entries that the regex
    # collector does not see (methods= keyword on api_route).
    source = MAIN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = _unwrap_call(decorator)
            if call is None:
                continue
            func = _attr_name(call.func)
            if func != "app.api_route" or not call.args:
                continue
            path_arg = call.args[0]
            if not (
                isinstance(path_arg, ast.Constant) and isinstance(path_arg.value, str)
            ):
                continue
            path = normalize_openapi_path(path_arg.value)
            for method in _methods_kwarg(call):
                routes.add((method.upper(), path))
    return routes


def _methods_kwarg(call: ast.Call) -> list[str]:
    for keyword in call.keywords:
        if keyword.arg != "methods":
            continue
        value = keyword.value
        if isinstance(value, (ast.List, ast.Tuple)):
            methods: list[str] = []
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    methods.append(elt.value.lower())
            return methods or ["get"]
    return ["get"]


def openapi_routes() -> set[tuple[str, str]]:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    routes: set[tuple[str, str]] = set()
    for path, operations in (document.get("paths") or {}).items():
        normalized = normalize_openapi_path(path)
        for method, operation in operations.items():
            if method.lower() in HTTP_METHODS and isinstance(operation, dict):
                routes.add((method.upper(), normalized))
    return routes


def _unwrap_call(node: ast.AST) -> ast.Call | None:
    current = node
    while isinstance(current, ast.Call):
        return current
    return None


def _attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        prefix = _attr_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare FastAPI routes with api/openapi.yaml paths and methods."
    )
    parser.add_argument(
        "--prefix",
        action="append",
        default=[],
        help="Validate only routes beginning with this path prefix. Repeatable.",
    )
    return parser.parse_args()


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
    impl = fastapi_routes()
    documented = openapi_routes()
    impl = within_prefixes(impl, args.prefix)
    documented = within_prefixes(documented, args.prefix)
    missing_in_openapi = sorted(impl - documented)
    extra_in_openapi = sorted(documented - impl)
    failed = False
    if missing_in_openapi:
        failed = True
        print("Routes implemented but missing from OpenAPI:")
        for method, path in missing_in_openapi:
            print(f"  {method} {path}")
    if extra_in_openapi:
        failed = True
        print("OpenAPI routes with no FastAPI implementation:")
        for method, path in extra_in_openapi:
            print(f"  {method} {path}")
    if not failed:
        print(f"OpenAPI route parity OK ({len(impl)} routes).")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
