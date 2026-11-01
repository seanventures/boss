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

SHIPPED_PACK_IDS = ("banking-regulatory", "banking-dq", "banking-aml")

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
    #: The schema version of the context kinds this pack declares (graph_versions.cg_version);
    #: None means the pack's own version stands for it.
    context_version: Optional[str] = None

    def get(self, name: str, default: Any = None) -> Any:
        """Dict-like read of a field by name, for a caller that treats a pack as data."""
        return getattr(self, name, default)


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
    # A PACK MAY INCLUDE ANOTHER (banking-aml includes banking-regulatory): one project runs
    # both case studies, and a project has exactly one active pack, so switching to the AML pack
    # must not take the regulation's kinds away from it.
    for included_id in document.get("includes") or []:
        included_id = str(included_id)
        if included_id == pack_id:
            raise PackRefused(f"pack {pack_id!r} cannot include itself")
        included = load_pack(included_id)
        kinds |= included.kinds
        edges |= included.edges
        kind_meta = {**included.kind_meta, **kind_meta}
    return OntologyPack(
        pack_id=pack_id, version=str(document.get("version") or "1"),
        kinds=kinds, edges=edges, kind_meta=kind_meta,
        context_version=(str(document["context_version"]) if document.get("context_version") else None),
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


_LAYERS_SYNCED = False


def pack_context_kinds() -> frozenset[str]:
    """Every kind a shipped pack (or the core trace) puts in the Context Graph whose storage
    layer the core CASE does not already say: the kinds kg_kind_layers maps to 'context'."""
    from . import scopes
    from .view_context import kinds_in

    return frozenset(k for k in kinds_in("context") if scopes.core_layer_of(k) != "context")


def sync_kind_layers(connection: Connection, *, force: bool = False) -> dict[str, int]:
    """Keep ``kg_kind_layers`` equal to the packs' context kinds and backfill the stored
    ``knowledge_layer`` of existing nodes to match (the migration). Idempotent: a second call
    changes nothing. Once per process unless ``force``; a database without the table (an old
    schema) is left alone."""
    global _LAYERS_SYNCED
    if _LAYERS_SYNCED and not force:
        return {"kinds": 0, "nodes": 0}
    from . import scopes

    with connection.begin_nested():
        exists = connection.execute(text("SELECT to_regclass('kg_kind_layers') IS NOT NULL")).scalar()
        if not exists:
            return {"kinds": 0, "nodes": 0}
        wanted = {kind: "context" for kind in pack_context_kinds()}
        have = {str(k): str(v) for k, v in connection.execute(
            text("SELECT kind, layer FROM kg_kind_layers WHERE source = 'pack'")).all()}
        gone = sorted(set(have) - set(wanted))
        changed = sorted(k for k, layer in wanted.items() if have.get(k) != layer)
        if gone:
            connection.execute(text("DELETE FROM kg_kind_layers WHERE source = 'pack' AND kind = ANY(:k)"),
                               {"k": gone})
        for kind in changed:
            connection.execute(text(
                "INSERT INTO kg_kind_layers (kind, layer, source) VALUES (:k, :l, 'pack') "
                "ON CONFLICT (kind) DO UPDATE SET layer = EXCLUDED.layer, updated_at = now()"),
                {"k": kind, "l": wanted[kind]})
        nodes = 0
        touched = sorted(set(wanted) | set(gone))
        if touched:
            nodes = connection.execute(text(
                "UPDATE kg_nodes n SET knowledge_layer = COALESCE(l.layer, "
                + scopes.layer_sql_case("n.kind") + ") FROM (SELECT unnest(CAST(:k AS text[])) AS kind) t "
                "LEFT JOIN kg_kind_layers l ON l.kind = t.kind WHERE n.kind = t.kind "
                "AND n.knowledge_layer IS DISTINCT FROM COALESCE(l.layer, "
                + scopes.layer_sql_case("n.kind") + ")"), {"k": touched}).rowcount
    _LAYERS_SYNCED = True
    return {"kinds": len(changed) + len(gone), "nodes": int(nodes or 0)}


def effective_node_kinds(connection: Connection, *, project_id: str) -> frozenset[str]:
    from .store import NODE_KINDS

    pack = get_project_pack(connection, project_id=project_id)
    return NODE_KINDS | (pack.kinds if pack else frozenset())


def effective_edge_kinds(connection: Connection, *, project_id: str) -> frozenset[str]:
    from .store import EDGE_KINDS

    pack = get_project_pack(connection, project_id=project_id)
    return EDGE_KINDS | (pack.edges if pack else frozenset())
