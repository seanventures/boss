"""What the console DECLARES about access, parsed from its source (never hand-listed), and what
the catalogue says each role holds.

    areas()         {area id: permission}                         apps/web/lib/identity/areas.ts AREA_ACCESS
    area_actions()  {area or page id: {action: permission}}        apps/web/lib/identity/areas.ts AREA_ACTIONS
    pages()         [Page(path, label, permission|None)]           apps/web/lib/identity/pages.ts IDENTITY_PAGES + CONSOLE_PAGES
    catalogue()     Catalogue(role -> permissions, key -> scope)   packages/identity/catalogue.yaml
    custom_role()   the seeded `developer` role's permissions      engine/boss_engine/identity/seed_accounts.py

A walk built on these follows the code: a new area, page or action is walked the day it is
declared, and a declaration the parser cannot read fails loudly instead of being skipped.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]
AREAS_TS = REPO / "apps/web/lib/identity/areas.ts"
PAGES_TS = REPO / "apps/web/lib/identity/pages.ts"
CATALOGUE_YAML = REPO / "packages/identity/catalogue.yaml"
SEED_PY = REPO / "engine/boss_engine/identity/seed_accounts.py"


class MapError(RuntimeError):
    pass


def _block(src: str, start_marker: str) -> str:
    """The `{ ... }` / `[ ... ]` literal that follows `start_marker`, brace-balanced."""
    at = src.find(start_marker)
    if at < 0:
        raise MapError(f"{start_marker!r} not found")
    i = src.index("=", at + len(start_marker)) + 1      # past the type annotation
    while src[i] not in "{[":
        i += 1
    opener = src[i]
    closer = "}" if opener == "{" else "]"
    depth = 0
    for j in range(i, len(src)):
        if src[j] == opener:
            depth += 1
        elif src[j] == closer:
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    raise MapError(f"unbalanced literal after {start_marker!r}")


_KEY = r'(?:"([^"]+)"|([A-Za-z_][\w]*))'


def areas() -> dict[str, str]:
    body = _block(AREAS_TS.read_text(), "export const AREA_ACCESS")
    found = {}
    for m in re.finditer(_KEY + r'\s*:\s*p\("([a-z_]+:[a-z_]+)"\)', body):
        found[m.group(1) or m.group(2)] = m.group(3)
    declared = len(re.findall(r":\s*p\(", body))
    if not found or len(found) != declared:
        raise MapError(f"AREA_ACCESS: parsed {len(found)} of {declared} entries")
    return found


def identity_page_access() -> dict[str, Optional[str]]:
    body = _block(AREAS_TS.read_text(), "export const IDENTITY_PAGE_ACCESS")
    out: dict[str, Optional[str]] = {}
    for m in re.finditer(_KEY + r'\s*:\s*(?:p\("([a-z_]+:[a-z_]+)"\)|\{\s*authenticated:\s*true\s*\})', body):
        out[m.group(1) or m.group(2)] = m.group(3)
    return out


def area_actions() -> dict[str, dict[str, str]]:
    body = _block(AREAS_TS.read_text(), "export const AREA_ACTIONS")
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(_KEY + r"\s*:\s*\{([^{}]*)\}", body):
        key = m.group(1) or m.group(2)
        out[key] = {a: perm for a, perm in re.findall(r'(\w+)\s*:\s*"([a-z_]+:[a-z_]+)"', m.group(3))}
    if not out:
        raise MapError("AREA_ACTIONS: nothing parsed")
    return out


@dataclass(frozen=True)
class Page:
    path: str
    label: str
    permission: Optional[str]          # None: any signed-in person
    source: str                        # IDENTITY_PAGES | CONSOLE_PAGES
    any_scope: bool = False            # { anyScope: p }: held at the org OR in any project


def pages() -> list[Page]:
    src = PAGES_TS.read_text()
    area_perm = areas()
    out: list[Page] = []
    for name in ("IDENTITY_PAGES", "CONSOLE_PAGES"):
        body = _block(src, f"export const {name}")
        entries = re.findall(r'\{\s*path:\s*"([^"]+)",\s*label:\s*"([^"]+)",.*?access:\s*'
                             r'(\{\s*permission:\s*"([a-z_]+:[a-z_]+)"\s*\}|\{\s*authenticated:\s*true\s*\}|area\("([^"]+)"\)'
                             r'|\{\s*anyScope:\s*"([a-z_]+:[a-z_]+)"\s*\})',
                             body)
        literal_paths = len(re.findall(r'\{\s*path:\s*"', body))
        if len(entries) != literal_paths:
            raise MapError(f"{name}: parsed {len(entries)} of {literal_paths} page entries")
        for path, label, _whole, perm, area, any_perm in entries:
            permission = perm or any_perm or (area_perm[area] if area else None)
            out.append(Page(path, label, permission, name, any_scope=bool(any_perm)))
    return out


def public_pages() -> list[str]:
    """The sign-in pages CONSOLE_PAGES maps as public (not walked: they need no session)."""
    body = _block(PAGES_TS.read_text(), "export const CONSOLE_PAGES")
    m = re.search(r"\.\.\.\[([^\]]*)\]\.map", body)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


@dataclass(frozen=True)
class Catalogue:
    roles: dict[str, frozenset[str]]
    scope: dict[str, str]              # permission -> "org" | "project"


def catalogue() -> Catalogue:
    import yaml

    doc = yaml.safe_load(CATALOGUE_YAML.read_text())
    roles: dict[str, set[str]] = {}
    scope: dict[str, str] = {}
    for cat in doc["categories"]:
        for p in cat["permissions"]:
            scope[p["key"]] = p.get("scope", "project")
            for r in p.get("roles") or []:
                roles.setdefault(r, set()).add(p["key"])
    return Catalogue({k: frozenset(v) for k, v in roles.items()}, scope)


def custom_role() -> tuple[str, frozenset[str]]:
    """(key, permissions) of the seeded custom role, read from the seed module's literals."""
    tree = ast.parse(SEED_PY.read_text())
    vals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("CUSTOM_ROLE_KEY", "CUSTOM_ROLE_PERMISSIONS"):
                vals[node.targets[0].id] = ast.literal_eval(node.value)
    return vals["CUSTOM_ROLE_KEY"], frozenset(vals["CUSTOM_ROLE_PERMISSIONS"])


if __name__ == "__main__":     # a quick look at what the walk will cover
    print(len(areas()), "areas;", len(pages()), "pages;", len(area_actions()), "action sets;",
          "public:", public_pages())
    for p in pages():
        print(f"  {p.source:14} {p.path:40} {p.permission}")
