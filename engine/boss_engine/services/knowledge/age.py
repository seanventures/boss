"""Apache AGE projection for one project.

kg_edges stays the system of record. traverse() keeps the recursive CTE.
This module copies open nodes and edges into a graph so their neighbors can
be compared. It does not switch the read path.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from .store import PostgresKnowledgeStore

_GRAPH_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def extension_version(connection: Connection) -> str:
    row = connection.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'age'")
    ).scalar()
    return str(row) if row else "not_installed"


def _graph(name: str) -> str:
    if not _GRAPH_NAME.fullmatch(name):
        raise ValueError("graph name must be a plain identifier")
    return name


def _ag(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip('"')


def _prepare(connection: Connection) -> None:
    connection.execute(text("LOAD 'age'"))
    connection.execute(text('SET search_path = ag_catalog, "$user", public'))


def _cypher(connection: Connection, graph: str, query: str, params: dict) -> list[Any]:
    # exec_driver_sql, because SQLAlchemy text() treats ":Label" as a bind.
    statement = f"""
        SELECT * FROM cypher('{graph}', $$
        {query}
        $$, CAST(%(params)s AS agtype)) AS (value agtype)
    """
    rows = connection.exec_driver_sql(
        statement, {"params": json.dumps(params)}
    ).scalars().all()
    return [_ag(row) for row in rows]


def project_graph(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    graph: str = "boss_kg",
) -> dict[str, Any]:
    """Copy open nodes and edges for one project. Refuses when AGE is absent."""
    if extension_version(connection) == "not_installed":
        raise RuntimeError("apache age is not installed")
    name = _graph(graph)
    store.ensure_schema(connection)
    store._org_id(connection, project_id, None)
    _prepare(connection)
    exists = connection.execute(
        text("SELECT 1 FROM ag_catalog.ag_graph WHERE name = :name"),
        {"name": name},
    ).first()
    if exists is None:
        connection.execute(text(f"SELECT create_graph('{name}')"))
    nested = connection.begin_nested()
    try:
        _cypher(
            connection,
            name,
            "MATCH (n:KgNode {project_id: $project_id}) DETACH DELETE n RETURN 1",
            {"project_id": project_id},
        )
        nested.commit()
    except SQLAlchemyError as exc:
        nested.rollback()
        message = str(exc).lower()
        if "does not exist" not in message and "label" not in message:
            raise
    nodes = connection.execute(
        text(
            """
            SELECT id, node_key
            FROM kg_nodes
            WHERE project_id = :project_id
              AND (valid_to IS NULL OR valid_to > now())
            ORDER BY id
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    edges = connection.execute(
        text(
            """
            SELECT src_id, dst_id, kind
            FROM kg_edges
            WHERE project_id = :project_id
              AND (valid_to IS NULL OR valid_to > now())
            ORDER BY src_id, dst_id, kind
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    for node in nodes:
        _cypher(
            connection,
            name,
            """
            CREATE (:KgNode {boss_id: $boss_id, node_key: $node_key, project_id: $project_id})
            RETURN 1
            """,
            {
                "boss_id": node["id"],
                "node_key": node["node_key"],
                "project_id": project_id,
            },
        )
    for edge in edges:
        _cypher(
            connection,
            name,
            """
            MATCH (a:KgNode {boss_id: $src, project_id: $project_id}),
                  (b:KgNode {boss_id: $dst, project_id: $project_id})
            CREATE (a)-[:KG_EDGE {project_id: $project_id, kind: $kind}]->(b)
            RETURN 1
            """,
            {
                "src": edge["src_id"],
                "dst": edge["dst_id"],
                "kind": edge["kind"],
                "project_id": project_id,
            },
        )
    return {"graph": name, "nodes": len(nodes), "edges": len(edges)}


def age_neighbors(
    connection: Connection,
    *,
    project_id: str,
    start_id: str,
    max_depth: int,
    graph: str = "boss_kg",
) -> list[dict[str, Any]]:
    """Directed neighbors from the projection. Depth is the shortest path."""
    if extension_version(connection) == "not_installed":
        raise RuntimeError("apache age is not installed")
    depth = int(max_depth)
    if depth < 1 or depth > 8:
        raise ValueError("max_depth must be from 1 to 8")
    name = _graph(graph)
    _prepare(connection)
    rows = connection.exec_driver_sql(
        f"""
        SELECT * FROM cypher('{name}', $$
        MATCH path = (a:KgNode {{boss_id: $start, project_id: $project_id}})-[:KG_EDGE*1..{depth}]->(b:KgNode)
        WHERE b.project_id = $project_id
        RETURN [b.boss_id, length(path)]
        $$, CAST(%(params)s AS agtype)) AS (value agtype)
        """,
        {"params": json.dumps({"start": start_id, "project_id": project_id})},
    ).scalars().all()
    best: dict[str, int] = {}
    for row in rows:
        parsed = _ag(row)
        if not isinstance(parsed, list) or len(parsed) != 2:
            continue
        node_id = str(parsed[0])
        hop = int(parsed[1])
        current = best.get(node_id)
        if current is None or hop < current:
            best[node_id] = hop
    return [
        {"node_id": node_id, "depth": hop}
        for node_id, hop in sorted(best.items(), key=lambda item: (item[1], item[0]))
    ]
