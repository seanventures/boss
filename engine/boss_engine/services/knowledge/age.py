"""The Apache AGE projection of the knowledge graph, and read-only Cypher over it.

Operator decision 2026-10-22. ``kg_nodes`` / ``kg_edges`` stay the source of truth, and packs and
tools keep traversing with the recursive CTE (measured faster). AGE holds a projection:

* **One graph per project** (``graph_name``), so scope is structural: a Cypher query runs inside
  one project's graph and cannot name another.
* A vertex's label is its knowledge layer (``Code``, ``Lifecycle``, ``Ops``, ``Knowledge``,
  ``Rules``), so the code layer is its own label and a stage scope can refuse it (or rules) by
  name. Every vertex carries its ``product_layer`` (code, rules, learning; scopes.product_layer)
  and a rule vertex its ``status`` (proposed, approved, rejected, superseded). An edge's label is its
  kind. Properties carry the node id, kind, key, name and trust tier, and where the node has
  them its repo, path, line, lang, symbol_kind (function/method/class/property) and type_kind
  (class/interface/enum/record/struct). An edge carries its kind, trust tier and confidence.
* **Incremental**: ``after_write`` re-projects what changed since the last sync (the knowledge
  write paths call it); a large change, or a first sync, rebuilds the graph. ``forget_vertex``
  drops a deleted node at once. ``reconcile`` (the worker's 10-minute sweep) rebuilds a graph
  whose counts drifted from the tables.

``run_cypher`` is the read path: writes refused (a strict keyword check, and the statement runs
in a READ ONLY transaction), a statement timeout, a row limit, and under a stage scope the
``Code`` label refused in words and every node pattern labelled with a layer the stage reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import now_utc
from .scopes import LAYER_KINDS, StagePolicy, layer_of, product_layer

LABELS = {"code": "Code", "lifecycle": "Lifecycle", "ops": "Ops", "knowledge": "Knowledge",
          "rules": "Rules"}
#: The shape of what a projection writes (labels and vertex properties). Raise it when that shape
#: changes: a graph projected with an older shape is rebuilt by the next reconcile or sync, even
#: when its counts match the tables. 1: the Rules label, product_layer and a rule's status.
PROJECTION_SHAPE = 1
REBUILD_ABOVE = 2000                # changed nodes: past this, rebuild instead of patching
DEFAULT_TIMEOUT_MS = 5000
DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 1000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_age_sync (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  graph TEXT NOT NULL,
  synced_at TIMESTAMPTZ NOT NULL,
  nodes INTEGER NOT NULL DEFAULT 0,
  edges INTEGER NOT NULL DEFAULT 0,
  shape INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kg_age_vertices (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL,
  graphid TEXT NOT NULL,
  PRIMARY KEY (project_id, node_id)
)
"""
_WRITE_WORDS = re.compile(r"(?i)\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD|CALL|FOREACH|COPY)\b")
_STRINGS = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_NODE = re.compile(r"(?<![\w`])\(\s*([A-Za-z_]\w*)?\s*(?::\s*`?([A-Za-z_]\w*)`?)?\s*(\{[^{}]*\})?\s*\)")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")
_TRAILING_LIMIT = re.compile(r"(?i)\bLIMIT\s+(\d+)\s*;?\s*$")


# --- availability and names -------------------------------------------------------------------
def extension_version(connection: Connection) -> str:
    row = connection.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'age'")).scalar()
    return str(row) if row else "not_installed"


def available(connection: Connection) -> bool:
    return extension_version(connection) != "not_installed"


def graph_name(project_id: str) -> str:
    """A plain identifier per project; the project id itself never reaches SQL text."""
    return "kg_" + hashlib.sha256(str(project_id).encode("utf-8")).hexdigest()[:24]


def _prepare(connection: Connection) -> None:
    connection.execute(text("LOAD 'age'"))
    for stmt in _SCHEMA.split(";"):
        if stmt.strip():
            connection.execute(text(stmt))


def _graph_exists(connection: Connection, name: str) -> bool:
    return connection.execute(text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = :n"),
                              {"n": name}).first() is not None


def _labels(connection: Connection, name: str) -> set[str]:
    return set(connection.execute(text(
        "SELECT l.name FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        "WHERE g.name = :n"), {"n": name}).scalars().all())


def _ensure_label(connection: Connection, name: str, label: str, kind: str, have: set[str]) -> None:
    if label in have:
        return
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", label):
        raise ValueError(f"label {label!r} is not a plain identifier")
    fn = "create_vlabel" if kind == "v" else "create_elabel"
    connection.execute(text(f"SELECT ag_catalog.{fn}(:g, :l)"), {"g": name, "l": label})
    have.add(label)


# --- indexes ------------------------------------------------------------------------------------
#: AGE gives each label its own table, inheriting from the graph's base tables, and indexes only
#: the base tables (which hold no rows). Without these a label table has no index at all: every
#: hop of a variable-length pattern scanned the whole vertex table (Fineract, 51k vertices:
#: `(c)-[:extends*1..3]->(a)-[:implements]->(i)` 12.7 s, 0.2 s with them), and a property match
#: scanned every vertex. Vertex: id, properties (GIN, for `{name: ...}` maps), name and node_id
#: (for `WHERE n.name = ...`). Edge: id, start_id, end_id.
_VERTEX_INDEXES = {
    "id": "USING btree (id)",
    "props": "USING gin (properties)",
    "name": "USING btree (ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, '\"name\"'::ag_catalog.agtype]))",
    "node_id": "USING btree (ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, '\"node_id\"'::ag_catalog.agtype]))",
}
_EDGE_INDEXES = {"id": "USING btree (id)", "start": "USING btree (start_id)", "end": "USING btree (end_id)"}


def _index_name(label: str, what: str) -> str:
    return "kgix_" + hashlib.sha256(f"{label}|{what}".encode("utf-8")).hexdigest()[:16]


def index_graph(connection: Connection, name: str, *, analyze: bool = False) -> int:
    """Give every label table of graph ``name`` its indexes; idempotent. A table that gains an
    index (a new label), or every table when ``analyze``, is ANALYZEd: a graph just written has
    no statistics, and the planner then nested-looped a one-hop match past the time limit."""
    labels = connection.execute(text(
        "SELECT l.name, l.kind FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        "WHERE g.name = :n AND l.name NOT LIKE '\\_ag\\_label\\_%'"), {"n": name}).all()
    have = set(connection.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = :n"),
                                  {"n": name}).scalars().all())
    created = 0
    for label, kind in labels:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", label):
            continue
        fresh = False
        for what, spec in (_VERTEX_INDEXES if kind == "v" else _EDGE_INDEXES).items():
            index = _index_name(label, what)
            if index not in have:
                connection.execute(text(f'CREATE INDEX IF NOT EXISTS {index} ON "{name}"."{label}" {spec}'))
                created += 1
                fresh = True
        if fresh or analyze:
            connection.execute(text(f'ANALYZE "{name}"."{label}"'))
    return created


# --- projection ---------------------------------------------------------------------------------
def _node_rows(connection: Connection, project_id: str, ids: list[str] | None = None) -> list[dict]:
    where = "" if ids is None else "AND id = ANY(:ids)"
    return [dict(r) for r in connection.execute(text(
        f"""
        SELECT id, kind, node_key, name, trust_tier, repo, path, attrs->>'symbol_kind' AS symbol_kind,
               attrs->>'type_kind' AS type_kind, attrs->>'lang' AS lang, attrs->>'line' AS line,
               CASE WHEN kind = 'rule' THEN attrs->>'status' END AS status
        FROM kg_nodes
        WHERE project_id = :p AND (valid_to IS NULL OR valid_to > now()) {where}
        """), {"p": project_id, "ids": list(ids or [])}).mappings()]


def _edge_rows(connection: Connection, project_id: str, where: str = "", params: dict | None = None) -> list[dict]:
    return [dict(r) for r in connection.execute(text(
        f"""
        SELECT e.id, e.src_id, e.dst_id, e.kind, e.trust_tier, e.confidence FROM kg_edges e
        JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id
        WHERE e.project_id = :p AND (e.valid_to IS NULL OR e.valid_to > now())
          AND (s.valid_to IS NULL OR s.valid_to > now()) AND (d.valid_to IS NULL OR d.valid_to > now())
          {where}
        """), {"p": project_id, **(params or {})}).mappings()]


#: Optional vertex properties, copied when the node has them (a code node's location and shape).
VERTEX_EXTRAS = ("repo", "path", "line", "lang", "symbol_kind", "type_kind", "status")


def _vertex_props(node: dict) -> dict:
    props = {"node_id": node["id"], "kind": node["kind"], "node_key": node["node_key"],
             "name": node["name"], "trust_tier": node["trust_tier"]}
    layer = product_layer(node["kind"])
    if layer:
        props["product_layer"] = layer
    for key in VERTEX_EXTRAS:
        value = node.get(key)
        if value not in (None, ""):
            props[key] = int(value) if key == "line" and str(value).isdigit() else value
    return props


def _insert_vertices(connection: Connection, project_id: str, name: str, nodes: list[dict]) -> int:
    have = _labels(connection, name)
    by_label: dict[str, list[dict]] = {}
    for node in nodes:
        by_label.setdefault(LABELS[layer_of(node["kind"])], []).append(node)
    written = 0
    for label, group in by_label.items():
        _ensure_label(connection, name, label, "v", have)
        props = [json.dumps(_vertex_props(n)) for n in group]
        for start in range(0, len(props), 2000):
            rows = connection.execute(text(
                f'INSERT INTO "{name}"."{label}" (properties) '
                "SELECT CAST(p AS ag_catalog.agtype) FROM unnest(CAST(:props AS TEXT[])) AS p "
                "RETURNING id::text, properties::text"), {"props": props[start:start + 2000]}).all()
            mapping = [(json.loads(p)["node_id"], gid) for gid, p in rows]
            connection.execute(text(
                "INSERT INTO kg_age_vertices (project_id, node_id, graphid) "
                "SELECT :p, n, g FROM unnest(CAST(:nodes AS TEXT[]), CAST(:gids AS TEXT[])) AS u(n, g) "
                "ON CONFLICT (project_id, node_id) DO UPDATE SET graphid = EXCLUDED.graphid"),
                {"p": project_id, "nodes": [m[0] for m in mapping], "gids": [m[1] for m in mapping]})
            written += len(rows)
    return written


def _insert_edges(connection: Connection, project_id: str, name: str, edges: list[dict]) -> int:
    have = _labels(connection, name)
    by_label: dict[str, list[dict]] = {}
    for edge in edges:
        by_label.setdefault(edge["kind"], []).append(edge)
    written = 0
    for label, group in by_label.items():
        _ensure_label(connection, name, label, "e", have)
        for start in range(0, len(group), 5000):
            part = group[start:start + 5000]
            written += connection.execute(text(
                f'INSERT INTO "{name}"."{label}" (start_id, end_id, properties) '
                "SELECT CAST(s.graphid AS ag_catalog.graphid), CAST(d.graphid AS ag_catalog.graphid), "
                "CAST(u.props AS ag_catalog.agtype) "
                "FROM unnest(CAST(:srcs AS TEXT[]), CAST(:dsts AS TEXT[]), CAST(:props AS TEXT[])) AS u(src, dst, props) "
                "JOIN kg_age_vertices s ON s.project_id = :p AND s.node_id = u.src "
                "JOIN kg_age_vertices d ON d.project_id = :p AND d.node_id = u.dst"),
                {"p": project_id, "srcs": [e["src_id"] for e in part], "dsts": [e["dst_id"] for e in part],
                 "props": [json.dumps({"edge_id": e["id"], "kind": e["kind"], "trust_tier": e["trust_tier"],
                                       "confidence": float(e.get("confidence") if e.get("confidence") is not None else 1.0)})
                           for e in part]}).rowcount
    return written


def _project_all_edges(connection: Connection, project_id: str, name: str) -> int:
    """Every current edge into a fresh graph, one statement per relationship type, inside the
    database: a whole repository's edges (Fineract: 234k) never travel to Python and back.
    Same rows and properties as _insert_edges over _edge_rows."""
    have = _labels(connection, name)
    kinds = connection.execute(text(
        "SELECT DISTINCT kind FROM kg_edges WHERE project_id = :p AND (valid_to IS NULL OR valid_to > now())"),
        {"p": project_id}).scalars().all()
    written = 0
    for kind in sorted(kinds):
        _ensure_label(connection, name, kind, "e", have)
        written += connection.execute(text(
            f'INSERT INTO "{name}"."{kind}" (start_id, end_id, properties) '
            "SELECT CAST(sv.graphid AS ag_catalog.graphid), CAST(dv.graphid AS ag_catalog.graphid), "
            "CAST('{\"edge_id\": ' || to_json(e.id)::text || ', \"kind\": ' || to_json(e.kind)::text "
            "|| ', \"trust_tier\": ' || to_json(e.trust_tier)::text || ', \"confidence\": ' "
            "|| CASE WHEN e.confidence = trunc(e.confidence) THEN trunc(e.confidence)::bigint::text || '.0' "
            "ELSE e.confidence::text END || '}' AS ag_catalog.agtype) "
            "FROM kg_edges e "
            "JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id "
            "JOIN kg_age_vertices sv ON sv.project_id = :p AND sv.node_id = e.src_id "
            "JOIN kg_age_vertices dv ON dv.project_id = :p AND dv.node_id = e.dst_id "
            "WHERE e.project_id = :p AND e.kind = :k AND (e.valid_to IS NULL OR e.valid_to > now()) "
            "AND (s.valid_to IS NULL OR s.valid_to > now()) AND (d.valid_to IS NULL OR d.valid_to > now())"),
            {"p": project_id, "k": kind}).rowcount
    return written


def _mark(connection: Connection, project_id: str, name: str, synced_at) -> dict[str, int]:
    counts = projection_counts(connection, project_id)
    connection.execute(text(
        "INSERT INTO kg_age_sync (project_id, graph, synced_at, nodes, edges, shape) "
        "VALUES (:p, :g, :t, :n, :e, :s) "
        "ON CONFLICT (project_id) DO UPDATE SET graph = EXCLUDED.graph, synced_at = EXCLUDED.synced_at, "
        "nodes = EXCLUDED.nodes, edges = EXCLUDED.edges, shape = EXCLUDED.shape"),
        {"p": project_id, "g": name, "t": synced_at, "n": counts["nodes"], "e": counts["edges"],
         "s": PROJECTION_SHAPE})
    return counts


def projection_counts(connection: Connection, project_id: str) -> dict[str, int]:
    name = graph_name(project_id)
    if not _graph_exists(connection, name):
        return {"nodes": 0, "edges": 0}
    return {"nodes": int(connection.execute(text(f'SELECT COUNT(*) FROM "{name}"._ag_label_vertex')).scalar_one()),
            "edges": int(connection.execute(text(f'SELECT COUNT(*) FROM "{name}"._ag_label_edge')).scalar_one())}


def rebuild(connection: Connection, project_id: str) -> dict[str, Any]:
    """Drop and rebuild one project's graph from the tables."""
    if not available(connection):
        raise RuntimeError("apache age is not installed")
    _prepare(connection)
    synced_at = now_utc()
    name = graph_name(project_id)
    if _graph_exists(connection, name):
        connection.execute(text("SELECT ag_catalog.drop_graph(:g, true)"), {"g": name})
    connection.execute(text("SELECT ag_catalog.create_graph(:g)"), {"g": name})
    connection.execute(text("DELETE FROM kg_age_vertices WHERE project_id = :p"), {"p": project_id})
    nodes = _insert_vertices(connection, project_id, name, _node_rows(connection, project_id))
    edges = _project_all_edges(connection, project_id, name)
    index_graph(connection, name, analyze=True)
    _mark(connection, project_id, name, synced_at)
    return {"graph": name, "mode": "rebuild", "nodes": nodes, "edges": edges}


def _drop_vertices(connection: Connection, project_id: str, name: str, node_ids: list[str]) -> None:
    gids = connection.execute(text(
        "SELECT graphid FROM kg_age_vertices WHERE project_id = :p AND node_id = ANY(:ids)"),
        {"p": project_id, "ids": node_ids}).scalars().all()
    if gids:
        connection.execute(text(
            f'DELETE FROM "{name}"._ag_label_edge WHERE start_id::text = ANY(:g) OR end_id::text = ANY(:g)'),
            {"g": list(gids)})
        connection.execute(text(f'DELETE FROM "{name}"._ag_label_vertex WHERE id::text = ANY(:g)'),
                           {"g": list(gids)})
    connection.execute(text("DELETE FROM kg_age_vertices WHERE project_id = :p AND node_id = ANY(:ids)"),
                       {"p": project_id, "ids": node_ids})


def sync(connection: Connection, project_id: str) -> dict[str, Any]:
    """Re-project what changed since the last sync; rebuild on a first sync or a large change."""
    if not available(connection):
        return {"mode": "unavailable"}
    _prepare(connection)
    name = graph_name(project_id)
    row = connection.execute(text("SELECT synced_at, shape FROM kg_age_sync WHERE project_id = :p"),
                             {"p": project_id}).first()
    last = row[0] if row else None
    if last is None or not _graph_exists(connection, name) or int(row[1] or 0) != PROJECTION_SHAPE:
        return rebuild(connection, project_id)
    # A margin for the application's clock against the database's: re-projecting twice is harmless.
    since = last - timedelta(seconds=5)
    synced_at = now_utc()
    changed = connection.execute(text(
        """
        SELECT id FROM kg_nodes WHERE project_id = :p AND (
          last_seen >= :t OR recorded_at >= :t OR first_seen >= :t OR (valid_to IS NOT NULL AND valid_to >= :t))
        """), {"p": project_id, "t": since}).scalars().all()
    if len(changed) > REBUILD_ABOVE:
        return rebuild(connection, project_id)
    changed = list(changed)
    if changed:
        _drop_vertices(connection, project_id, name, changed)
        _insert_vertices(connection, project_id, name, _node_rows(connection, project_id, changed))
    edge_changes = connection.execute(text(
        """
        SELECT id FROM kg_edges WHERE project_id = :p AND (
          last_seen >= :t OR recorded_at >= :t OR first_seen >= :t OR (valid_to IS NOT NULL AND valid_to >= :t))
        """), {"p": project_id, "t": since}).scalars().all()
    if len(edge_changes) > 4 * REBUILD_ABOVE:
        return rebuild(connection, project_id)
    if edge_changes:
        # A plain property map reads as JSON text, so one statement removes every changed edge.
        connection.execute(text(
            f'DELETE FROM "{name}"._ag_label_edge '
            "WHERE CAST(CAST(properties AS text) AS jsonb)->>'edge_id' = ANY(:ids)"),
            {"ids": list(edge_changes)})
    edges = _insert_edges(connection, project_id, name, _edge_rows(
        connection, project_id,
        "AND (e.src_id = ANY(:ids) OR e.dst_id = ANY(:ids) OR e.id = ANY(:eids))",
        {"ids": changed, "eids": list(edge_changes)})) if (changed or edge_changes) else 0
    index_graph(connection, name)            # a label this sync created gets its indexes
    counts = _mark(connection, project_id, name, synced_at)
    return {"graph": name, "mode": "incremental", "nodes_changed": len(changed),
            "edges_written": edges, **counts}


def after_write(connection: Connection, project_id: str) -> None:
    """Keep the projection current after a knowledge write. Never fails the write."""
    try:
        if not available(connection):
            return
        with connection.begin_nested():
            sync(connection, project_id)
    except Exception:
        import logging

        logging.getLogger(__name__).warning("knowledge_age_sync_failed", exc_info=True)
        if os.environ.get("BOSS_KG_AGE_STRICT"):
            raise


def forget_vertex(connection: Connection, project_id: str, node_id: str) -> None:
    """Drop a deleted node from the projection at once (a forget must not linger in Cypher)."""
    try:
        if not available(connection):
            return
        with connection.begin_nested():
            _prepare(connection)
            name = graph_name(project_id)
            if _graph_exists(connection, name):
                _drop_vertices(connection, project_id, name, [node_id])
    except Exception:
        import logging

        logging.getLogger(__name__).warning("knowledge_age_forget_failed", exc_info=True)


def reconcile(connection: Connection, project_id: str) -> dict[str, Any]:
    """Rebuild a projection whose counts drifted from the tables (the worker's sweep)."""
    if not available(connection):
        return {"mode": "unavailable"}
    _prepare(connection)
    want = {"nodes": len(_node_rows(connection, project_id)), "edges": len(_edge_rows(connection, project_id))}
    have = projection_counts(connection, project_id)
    shape = connection.execute(text("SELECT shape FROM kg_age_sync WHERE project_id = :p"),
                               {"p": project_id}).scalar()
    if have == want and int(shape or 0) == PROJECTION_SHAPE:
        return {"mode": "current", **have}
    return rebuild(connection, project_id)


# --- the CTE comparison PR #15 shipped ------------------------------------------------------------
def project_graph(connection: Connection, store: Any = None, *, project_id: str, graph: str = "") -> dict[str, Any]:
    """Rebuild the project's projection (kept for the CTE comparison; ``graph`` is ignored)."""
    if not available(connection):
        raise RuntimeError("apache age is not installed")
    return rebuild(connection, project_id)


def age_neighbors(connection: Connection, *, project_id: str, start_id: str, max_depth: int,
                  graph: str = "") -> list[dict[str, Any]]:
    """Directed neighbors from the projection, at their shortest depth (compare traverse())."""
    if not available(connection):
        raise RuntimeError("apache age is not installed")
    depth = int(max_depth)
    if depth < 1 or depth > 8:
        raise ValueError("max_depth must be from 1 to 8")
    rows = run_cypher(connection, project_id=project_id, columns=["node_id", "hops"], row_limit=MAX_ROW_LIMIT,
                      query=f"MATCH p = (a {{node_id: $start}})-[*1..{depth}]->(b) "
                            "RETURN b.node_id, length(p)", params={"start": start_id})["rows"]
    best: dict[str, int] = {}
    for row in rows:
        node_id, hop = str(row["node_id"]), int(row["hops"])
        if node_id not in best or hop < best[node_id]:
            best[node_id] = hop
    return [{"node_id": n, "depth": d} for n, d in sorted(best.items(), key=lambda item: (item[1], item[0]))]


# --- read-only Cypher ---------------------------------------------------------------------------
def check_read_only(query: str) -> None:
    stripped = _STRINGS.sub("''", query or "")
    found = _WRITE_WORDS.search(stripped)
    if found:
        raise ValueError(f"Cypher here is read-only: {found.group(1).upper()} is refused.")


def check_scope(query: str, policy: StagePolicy | None) -> None:
    """Under a stage scope: the Code label is refused in words (code is read through the seeded
    tools, never by a whole-graph query), every node pattern names a layer the stage reads, and
    a variable-length pattern (which could walk through code) is refused."""
    if policy is None:
        return
    stripped = _STRINGS.sub("''", query)
    allowed = {LABELS[layer] for layer in policy.layers if layer != "code"}
    if re.search(r":\s*`?Code\b", stripped):
        raise PermissionError(policy.refusal())
    if re.search(r"\[[^\]]*\*[^\]]*\]", stripped):
        raise PermissionError(f"A variable-length pattern isn't available in {policy.stage}: "
                              "name each hop between labelled nodes.")
    labelled: set[str] = set()
    for var, label, _props in _NODE.findall(stripped):
        if label:
            if label not in allowed:
                raise PermissionError(f"The {label} label isn't available in {policy.stage}; "
                                      f"it reads {', '.join(sorted(allowed))}.")
            if var:
                labelled.add(var)
        elif not var or var not in labelled:
            raise PermissionError(f"In {policy.stage} every node in a pattern names its label "
                                  f"({', '.join(sorted(allowed))}).")


def run_cypher(
    connection: Connection,
    *,
    project_id: str,
    query: str,
    params: dict | None = None,
    columns: list[str] | None = None,
    policy: StagePolicy | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> dict[str, Any]:
    """One read-only Cypher query over the project's graph, in a READ ONLY savepoint with a
    statement timeout and a row limit."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is required")
    check_read_only(query)
    check_scope(query, policy)
    cols = list(columns or ["result"])
    if not cols or len(cols) > 12 or not all(_COLUMN.fullmatch(c) for c in cols):
        raise ValueError("columns must be 1 to 12 plain names, one per RETURN item")
    limit = max(1, min(int(row_limit), MAX_ROW_LIMIT))
    timeout = max(1, min(int(timeout_ms), DEFAULT_TIMEOUT_MS))
    if not available(connection):
        raise RuntimeError("apache age is not installed")
    name = graph_name(project_id)
    # The query's own trailing LIMIT is asked for one row more, so the result can say whether
    # more matched: a model read a LIMIT 50 sample as "the only ones" (exam, 2026-10-24).
    query_limit = None
    # Positions from the query itself; the string-blanked copy only confirms the LIMIT is not
    # inside a string literal.
    trailing = _TRAILING_LIMIT.search(query) if _TRAILING_LIMIT.search(_STRINGS.sub("''", query)) else None
    if trailing:
        query_limit = int(trailing.group(1))
        query = query[:trailing.start()] + f"LIMIT {query_limit + 1}"
    tag = "$kgq" + secrets.token_hex(6) + "$"
    if tag in query:
        raise ValueError("query is refused")
    statement = (
        f"SELECT * FROM ag_catalog.cypher('{name}', {tag}{query.replace('%', '%%')}{tag}, "
        f"CAST(%(params)s AS ag_catalog.agtype)) AS ({', '.join(f'{c} ag_catalog.agtype' for c in cols)}) "
        f"LIMIT {limit + 1}")
    from sqlalchemy.exc import DBAPIError

    if not _graph_exists(connection, name):
        raise LookupError("this project has no graph yet; it is built on the next knowledge write")
    # A savepoint that is always rolled back: inside it the transaction is READ ONLY and the
    # statement has a timeout, and rolling back restores both settings for the caller.
    savepoint = connection.begin_nested()
    try:
        connection.exec_driver_sql("SET LOCAL transaction_read_only = on")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout}")
        # Indexed graph lookups gain nothing from parallel workers or JIT (JIT alone added ~0.1 s
        # per query), and a parallel hash join's shared memory fails a database container's
        # small /dev/shm outright ("could not resize shared memory segment").
        connection.exec_driver_sql("SET LOCAL max_parallel_workers_per_gather = 0")
        connection.exec_driver_sql("SET LOCAL jit = off")
        connection.exec_driver_sql("LOAD 'age'")
        # AGE's operators (agtype and graphid comparisons) resolve through ag_catalog.
        connection.exec_driver_sql('SET LOCAL search_path = ag_catalog, "$user", public')
        rows = connection.exec_driver_sql(statement, {"params": json.dumps(params or {})}).all()
    except DBAPIError as error:
        code = getattr(getattr(error, "orig", None), "sqlstate", "")
        if code == "57014":
            raise TimeoutError(f"the query ran past {timeout} ms") from error
        if code == "25006":
            raise ValueError("Cypher here is read-only.") from error
        said = str(getattr(error, "orig", error)).splitlines()[0]
        raise ValueError(f"the query did not run: {said}{_dialect_hint(query)}") from error
    finally:
        savepoint.rollback()
    cap = min(limit, query_limit) if query_limit is not None else limit
    out = [{c: _ag(v) for c, v in zip(cols, row)} for row in rows[:cap]]
    result = {"columns": cols, "rows": out, "rows_returned": len(out), "row_limit": limit,
              "query_limit": query_limit, "truncated": len(rows) > cap, "graph": name}
    if result["truncated"]:
        by = f"the query's LIMIT {query_limit}" if query_limit is not None and query_limit <= limit \
            else f"the row limit of {limit}"
        result["truncated_by"] = by
        result["note"] = (f"TRUNCATED: {len(out)} rows returned and more matched ({by}). This is a sample, "
                          "not every match: absence from it proves nothing. Count with RETURN count(*), "
                          "narrow the pattern, or raise the limit.")
    return result


#: What Apache AGE's Cypher does not have, said where a query trips on it. General: about the
#: dialect, never about one graph.
DIALECT_NOTES = (
    "Apache AGE Cypher: no relationship-type alternation [:a|b] and no all()/any()/none() "
    "predicates; match each relationship type in its own pattern and combine with UNION, or chain "
    "them, e.g. (c)-[:extends*0..3]->()-[:implements]->(i).",
    "Each RETURN item needs its own name in columns.",
)
_ALTERNATION = re.compile(r"\[[^\]]*:[^\]]*\|[^\]]*\]")
_QUANTIFIER = re.compile(r"(?i)\b(?:all|any|none|single)\s*\(")


def _dialect_hint(query: str) -> str:
    stripped = _STRINGS.sub("''", query)
    if _ALTERNATION.search(stripped) or _QUANTIFIER.search(stripped):
        return " — " + DIALECT_NOTES[0]
    return ""


def _ag(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value)
    for suffix in ("::vertex", "::edge", "::path"):
        raw = raw.replace(suffix, "")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip('"')


__all__ = [
    "LABELS",
    "LAYER_KINDS",
    "after_write",
    "age_neighbors",
    "available",
    "check_read_only",
    "check_scope",
    "extension_version",
    "forget_vertex",
    "graph_name",
    "index_graph",
    "project_graph",
    "rebuild",
    "reconcile",
    "run_cypher",
    "sync",
]
