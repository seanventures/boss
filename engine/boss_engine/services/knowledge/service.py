"""Application service used by the API and the MCP tools.

Reads require project scope inside the store. Writes that change trust or
forget a fact require a person. `kg_propose_fact` always stores `inferred`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .age import extension_version as age_extension_version
from .age import projection_counts
from .archivist import distil, forget_source as forget_source_lineage
from .capture import capture_lifecycle as capture_lifecycle_record
from .learning import apply_learning
from .retrieval import blast_radius, fuse_lists, knowledge_pack, precedent_section, rank_hits
from .scopes import KEYWORD_EXCLUDED_LAYERS, keyword_policy
from .store import PostgresKnowledgeStore

READ = "knowledge:read"
MANAGE = "knowledge:manage"


class KnowledgeService:
    def __init__(self, store: Optional[PostgresKnowledgeStore] = None) -> None:
        self.store = store or PostgresKnowledgeStore()

    def search(
        self,
        connection: Connection,
        *,
        project_id: str,
        query: str,
        limit: int = 20,
        policy=None,
        embedder=None,
        seeds: Optional[list[str]] = None,
        mode: str = "on",
    ) -> dict[str, Any]:
        """Hybrid retrieval: full text, the graph around the seeds (or around the best text hits),
        and the vector index when an embedder is given, fused by reciprocal rank. Governed and
        approved facts still rank above inferred ones (rank_hits).

        ``mode`` is the project's knowledge switch as an agent path read it
        (runtime.knowledge_mode): ``keyword`` is full-text keyword search only, ``off`` finds
        nothing. A person's own search (the console) leaves it ``on``."""
        if mode == "off":
            return {"hits": [], "retrieval": {"mode": "off", "lists": {}}}
        if mode == "keyword":
            return self._keyword_search(connection, project_id=project_id, query=query, limit=limit,
                                        policy=policy)
        text_hits = self.store.text_search(
            connection, project_id=project_id, query=query, limit=max(limit, 20), policy=policy
        )
        seed_ids = list(seeds or [hit["node_id"] for hit in text_hits[:5]])
        lists: dict[str, list[dict]] = {
            "text": text_hits,
            "graph": self.store.graph_candidates(
                connection, project_id=project_id, seed_ids=seed_ids, policy=policy),
        }
        reason = None
        if embedder is None:
            from ...config import get_settings
            from .embeddings import configured_embedder

            embedder, reason = configured_embedder(connection, get_settings(), project_id)
        if embedder is not None:
            # A refused budget or a provider that is down narrows THIS search to text and the
            # graph, and says so; it never fails the search.
            try:
                with connection.begin_nested():
                    vector = embedder.embed([query])[0]
                    lists["vector"] = self.store.vector_search(
                        connection, project_id=project_id, model_id=embedder.model_id,
                        embedding=list(vector), limit=50, policy=policy)
            except Exception as error:  # noqa: BLE001
                reason = ("meaning-based search is off for this search: the embedding model did "
                          f"not answer ({type(error).__name__}: {str(error)[:160]})")
        enriched = []
        for hit in fuse_lists(lists):
            node = self.store.get_node(
                connection, project_id=project_id, node_id=hit["node_id"]
            )
            if node is None:
                continue
            enriched.append({
                **hit,
                "name": node["name"],
                "trust_tier": node["trust_tier"],
                "body": node["body"],
            })
        return {
            "hits": rank_hits(enriched)[:limit],
            "retrieval": {"mode": "hybrid" if "vector" in lists else "text+graph",
                          "lists": {name: len(items) for name, items in lists.items()},
                          **({"reason": reason} if reason else {})},
        }

    def _keyword_search(
        self, connection: Connection, *, project_id: str, query: str, limit: int, policy=None
    ) -> dict[str, Any]:
        """The A′ baseline: plain full-text matches in text-rank order. No graph traversal (a
        seeded stage reads its seeds only, zero hops), no vectors, and no learned facts (the
        knowledge layer); precedent is not offered at all (precedent())."""
        text_hits = self.store.text_search(
            connection, project_id=project_id, query=query, limit=limit,
            policy=keyword_policy(policy), exclude_layers=KEYWORD_EXCLUDED_LAYERS,
        )
        hits = []
        for hit in text_hits:
            node = self.store.get_node(connection, project_id=project_id, node_id=hit["node_id"])
            if node is None:
                continue
            hits.append({"node_id": hit["node_id"], "node_key": hit.get("node_key"),
                         "score": float(hit.get("rank") or 0), "via": ["text"],
                         "name": node["name"], "trust_tier": node["trust_tier"], "body": node["body"]})
        return {"hits": hits[:limit], "retrieval": {"mode": "keyword", "lists": {"text": len(text_hits)}}}

    def why(
        self, connection: Connection, *, project_id: str, node_id: str, policy=None
    ) -> dict[str, Any]:
        node = self.store.get_node(connection, project_id=project_id, node_id=node_id)
        if node is None:
            raise LookupError(node_id)
        facts = self.store.neighbors(
            connection, project_id=project_id, node_id=node_id, kind="about", direction="in",
            policy=policy,
        )
        return {"node": _public_node(node), "why": facts}

    def blast(
        self, connection: Connection, *, project_id: str, node_id: str, max_depth: int = 3,
        policy=None,
    ) -> dict[str, Any]:
        self.store.assert_readable(connection, project_id=project_id, node_id=node_id, policy=policy)
        return {
            "node_id": node_id,
            "affected": blast_radius(
                connection, self.store, project_id=project_id, node_id=node_id, max_depth=max_depth,
                policy=policy,
            ),
            "dependent_files": self.dependent_files(connection, project_id=project_id, node_id=node_id),
        }

    def dependent_files(self, connection: Connection, *, project_id: str, node_id: str) -> list[str]:
        """The files that use this node directly (depth 1): they call it, refer to it, or import
        it, other than the file that defines it. The measure the blind answer keys score."""
        node = self.store.get_node(connection, project_id=project_id, node_id=node_id)
        if node is None:
            raise LookupError(node_id)
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT s.path
                FROM kg_edges e
                JOIN kg_nodes s ON s.id = e.src_id
                JOIN kg_nodes d ON d.id = e.dst_id
                WHERE e.dst_id = :node_id AND e.project_id = :project_id
                  AND e.kind IN ('calls', 'references', 'imports')
                  AND (e.valid_to IS NULL OR e.valid_to > now())
                  AND (s.valid_to IS NULL OR s.valid_to > now())
                  AND s.path IS NOT NULL AND s.path IS DISTINCT FROM d.path
                ORDER BY s.path
                """
            ),
            {"node_id": node_id, "project_id": project_id},
        ).scalars().all()
        return list(rows)

    def symbol(
        self, connection: Connection, *, project_id: str, name: str, policy=None
    ) -> dict[str, Any]:
        self.store._org_id(connection, project_id, None)
        if policy is not None and not policy.reads_code:
            raise PermissionError(policy.refusal())
        row = connection.execute(
            text(
                """
                SELECT id, node_key, name, trust_tier FROM kg_nodes
                WHERE project_id = :project_id AND kind = 'symbol' AND name = :name
                  AND (valid_to IS NULL OR valid_to > now())
                ORDER BY node_key
                LIMIT 1
                """
            ),
            {"project_id": project_id, "name": name},
        ).mappings().first()
        if row is None:
            return {"found": False, "name": name}
        self.store.assert_readable(connection, project_id=project_id, node_id=row["id"], policy=policy)
        return {"found": True, **dict(row)}

    def callers(
        self, connection: Connection, *, project_id: str, node_id: str, policy=None
    ) -> dict[str, Any]:
        return {
            "callers": self.store.neighbors(
                connection, project_id=project_id, node_id=node_id, kind="calls", direction="in",
                policy=policy,
            )
        }

    def capture_lifecycle(
        self, connection: Connection, *, project_id: str, record: dict[str, Any]
    ) -> dict[str, Any]:
        return capture_lifecycle_record(
            connection, self.store, project_id=project_id, record=record
        )

    def attribute_run(
        self, connection: Connection, *, project_id: str, run_id: str, success: bool
    ) -> list[str]:
        return self.store.record_outcome(
            connection, project_id=project_id, run_id=run_id, success=success
        )

    def propose_fact(
        self,
        connection: Connection,
        *,
        project_id: str,
        statement: str,
        subject: str,
        predicate: str,
        obj: str,
        kind: str = "lesson",
    ) -> dict[str, Any]:
        """Agents propose inferred facts only. A requested tier is ignored."""
        from .archivist import ScriptedExtractor

        capture = self.store.capture(
            connection,
            project_id=project_id,
            source_kind="proposal",
            source_id=f"{subject}|{predicate}|{obj}",
            body=statement,
            scrub_counts={},
        )
        result = distil(
            connection,
            self.store,
            project_id=project_id,
            capture_id=capture["id"],
            text=statement,
            extractor=ScriptedExtractor(
                facts=({
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "statement": statement,
                    "kind": kind,
                    "confidence": 0.4,
                },)
            ),
        )
        return {"trust_tier": "inferred", **result}

    def promote(
        self, connection: Connection, *, project_id: str, fact_id: str, actor_kind: str
    ) -> dict[str, Any]:
        self.store.set_trust(
            connection,
            project_id=project_id,
            node_id=fact_id,
            trust_tier="approved",
            actor_kind=actor_kind,
        )
        self.store.audit(
            connection,
            project_id=project_id,
            action="knowledge.promote",
            entity_id=fact_id,
            actor_kind=actor_kind,
            detail={},
        )
        from .age import after_write

        after_write(connection, project_id)
        return {"id": fact_id, "trust_tier": "approved"}

    def reject(
        self, connection: Connection, *, project_id: str, fact_id: str, actor_kind: str
    ) -> dict[str, Any]:
        self.store.close_validity(
            connection,
            project_id=project_id,
            node_id=fact_id,
            valid_to=datetime.now(timezone.utc),
            actor_kind=actor_kind,
        )
        self.store.audit(
            connection,
            project_id=project_id,
            action="knowledge.reject",
            entity_id=fact_id,
            actor_kind=actor_kind,
            detail={},
        )
        from .age import after_write

        after_write(connection, project_id)
        return {"id": fact_id, "closed": True}

    def forget(
        self,
        connection: Connection,
        *,
        project_id: str,
        fact_id: str,
        actor_kind: str,
    ) -> dict[str, Any]:
        self.store.delete_node(
            connection, project_id=project_id, node_id=fact_id, actor_kind=actor_kind
        )
        self.store.audit(
            connection,
            project_id=project_id,
            action="knowledge.forget",
            entity_id=fact_id,
            actor_kind=actor_kind,
            detail={},
        )
        return {"forgotten": [fact_id]}

    def forget_source(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_id: str,
        actor_kind: str,
    ) -> dict[str, Any]:
        return forget_source_lineage(
            connection,
            self.store,
            project_id=project_id,
            source_id=source_id,
            actor_kind=actor_kind,
        )

    def pack(
        self,
        connection: Connection,
        *,
        project_id: str,
        role: str,
        stage: str,
        query: str,
        budget_tokens: int,
        ceiling: str = "governed",
        policy=None,
        mode: str = "on",
    ) -> dict[str, Any]:
        """A budgeted pack, built under the stage's knowledge scope when one is given, from the
        retrieval the project's switch allows (``mode``, see search)."""
        if policy is not None:
            budget_tokens = min(budget_tokens, policy.budget_tokens)
        found = self.search(connection, project_id=project_id, query=query, limit=20, policy=policy,
                            mode=mode)
        return knowledge_pack(
            role=role,
            stage=stage,
            hits=found["hits"],
            budget_tokens=budget_tokens,
            ceiling=ceiling,
        )

    def precedent(
        self, connection: Connection, *, project_id: str, query: str, limit: int = 5, policy=None,
        mode: str = "on",
    ) -> dict[str, Any]:
        if mode != "on":            # keyword search only, or off: no precedent and nothing to cite
            return {"cited_fact_ids": [], "section": ""}
        found = self.search(connection, project_id=project_id, query=query, limit=limit, policy=policy)
        decisions = [
            hit for hit in found["hits"]
            if str(hit.get("node_key") or "").startswith("fact:")
            or hit.get("trust_tier") in {"governed", "approved"}
        ]
        return {
            "cited_fact_ids": [hit["node_id"] for hit in decisions],
            "section": precedent_section(decisions),
        }

    def status(self, connection: Connection, *, project_id: str) -> dict[str, Any]:
        from .embedding_slot import model_id_of, read, search_state

        slot = read(connection, project_id)
        searching, reason = search_state(slot)
        counts = self.store.counts(connection, project_id=project_id)
        rows = connection.execute(
            text(
                """
                SELECT n.id, n.node_key, n.trust_tier, n.body,
                       COALESCE(s.success_count, 0) AS success_count,
                       COALESCE(s.failure_count, 0) AS failure_count,
                       s.last_used_at, COALESCE(s.archived, FALSE) AS archived
                FROM kg_nodes n
                LEFT JOIN kg_fact_stats s ON s.fact_id = n.id AND s.project_id = n.project_id
                WHERE n.project_id = :project_id AND n.kind = 'fact'
                  AND (n.valid_to IS NULL OR n.valid_to > now())
                ORDER BY n.node_key
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        now = datetime.now(timezone.utc)
        candidates = []
        for row in rows:
            learned = apply_learning(dict(row), now=now)
            if learned["promotion_candidate"]:
                candidates.append({"id": row["id"], "node_key": row["node_key"], "body": row["body"]})
        from .runtime import knowledge_mode

        return {
            "project_id": project_id,
            # The knowledge_graph switch: what agents are given (on | keyword | off).
            "agent_access": knowledge_mode(connection, project_id),
            "counts": counts,
            "promotion_candidates": candidates,
            "budget_category": "knowledge",
            "traversal": "recursive_cte",
            "ship_gate": "case_study_primary_not_run",
            "ingest_lag_seconds": None,
            "cost_usd": None,
            "retention_years": 1,
            "embedding_model": ({"provider": slot["provider"], "model": slot.get("model"),
                                 "dimension": slot.get("dimension"), "model_id": model_id_of(slot),
                                 "check": (slot.get("check") or {}).get("state", "unchecked")}
                                if slot else None),
            "retrieval": {"mode": "hybrid" if searching else "text+graph",
                          **({"reason": reason} if reason else {})},
            "vector_index": self.store.vector_index_kind(connection),
            "vector_extension": self.store.vector_extension_version(connection),
            "age_extension": age_extension_version(connection),
            "cypher": {"graph": "one per project", "available": age_extension_version(connection) != "not_installed",
                       "projection": projection_counts(connection, project_id)
                       if age_extension_version(connection) != "not_installed" else None},
            "sources": ["adr folder", "connector document"],
            # Each registered repository's code graph (repo_index.py): state, commit, counts.
            "repositories": _repositories(connection, project_id),
        }

    def log_pack(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        step: str,
        fact_ids: list[str],
    ) -> int:
        return self.store.log_usage(
            connection, project_id=project_id, run_id=run_id, step=step, fact_ids=fact_ids
        )


    def cypher(
        self,
        connection: Connection,
        *,
        project_id: str,
        query: str,
        params: Optional[dict] = None,
        columns: Optional[list[str]] = None,
        limit: int = 200,
        policy=None,
    ) -> dict[str, Any]:
        """Read-only Cypher over the project's AGE projection (knowledge/age.py run_cypher)."""
        from .age import run_cypher

        self.store._org_id(connection, project_id, None)
        result = run_cypher(connection, project_id=project_id, query=query, params=params,
                            columns=columns, policy=policy, row_limit=limit)
        if not result["rows"]:
            result["note"] = ("No rows. Check the labels, relationship types and property names "
                              "against kg_schema before reading this as absence.")
        return result

    def schema(self, connection: Connection, *, project_id: str, policy=None) -> dict[str, Any]:
        """What this project's graph holds, from its own rows: labels and kinds with counts,
        relationship types with counts and the labels they join, the properties a query can
        read, and the repositories indexed. Cached per project until the graph is written again."""
        self.store._org_id(connection, project_id, None)
        return graph_schema(connection, project_id=project_id, policy=policy)

_SCHEMA_CACHE: dict[str, tuple[Any, float, dict[str, Any]]] = {}
_SCHEMA_TTL_SECONDS = 600.0

#: How to read the summary. General, never about one repository or question.
SCHEMA_GUIDANCE = (
    "Answer structure questions (who implements or extends a type, who calls a function, what a "
    "file defines) by following relationships, not by matching names: names match methods, "
    "tests and samples too.",
    "A result that hits a LIMIT is a sample; absence from a sample proves nothing. Count first "
    "(RETURN count(*)) when completeness matters.",
    "A relationship type with no count here does not exist in this graph: say so rather than "
    "inferring from names.",
)


def graph_schema(connection: Connection, *, project_id: str, policy=None) -> dict[str, Any]:
    import time

    from .age import DIALECT_NOTES, LABELS, VERTEX_EXTRAS
    from .scopes import LAYERS

    layers = [layer for layer in LAYERS if policy is None or layer in policy.layers]
    if policy is not None and not policy.reads_code:
        layers = [layer for layer in layers if layer != "code"]
    stamp = connection.execute(text(
        "SELECT synced_at FROM kg_age_sync WHERE project_id = :p"), {"p": project_id}).scalar() \
        if _has_table(connection, "kg_age_sync") else None
    key = f"{project_id}|{','.join(layers)}"
    cached = _SCHEMA_CACHE.get(key)
    if cached is not None and cached[0] == stamp and time.monotonic() - cached[1] < _SCHEMA_TTL_SECONDS:
        return cached[2]
    nodes = connection.execute(text(
        """
        SELECT knowledge_layer AS layer, kind, attrs->>'symbol_kind' AS symbol_kind,
               attrs->>'type_kind' AS type_kind, count(*) AS n
        FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL AND knowledge_layer = ANY(:layers)
        GROUP BY 1, 2, 3, 4
        """), {"p": project_id, "layers": layers}).mappings().all()
    labels: dict[str, dict[str, Any]] = {}
    for row in nodes:
        label = labels.setdefault(LABELS[row["layer"]], {"count": 0, "kinds": {}})
        label["count"] += int(row["n"])
        label["kinds"][row["kind"]] = label["kinds"].get(row["kind"], 0) + int(row["n"])
        for prop in ("symbol_kind", "type_kind"):
            if row[prop]:
                bucket = label.setdefault(prop, {})
                bucket[row[prop]] = bucket.get(row[prop], 0) + int(row["n"])
    edges = connection.execute(text(
        """
        SELECT e.kind, s.knowledge_layer AS src, d.knowledge_layer AS dst, count(*) AS n
        FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id
        WHERE e.project_id = :p AND e.valid_to IS NULL AND s.valid_to IS NULL AND d.valid_to IS NULL
          AND s.knowledge_layer = ANY(:layers) AND d.knowledge_layer = ANY(:layers)
        GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """), {"p": project_id, "layers": layers}).mappings().all()
    relationships: dict[str, dict[str, Any]] = {}
    for row in edges:
        rel = relationships.setdefault(row["kind"], {"count": 0, "between": {}})
        rel["count"] += int(row["n"])
        rel["between"][f"{LABELS[row['src']]}->{LABELS[row['dst']]}"] = int(row["n"])
    repos = connection.execute(text(
        "SELECT repo, status, indexed_sha FROM kg_repo_index WHERE project_id = :p ORDER BY repo"),
        {"p": project_id}).mappings().all() if _has_table(connection, "kg_repo_index") else []
    out = {
        "project_id": project_id,
        "labels": {k: labels[k] for k in sorted(labels, key=lambda k: -labels[k]["count"])},
        "relationships": dict(sorted(relationships.items(), key=lambda kv: -kv[1]["count"])),
        "vertex_properties": ["node_id", "kind", "node_key", "name", "trust_tier", *VERTEX_EXTRAS],
        "edge_properties": ["edge_id", "kind", "trust_tier", "confidence"],
        "notes": {
            "name": "a symbol's name is its qualname inside its file: Class, Class.method, function",
            "confidence": "1.0 resolved through the code's own scope or imports; 0.5 only because "
                          "the simple name is unique in the repository",
        },
        "repositories": [{"repo": r["repo"], "status": r["status"], "commit": (r["indexed_sha"] or "")[:12]}
                         for r in repos],
        "guidance": list(SCHEMA_GUIDANCE),
        "dialect": list(DIALECT_NOTES),
    }
    _SCHEMA_CACHE[key] = (stamp, time.monotonic(), out)
    return out


def _has_table(connection: Connection, name: str) -> bool:
    return connection.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar() is True


def _public_node(node: dict) -> dict:
    return {
        "id": node["id"],
        "node_key": node["node_key"],
        "name": node["name"],
        "kind": node.get("kind"),
        "trust_tier": node["trust_tier"],
        "body": node["body"],
        "valid_to": node.get("valid_to"),
    }


def require_confirmation(step_up: str | None) -> None:
    if step_up != "confirmed":
        raise PermissionError("knowledge:manage requires step_up=confirmed")


def _repositories(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    from .repo_index import status as repo_status

    try:
        with connection.begin_nested():
            return repo_status(connection, project_id)["repositories"]
    except Exception:  # noqa: BLE001 - an older schema without kg_repo_index: no rows to show
        return []
