#!/usr/bin/env python3
"""Compare FastAPI routes with api/openapi.yaml paths and methods."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "apps/api/app/main.py"
OPENAPI_PATH = ROOT / "api/openapi.yaml"

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def normalize_openapi_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")


def fastapi_routes() -> set[tuple[str, str]]:
    source = MAIN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = _unwrap_call(decorator)
            if call is None:
                continue
            func = _attr_name(call.func)
            if func not in {f"app.{method}" for method in HTTP_METHODS}:
                continue
            method = func.split(".", 1)[1].upper()
            if not call.args:
                continue
            path_arg = call.args[0]
            if isinstance(path_arg, ast.Constant) and isinstance(path_arg.value, str):
                routes.add((method, normalize_openapi_path(path_arg.value)))
    return routes


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


def main() -> int:
    impl = fastapi_routes()
    documented = openapi_routes()
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
