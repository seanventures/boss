"""Ontology packs (KG Domain layer v1, A2): node/edge kinds beyond BOSS's core (Code, Rules,
Learning), declared per project from a versioned YAML pack under
``packages/specpack/ontology/``.

A PROJECT HOLDS SEVERAL PACKS AT ONCE (``kg_ontology_packs``, one row per project AND pack). Its
effective kind set is core ∪ every pack it holds (∪ what those packs include). NorthPeak Lending
runs the regulatory, AML and DQ case studies side by side: adding banking-dq must never take the
AML or regulation kinds away, which one-pack-per-project did (the table's key was project_id
alone, so setting a pack replaced the last one). Packs are additive; removing one is explicit
(``rollback_project_pack`` with that pack's id).

Where two packs a project holds declare the same kind (``system`` is in banking-regulatory and
banking-dq), the pack set FIRST keeps its words (layer, label, colour): adding a pack never
changes how kinds a project already had are shown or traced.

With no pack, a project's effective set is core-only, so writing a pack kind (``document``,
``clause``, …) is refused with a reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

SHIPPED_PACK_IDS = ("banking-regulatory", "banking-dq", "banking-aml")

#: New installs get the composite key. An install made while the key was project_id alone is
#: migrated in place by `_MIGRATE_SQL` (its one row per project stays, as that project's first
#: pack): the DO block reads the catalog and alters nothing when the key is already composite.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_ontology_packs (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  pack_id TEXT NOT NULL,
  version TEXT NOT NULL,
  set_by TEXT NOT NULL,
  set_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, pack_id)
)
"""
_MIGRATE_SQL = """
DO $$
BEGIN
  IF (SELECT count(*) FROM pg_index i JOIN pg_attribute a
        ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
      WHERE i.indrelid = 'kg_ontology_packs'::regclass AND i.indisprimary) = 1 THEN
    ALTER TABLE kg_ontology_packs DROP CONSTRAINT kg_ontology_packs_pkey;
    ALTER TABLE kg_ontology_packs ADD PRIMARY KEY (project_id, pack_id);
  END IF;
END $$
"""
#: How many columns the table's primary key has (0: no table yet). Read before any DDL, so a
#: booted install (db/schema.sql creates and migrates the table) runs none on a read.
_KEY_WIDTH_SQL = """
SELECT CASE WHEN to_regclass('kg_ontology_packs') IS NULL THEN 0 ELSE
  (SELECT count(*) FROM pg_index i JOIN pg_attribute a
     ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
   WHERE i.indrelid = to_regclass('kg_ontology_packs') AND i.indisprimary) END
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
    #: The Knowledge Graph version the pack's agent designs are written against ("KG v3"), when
    #: it declares one: the version the agent design views name, so a band and a design card
    #: never disagree (graph_versions.kg_reference).
    kg_reference: Optional[str] = None
    #: Every pack this one stands for: itself and what it includes (transitively). A project's
    #: composite pack lists every pack the project holds.
    pack_ids: tuple[str, ...] = field(default=())

    def get(self, name: str, default: Any = None) -> Any:
        """Dict-like read of a field by name, for a caller that treats a pack as data."""
        return getattr(self, name, default)

    def includes(self, pack_id: str) -> bool:
        return pack_id == self.pack_id or pack_id in self.pack_ids


def ensure_schema(connection: Connection) -> None:
    """Create the table, or move an install from one pack per project to several — only when
    the catalog says it is needed (db/schema.sql does both at boot; this is the fallback)."""
    width = int(connection.execute(text(_KEY_WIDTH_SQL)).scalar() or 0)
    if width == 0:
        connection.execute(text(_SCHEMA_SQL))
    elif width == 1:
        connection.exec_driver_sql(_MIGRATE_SQL)


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
                      "plural": k.get("plural"), "group": k.get("group"),
                      "attrs": list(k.get("attrs") or [])}
        for k in kinds_spec
    }
    pack_ids = [pack_id]
    # A PACK MAY INCLUDE ANOTHER (banking-aml includes banking-regulatory): its kinds come with it.
    for included_id in document.get("includes") or []:
        included_id = str(included_id)
        if included_id == pack_id:
            raise PackRefused(f"pack {pack_id!r} cannot include itself")
        included = load_pack(included_id)
        kinds |= included.kinds
        edges |= included.edges
        kind_meta = {**included.kind_meta, **kind_meta}
        pack_ids += [p for p in (included.pack_ids or (included_id,)) if p not in pack_ids]
    return OntologyPack(
        pack_id=pack_id, version=str(document.get("version") or "1"),
        kinds=kinds, edges=edges, kind_meta=kind_meta, pack_ids=tuple(pack_ids),
        context_version=(str(document["context_version"]) if document.get("context_version") else None),
        kg_reference=(str(document["knowledge_graph_reference"])
                      if document.get("knowledge_graph_reference") else None),
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
    """ADD ``pack_id`` to the project's packs (or refresh its version). Additive: every pack the
    project already holds stays, so its kinds stay writable. Returns the pack added."""
    ensure_schema(connection)
    pack = shipped_packs().get(pack_id)
    if pack is None:
        raise PackRefused(f"unknown ontology pack {pack_id!r}; known: {SHIPPED_PACK_IDS}")
    connection.execute(
        text(
            """
            INSERT INTO kg_ontology_packs (project_id, pack_id, version, set_by, set_at)
            VALUES (:project_id, :pack_id, :version, :set_by, clock_timestamp())
            ON CONFLICT (project_id, pack_id) DO UPDATE SET
              version = EXCLUDED.version, set_by = EXCLUDED.set_by
            """
        ),
        {"project_id": project_id, "pack_id": pack_id, "version": version or pack.version,
         "set_by": set_by},
    )
    _PROJECT_PACK_CACHE.pop(project_id, None)
    return pack


def rollback_project_pack(connection: Connection, *, project_id: str,
                          pack_id: Optional[str] = None) -> None:
    """Remove one pack from the project (``pack_id``), or every pack when none is named."""
    ensure_schema(connection)
    if pack_id:
        connection.execute(
            text("DELETE FROM kg_ontology_packs WHERE project_id = :project_id "
                 "AND pack_id = :pack_id"),
            {"project_id": project_id, "pack_id": pack_id},
        )
    else:
        connection.execute(
            text("DELETE FROM kg_ontology_packs WHERE project_id = :project_id"),
            {"project_id": project_id},
        )
    _PROJECT_PACK_CACHE.pop(project_id, None)


def project_pack_ids(connection: Connection, *, project_id: str) -> list[str]:
    """The packs the project holds, in the order they were set (not what they include)."""
    ensure_schema(connection)
    return [str(r) for r in connection.execute(
        text("SELECT pack_id FROM kg_ontology_packs WHERE project_id = :project_id "
             "ORDER BY set_at, pack_id"),
        {"project_id": project_id},
    ).scalars().all()]


def _composite(packs: list[OntologyPack]) -> Optional[OntologyPack]:
    """One pack standing for several: kinds and edges unioned; for a kind two packs declare, the
    pack set first keeps its words. Named after the pack set last (the one a person just added),
    so a caller asking "which pack" hears the newest."""
    if not packs:
        return None
    if len(packs) == 1:
        return packs[0]
    kinds: frozenset[str] = frozenset()
    edges: frozenset[str] = frozenset()
    meta: dict[str, dict[str, Any]] = {}
    ids: list[str] = []
    for pack in packs:
        kinds |= pack.kinds
        edges |= pack.edges
        for kid, kmeta in pack.kind_meta.items():
            meta.setdefault(kid, kmeta)
        ids += [p for p in (pack.pack_ids or (pack.pack_id,)) if p not in ids]
    newest = packs[-1]
    # The Context Graph schema version: the newest pack that declares one.
    context_version = next((p.context_version for p in reversed(packs) if p.context_version),
                           None)
    kg_reference = next((p.kg_reference for p in reversed(packs) if p.kg_reference), None)
    return OntologyPack(pack_id=newest.pack_id, version=newest.version, kinds=kinds,
                        edges=edges, kind_meta=meta, pack_ids=tuple(ids),
                        context_version=context_version, kg_reference=kg_reference)


def get_project_packs(connection: Connection, *, project_id: str) -> list[OntologyPack]:
    """The packs the project holds, each on its own, in the order they were set."""
    shipped = shipped_packs()
    return [shipped[p] for p in project_pack_ids(connection, project_id=project_id)
            if p in shipped]


def get_project_pack(connection: Connection, *, project_id: str) -> Optional[OntologyPack]:
    """Every pack the project holds, as one composite (None with no pack)."""
    if project_id in _PROJECT_PACK_CACHE:
        return _PROJECT_PACK_CACHE[project_id]
    pack = _composite(get_project_packs(connection, project_id=project_id))
    _PROJECT_PACK_CACHE[project_id] = pack
    return pack


def has_pack(connection: Connection, *, project_id: str, pack_id: str) -> bool:
    """The project holds ``pack_id``, directly or through a pack that includes it."""
    pack = get_project_pack(connection, project_id=project_id)
    return pack is not None and pack.includes(pack_id)


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
