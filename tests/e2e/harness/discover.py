"""What the harness covers, read from the code — never a list kept here.

Six streams change templates and screens at once. A harness that hard-coded "the twelve
templates" or "the thirty-six areas" would go stale the day one was added, and stale in the worst
direction: the new template or screen is exactly the one nobody has proven yet, and it would be
silently skipped. So every list comes from where the product defines it:

  catalogue templates   GET /v1/delivery-workflows/catalogue (the running install), and
                        workflow_catalogue.CATALOGUE (the source) for the self-test
  opt-in examples       every examples/<dir>/setup.py, its NAME read from the file
  console areas         the AREAS array in apps/web/app/ConsoleShell.tsx
  console routes        every apps/web/app/**/page.tsx that is not an API route

The self-tests in tests/e2e/selftest assert that these readers find what the source holds, so a
change to how the product declares them breaks a test instead of shrinking the coverage.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import REPO_ROOT

EXAMPLES_DIR = REPO_ROOT / "examples"
CONSOLE_SHELL = REPO_ROOT / "apps" / "web" / "app" / "ConsoleShell.tsx"
WEB_APP = REPO_ROOT / "apps" / "web" / "app"


# ----------------------------------------------------------------------------- templates
def catalogue_from_api(api) -> list[dict[str, Any]]:
    """The catalogue as the running install serves it — what the console's Catalogue tab shows."""
    body = api.get("/v1/delivery-workflows/catalogue")
    return [item for item in (body or {}).get("items") or [] if item.get("key")]


def catalogue_keys_from_source() -> list[str]:
    """CATALOGUE keys, read from workflow_catalogue.py without importing the engine."""
    path = REPO_ROOT / "engine" / "boss_engine" / "services" / "workflow_catalogue.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        if not any(t.id == "CATALOGUE" for t in targets):
            continue
        keys: list[str] = []
        value = node.value
        if isinstance(value, ast.Dict):
            keys = [k.value for k in value.keys if isinstance(k, ast.Constant)]
        elif isinstance(value, (ast.List, ast.Tuple)):
            for element in value.elts:
                keys.extend(_key_of(element))
        if keys:
            return keys
    raise LookupError("workflow_catalogue.py no longer assigns CATALOGUE as a literal; "
                      "teach tests/e2e/harness/discover.py how it is declared now")


def _key_of(node: ast.AST) -> list[str]:
    """`{"key": "x", ...}`, `dict(key="x")` or a helper call whose first argument is the key."""
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "key" and isinstance(v, ast.Constant):
                return [v.value]
    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                return [kw.value.value]
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return [node.args[0].value]
    return []


# ----------------------------------------------------------------------------- examples
@dataclass(frozen=True)
class Example:
    directory: str          # examples/<directory>
    name: str               # NAME in its setup.py
    setup: Path

    @property
    def workflow_key(self) -> str:
        # examples/_common.publish: slug = name.lower().replace(" ", "_")
        return self.name.lower().replace(" ", "_")


def examples() -> list[Example]:
    found: list[Example] = []
    for setup in sorted(EXAMPLES_DIR.glob("*/setup.py")):
        name = _module_constant(setup, "NAME")
        if not name:
            raise LookupError(f"{setup} has no NAME constant; the harness reads the workflow "
                              f"key from it")
        found.append(Example(setup.parent.name, name, setup))
    return found


def examples_listed_by_setup_all() -> list[str]:
    tree = ast.parse((EXAMPLES_DIR / "setup_all.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXAMPLES" for t in node.targets):
            return [el.elts[0].value for el in node.value.elts  # type: ignore[attr-defined]
                    if isinstance(el, ast.Tuple) and isinstance(el.elts[0], ast.Constant)]
    raise LookupError("examples/setup_all.py has no EXAMPLES list")


def _module_constant(path: Path, name: str) -> Optional[str]:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


# ----------------------------------------------------------------------------- console
@dataclass(frozen=True)
class Area:
    id: str
    label: str
    group: str


_AREA_RE = re.compile(
    r'\{\s*id:\s*"(?P<id>[^"]+)",\s*label:\s*"(?P<label>[^"]+)".*?group:\s*"(?P<group>[^"]+)"',
    re.S)


def console_areas() -> list[Area]:
    """Every entry of `const AREAS: AreaDef[] = [...]` in ConsoleShell.tsx, in order."""
    source = CONSOLE_SHELL.read_text()
    start = source.find("const AREAS: AreaDef[] = [")
    if start < 0:
        raise LookupError("ConsoleShell.tsx no longer declares `const AREAS: AreaDef[] = [`; "
                          "teach tests/e2e/harness/discover.py where the areas live now")
    end = source.find("\n];", start)
    block = re.sub(r"//[^\n]*", "", source[start:end])
    # Entries may span lines (the enterprise one does); match id … group within one {…}.
    entries = re.findall(r"\{[^{}]*\}", block)
    areas: list[Area] = []
    for entry in entries:
        ident = re.search(r'\bid:\s*"([^"]+)"', entry)
        label = re.search(r'\blabel:\s*"([^"]+)"', entry)
        group = re.search(r'\bgroup:\s*"([^"]+)"', entry)
        if ident and group:
            areas.append(Area(ident.group(1), label.group(1) if label else ident.group(1),
                              group.group(1)))
    if not areas:
        raise LookupError("found AREAS in ConsoleShell.tsx but could not read any entry")
    return areas


def console_dynamic_routes() -> list[str]:
    """Page routes with a dynamic segment (`/v2/missions/[id]`). The walk does not visit them —
    it has no id to put there — and says so in its report instead of implying it did."""
    routes: list[str] = []
    for page in sorted(WEB_APP.rglob("page.tsx")):
        parts = [p for p in page.relative_to(WEB_APP).parent.parts
                 if not (p.startswith("(") and p.endswith(")"))]
        if parts and parts[0] != "api" and any(p.startswith("[") for p in parts):
            routes.append("/" + "/".join(parts))
    return routes


def console_routes() -> list[str]:
    """Static page routes (`/login`, `/v2/factory`, …). Dynamic segments are skipped and listed
    by console_dynamic_routes()."""
    routes: list[str] = []
    for page in sorted(WEB_APP.rglob("page.tsx")):
        rel = page.relative_to(WEB_APP).parent
        parts = [p for p in rel.parts if not (p.startswith("(") and p.endswith(")"))]
        if any(p.startswith("[") for p in parts) or (parts and parts[0] == "api"):
            continue
        routes.append("/" + "/".join(parts))
    return routes
