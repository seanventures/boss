"""A node's typed neighbours, a hop or two out (rulebook source G6: the ``kg_neighbors`` tool).

An agent that found a data-quality rule by meaning asks what it touches — the CDE it CHECKS, the
columns it APPLIES_TO, the report lines it REACHES, its Warning and Error thresholds (OF), the
clause it was read from (evidenced_by) — without writing Cypher. Every hop reads under the stage's
knowledge scope (``store._scope_sql``): a proposed rule is not a stage's neighbour, a code node is
only within the stage's code reach. Usage bookkeeping (consulted / reused) is left out unless the
caller names it.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore

MAX_HOPS = 2
MAX_NEIGHBOURS = 60
#: Edges that record who used what, not what a thing is: left out unless asked for.
_BOOKKEEPING = ("consulted", "reused", "shown")

_STORE = PostgresKnowledgeStore()


def resolve(connection: Connection, project_id: str, ref: str) -> dict[str, Any]:
    """One live node for ``ref``: its id, its key (``NPB-DQ-LN-003``, ``CDE-LN-001``) or its exact
    name. Refused in words when nothing, or more than one thing, matches."""
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError("node is required: a node id, key or exact name")
    live = "project_id = :p AND (valid_to IS NULL OR valid_to > now())"
    rows = connection.execute(text(
        f"SELECT id, kind, node_key, name FROM kg_nodes WHERE {live} AND (id = :r OR node_key = :r) "
        "LIMIT 2"), {"p": project_id, "r": ref}).mappings().all()
    if not rows:
        rows = connection.execute(text(
            f"SELECT id, kind, node_key, name FROM kg_nodes WHERE {live} AND name = :r "
            "ORDER BY id LIMIT 4"), {"p": project_id, "r": ref}).mappings().all()
    if not rows:
        raise LookupError(f"{ref!r} is not a node of this project: pass an id kg_search returned, "
                          "or a node's key or exact name")
    if len(rows) > 1 and rows[0]["id"] != ref:
        names = ", ".join(f"{r['kind']} {r['name']}" for r in rows[:3])
        raise LookupError(f"{ref!r} names more than one node ({names}); pass the id")
    return dict(rows[0])


def typed_neighbors(
    connection: Connection, *, project_id: str, node: str, hops: int = 1,
    edge_kinds: Optional[Iterable[str]] = None, policy: Any = None, limit: int = 40,
) -> dict[str, Any]:
    """The node and its neighbours up to ``hops`` (at most 2) away: each with its kind, name, the
    edge that reached it, which way the edge points, and the hop. ``edge_kinds`` narrows the edges
    followed (any spelling: ``checks`` or ``CHECKS``)."""
    start = resolve(connection, project_id, node)
    _STORE.assert_readable(connection, project_id=project_id, node_id=start["id"], policy=policy)
    hops = max(1, min(int(hops or 1), MAX_HOPS))
    limit = max(1, min(int(limit or 40), MAX_NEIGHBOURS))
    wanted = sorted({k for raw in (edge_kinds or []) for k in (str(raw).strip().lower(), str(raw).strip().upper())
                     if str(raw).strip()})
    scope_sql, scope_params = _STORE._scope_sql(connection, project_id, policy, alias="n")
    seen = {start["id"]}
    frontier = [start["id"]]
    out: list[dict[str, Any]] = []
    truncated = False
    for hop in range(1, hops + 1):
        if not frontier:
            break
        rows = connection.execute(text(
            f"""
            SELECT e.kind AS edge, CASE WHEN e.src_id = ANY(:ids) THEN 'out' ELSE 'in' END AS direction,
                   CASE WHEN e.src_id = ANY(:ids) THEN e.src_id ELSE e.dst_id END AS via,
                   n.id, n.kind, n.node_key, n.name, n.trust_tier, n.attrs
            FROM kg_edges e
            JOIN kg_nodes n ON n.id = CASE WHEN e.src_id = ANY(:ids) THEN e.dst_id ELSE e.src_id END
            WHERE e.project_id = :p AND n.project_id = :p
              AND (e.src_id = ANY(:ids) OR e.dst_id = ANY(:ids))
              AND (e.valid_to IS NULL OR e.valid_to > now())
              AND (n.valid_to IS NULL OR n.valid_to > now())
              AND (CAST(:wanted AS text[]) IS NULL OR e.kind = ANY(CAST(:wanted AS text[])))
              AND (CAST(:wanted AS text[]) IS NOT NULL OR e.kind <> ALL(CAST(:bookkeeping AS text[])))
              {scope_sql}
            ORDER BY e.kind, n.kind, n.name, n.id
            LIMIT :n
            """), {"p": project_id, "ids": frontier, "wanted": wanted or None,
                   "bookkeeping": list(_BOOKKEEPING), "n": limit * 4, **scope_params}).mappings().all()
        nxt: list[str] = []
        for row in rows:
            if row["id"] in seen:
                continue
            if len(out) >= limit:
                truncated = True
                break
            seen.add(row["id"])
            nxt.append(row["id"])
            attrs = row["attrs"] or {}
            item = {"id": row["id"], "kind": row["kind"], "key": row["node_key"], "name": row["name"],
                    "edge": row["edge"], "direction": row["direction"], "hop": hop,
                    "trust_tier": row["trust_tier"]}
            if row["via"] != start["id"]:
                item["via"] = row["via"]
            if row["kind"] == "dq_threshold":
                item.update({"level": attrs.get("level"), "metric": attrs.get("metric"),
                             "value": attrs.get("value")})
            if row["kind"] == "clause" and attrs.get("citation"):
                item["citation"] = attrs.get("citation")
            out.append(item)
        frontier = nxt
        if truncated:
            break
    return {"node": {"id": start["id"], "kind": start["kind"], "key": start["node_key"],
                     "name": start["name"]},
            "hops": hops, "neighbors": out, "count": len(out), "truncated": truncated,
            "note": ("Cite a rule as [R:<id>] and a clause, data-quality rule or other document "
                     "node as [D:<id>].")}
