"""The Apache AGE projection of the knowledge graph, and read-only Cypher over it.

Operator decision 2026-10-22. ``kg_nodes`` / ``kg_edges`` stay the source of truth, and packs and
tools keep traversing with the recursive CTE (measured faster). AGE holds a projection:

* **One graph per project** (``graph_name``), so scope is structural: a Cypher query runs inside
  one project's graph and cannot name another.
* A vertex's label is its knowledge layer (``Code``, ``Lifecycle``, ``Ops``, ``Knowledge``), so
  the code layer is its own label and a stage scope can refuse it by name. An edge's label is its
  kind. Properties carry the node id, kind, key, name and trust tier.
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
from .scopes import LAYER_KINDS, StagePolicy, layer_of

LABELS = {"code": "Code", "lifecycle": "Lifecycle", "ops": "Ops", "knowledge": "Knowledge"}
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
  edges INTEGER NOT NULL DEFAULT 0
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


# --- projection ---------------------------------------------------------------------------------
def _node_rows(connection: Connection, project_id: str, ids: list[str] | None = None) -> list[dict]:
    where = "" if ids is None else "AND id = ANY(:ids)"
    return [dict(r) for r in connection.execute(text(
        f"""
        SELECT id, kind, node_key, name, trust_tier FROM kg_nodes
        WHERE project_id = :p AND (valid_to IS NULL OR valid_to > now()) {where}
        """), {"p": project_id, "ids": list(ids or [])}).mappings()]


def _edge_rows(connection: Connection, project_id: str, where: str = "", params: dict | None = None) -> list[dict]:
    return [dict(r) for r in connection.execute(text(
        f"""
        SELECT e.id, e.src_id, e.dst_id, e.kind, e.trust_tier FROM kg_edges e
        JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id
        WHERE e.project_id = :p AND (e.valid_to IS NULL OR e.valid_to > now())
          AND (s.valid_to IS NULL OR s.valid_to > now()) AND (d.valid_to IS NULL OR d.valid_to > now())
          {where}
        """), {"p": project_id, **(params or {})}).mappings()]


def _insert_vertices(connection: Connection, project_id: str, name: str, nodes: list[dict]) -> int:
    have = _labels(connection, name)
    by_label: dict[str, list[dict]] = {}
    for node in nodes:
        by_label.setdefault(LABELS[layer_of(node["kind"])], []).append(node)
    written = 0
    for label, group in by_label.items():
        _ensure_label(connection, name, label, "v", have)
        props = [json.dumps({"node_id": n["id"], "kind": n["kind"], "node_key": n["node_key"],
                             "name": n["name"], "trust_tier": n["trust_tier"]}) for n in group]
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
                 "props": [json.dumps({"edge_id": e["id"], "kind": e["kind"], "trust_tier": e["trust_tier"]})
                           for e in part]}).rowcount
    return written


def _mark(connection: Connection, project_id: str, name: str, synced_at) -> dict[str, int]:
    counts = projection_counts(connection, project_id)
    connection.execute(text(
        "INSERT INTO kg_age_sync (project_id, graph, synced_at, nodes, edges) VALUES (:p, :g, :t, :n, :e) "
        "ON CONFLICT (project_id) DO UPDATE SET graph = EXCLUDED.graph, synced_at = EXCLUDED.synced_at, "
        "nodes = EXCLUDED.nodes, edges = EXCLUDED.edges"),
        {"p": project_id, "g": name, "t": synced_at, "n": counts["nodes"], "e": counts["edges"]})
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
    edges = _insert_edges(connection, project_id, name, _edge_rows(connection, project_id))
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
    last = connection.execute(text("SELECT synced_at FROM kg_age_sync WHERE project_id = :p"),
                              {"p": project_id}).scalar()
    if last is None or not _graph_exists(connection, name):
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
    if have == want:
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
        raise ValueError(f"the query did not run: {str(getattr(error, 'orig', error)).splitlines()[0]}") from error
    finally:
        savepoint.rollback()
    out = [{c: _ag(v) for c, v in zip(cols, row)} for row in rows[:limit]]
    return {"columns": cols, "rows": out, "truncated": len(rows) > limit, "graph": name}


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
    "project_graph",
    "rebuild",
    "reconcile",
    "run_cypher",
    "sync",
]
