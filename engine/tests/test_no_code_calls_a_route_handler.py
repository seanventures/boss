"""No code calls a route handler: a route handler is FastAPI's door, not a function.

The Infra Deploy button called `delivery_deploy_endpoint(body)` from Python. Identity S2 then gave
that handler a `request: Request` parameter, which FastAPI fills and a Python caller does not, and
every app-target deploy answered 500. Its test faked the handler, so nothing saw it. Setting up a
code task's deployment had the twin bug: it passed a dict to a handler whose body became typed and
read `.payload()` off it.

A handler's signature belongs to FastAPI (bodies, `Request`, `Depends`, `Query(...)` defaults) and
its guard runs only on HTTP, so the class is closed here: the logic lives in a plain function and
the handler and every internal caller both call that. Any reference to a route handler from code
(a call, or passing it as a callback) fails this test, wherever the handler is defined and however
it is imported.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {ROOT / "apps" / "api" / "app": "apps.api.app",
           ROOT / "engine" / "boss_engine": "boss_engine"}
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace",
                 "api_route", "websocket", "route"}


def _is_route_handler(fn: ast.AST) -> bool:
    return isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        and d.func.attr in ROUTE_METHODS for d in fn.decorator_list)


def _handlers(tree: ast.AST) -> set[str]:
    return {n.name for n in ast.walk(tree) if _is_route_handler(n)}


def _resolve(module: str, node: ast.ImportFrom) -> str:
    """The absolute dotted name an ImportFrom names (`from .. import main` in a.b.c -> a.b)."""
    if not node.level:
        return node.module or ""
    base = module.split(".")[: -node.level]
    return ".".join(base + ([node.module] if node.module else []))


def _locally_bound(fn: ast.AST) -> set[str]:
    """Names a function binds itself (args, imports, assignments), which shadow a module name."""
    names: set[str] = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = fn.args
        names |= {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x}
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(x.asname or x.name).split(".")[0] for x in node.names}
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn \
                and not _is_route_handler(node):
            names.add(node.name)
    return names


def violations(files: dict[str, str]) -> list[str]:
    """Every reference to a route handler in `files` ({dotted module: source})."""
    trees = {m: ast.parse(src) for m, src in files.items()}
    handlers = {m: _handlers(t) for m, t in trees.items()}
    found: list[str] = []
    for module, tree in trees.items():
        # Names bound to a module that defines handlers, and handlers imported by name.
        module_aliases: dict[str, str] = {}
        imported: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = _resolve(module, node)
                for alias in node.names:
                    as_module = f"{source}.{alias.name}" if source else alias.name
                    if handlers.get(as_module):
                        module_aliases[alias.asname or alias.name] = as_module
                    elif alias.name in handlers.get(source, ()):
                        imported[alias.asname or alias.name] = f"{source}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and handlers.get(alias.name):
                        module_aliases[alias.asname] = alias.name

        def visit(node: ast.AST, shadowed: frozenset[str]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # An import of a handler (or of its module) is what is being looked for, so it
                # does not shadow; any other local binding of a handler's name does.
                shadowed = shadowed | (_locally_bound(node) - module_aliases.keys()
                                       - imported.keys())
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and node.id not in shadowed:
                if node.id in handlers[module]:
                    found.append(f"{module}:{node.lineno} {node.id}")
                elif node.id in imported:
                    found.append(f"{module}:{node.lineno} {imported[node.id]}")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in module_aliases and node.value.id not in shadowed \
                    and node.attr in handlers[module_aliases[node.value.id]]:
                found.append(f"{module}:{node.lineno} {module_aliases[node.value.id]}.{node.attr}")
            for child in ast.iter_child_nodes(node):
                visit(child, shadowed)

        visit(tree, frozenset())
    return sorted(found)


def _product_sources() -> dict[str, str]:
    files = {}
    for root, package in SOURCES.items():
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).with_suffix("")
            # A package keeps its `__init__` so `from . import x` resolves to the package.
            files[".".join([package, *rel.parts])] = path.read_text(encoding="utf-8")
    return files


def test_no_code_calls_a_route_handler():
    found = violations(_product_sources())
    assert not found, (
        "these reference a route handler from code. A handler's signature is FastAPI's (Request, "
        "bodies, Depends) and its guard runs only on HTTP; move its body into a plain function "
        f"that the handler and these callers both call: {found}")


def test_the_check_sees_every_way_in():
    """The check is only as good as what it can see: each shape the codebase has used."""
    files = {
        "pkg.main": (
            "@app.post('/x')\n"
            "def deploy_endpoint(payload, request):\n    return plan(payload)\n"
            "def plan(payload):\n    return payload\n"
            "def caller():\n    return deploy_endpoint({})\n"                  # same module
            "def callback():\n    return run(starter=deploy_endpoint)\n"     # passed along
            "def shadowed():\n    from x import deploy_endpoint\n    return deploy_endpoint()\n"
        ),
        "pkg.routers.infra": (
            "def build(router):\n"
            "    @router.post('/y')\n"
            "    def inner(body):\n        return 1\n"
            "    def other():\n        return inner(2)\n"                     # inside a closure
            "    from .. import main as api_main\n"
            "    return api_main.deploy_endpoint(1)\n"                       # via module alias
        ),
        "pkg.other": "from .main import deploy_endpoint as d\nd(1)\n",      # imported by name
        "pkg.fine": "from . import main\nmain.plan(1)\n",
    }
    assert violations(files) == sorted([
        "pkg.main:7 deploy_endpoint",
        "pkg.main:9 deploy_endpoint",
        "pkg.routers.infra:6 inner",
        "pkg.routers.infra:8 pkg.main.deploy_endpoint",
        "pkg.other:2 pkg.main.deploy_endpoint",
    ])
