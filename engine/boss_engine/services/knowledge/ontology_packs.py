"""Ontology packs (KG Domain layer v1, A2): node/edge kinds beyond BOSS's core (Code, Rules,
Learning), declared per project from a versioned YAML pack under
``packages/specpack/ontology/``.

A project's effective kind set is core ∪ its one active pack (``kg_ontology_packs``, one row per
project). Before diff 2 sets any pointer, every project's effective set is core-only, so writing
a pack kind (``document``, ``clause``, ``regulation``, …) is refused with a reason — the
"mid-rollout" window this module is built to make safe, not merely survive.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

SHIPPED_PACK_IDS = ("banking-regulatory", "banking-dq")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_ontology_packs (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  pack_id TEXT NOT NULL,
  version TEXT NOT NULL,
  set_by TEXT NOT NULL,
  set_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PackRefused(ValueError):
    """A pack cannot be loaded or set, for a reason a person has to change."""


@dataclass(frozen=True)
class OntologyPack:
    pack_id: str
    version: str
    kinds: frozenset[str]
    edges: frozenset[str]
    kind_meta: dict[str, dict[str, Any]]


def ensure_schema(connection: Connection) -> None:
    for stmt in _SCHEMA_SQL.split(";"):
        sql = stmt.strip()
        if sql:
            connection.execute(text(sql))


def _ontology_root() -> Path:
    from ...config import get_settings

    return get_settings().specpack_root / "ontology"


def _parse_pack(pack_id: str, document: dict[str, Any]) -> OntologyPack:
    from .store import NODE_KINDS

    kinds_spec = document.get("kinds") or []
    edges_spec = document.get("edges") or []
    kinds = frozenset(str(k["id"]) for k in kinds_spec)
    edges = frozenset(str(e["id"]) for e in edges_spec)
    collision = kinds & NODE_KINDS
    if collision:
        raise PackRefused(
            f"pack {pack_id!r} declares kind(s) {sorted(collision)} that already belong to "
            "BOSS's core kinds; a pack may only add kinds, never collide with core."
        )
    kind_meta = {
        str(k["id"]): {"layer": k.get("layer"), "label": k.get("label"), "color": k.get("color"),
                      "attrs": list(k.get("attrs") or [])}
        for k in kinds_spec
    }
    return OntologyPack(
        pack_id=pack_id, version=str(document.get("version") or "1"),
        kinds=kinds, edges=edges, kind_meta=kind_meta,
    )


def load_pack(pack_id: str) -> OntologyPack:
    """Parse and validate the shipped pack ``pack_id``'s YAML. Raises PackRefused on a
    collision with a core kind, or if the file is not one of the shipped packs."""
    from ..yaml_files import load_mapping

    path = _ontology_root() / f"{pack_id}.yaml"
    if not path.exists():
        raise PackRefused(f"unknown ontology pack {pack_id!r}")
    document = load_mapping(path)
    return _parse_pack(pack_id, document)


_SHIPPED_CACHE: Optional[dict[str, OntologyPack]] = None


def shipped_packs() -> dict[str, OntologyPack]:
    """Every ontology pack BOSS ships, parsed once and validated against core kinds."""
    global _SHIPPED_CACHE
    if _SHIPPED_CACHE is None:
        _SHIPPED_CACHE = {pack_id: load_pack(pack_id) for pack_id in SHIPPED_PACK_IDS}
    return _SHIPPED_CACHE


#: {project_id: OntologyPack | None}, explicitly invalidated by set/rollback (FR8's "cache-bust"),
#: never trusted to expire on its own.
_PROJECT_PACK_CACHE: dict[str, Optional[OntologyPack]] = {}


def set_project_pack(
    connection: Connection, *, project_id: str, pack_id: str, version: Optional[str] = None,
    set_by: str = "system",
) -> OntologyPack:
    ensure_schema(connection)
    pack = shipped_packs().get(pack_id)
    if pack is None:
        raise PackRefused(f"unknown ontology pack {pack_id!r}; known: {SHIPPED_PACK_IDS}")
    connection.execute(
        text(
            """
            INSERT INTO kg_ontology_packs (project_id, pack_id, version, set_by, set_at)
            VALUES (:project_id, :pack_id, :version, :set_by, now())
            ON CONFLICT (project_id) DO UPDATE SET
              pack_id = EXCLUDED.pack_id, version = EXCLUDED.version,
              set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at
            """
        ),
        {"project_id": project_id, "pack_id": pack_id, "version": version or pack.version,
         "set_by": set_by},
    )
    _PROJECT_PACK_CACHE.pop(project_id, None)
    return pack


def rollback_project_pack(connection: Connection, *, project_id: str) -> None:
    ensure_schema(connection)
    connection.execute(
        text("DELETE FROM kg_ontology_packs WHERE project_id = :project_id"),
        {"project_id": project_id},
    )
    _PROJECT_PACK_CACHE.pop(project_id, None)


def get_project_pack(connection: Connection, *, project_id: str) -> Optional[OntologyPack]:
    if project_id in _PROJECT_PACK_CACHE:
        return _PROJECT_PACK_CACHE[project_id]
    ensure_schema(connection)
    row = connection.execute(
        text("SELECT pack_id FROM kg_ontology_packs WHERE project_id = :project_id"),
        {"project_id": project_id},
    ).mappings().first()
    pack = shipped_packs().get(str(row["pack_id"])) if row else None
    _PROJECT_PACK_CACHE[project_id] = pack
    return pack


def effective_node_kinds(connection: Connection, *, project_id: str) -> frozenset[str]:
    from .store import NODE_KINDS

    pack = get_project_pack(connection, project_id=project_id)
    return NODE_KINDS | (pack.kinds if pack else frozenset())


def effective_edge_kinds(connection: Connection, *, project_id: str) -> frozenset[str]:
    from .store import EDGE_KINDS

    pack = get_project_pack(connection, project_id=project_id)
    return EDGE_KINDS | (pack.edges if pack else frozenset())
