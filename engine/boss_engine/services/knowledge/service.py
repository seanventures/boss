"""Application service used by the API and the MCP tools.

Reads require project scope inside the store. Writes that change trust or
forget a fact require a person. `kg_propose_fact` always stores `inferred`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .archivist import distil, forget_source as forget_source_lineage
from .capture import capture_lifecycle as capture_lifecycle_record
from .learning import apply_learning
from .retrieval import blast_radius, knowledge_pack, precedent_section, rank_hits
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
    ) -> dict[str, Any]:
        hits = self.store.text_search(
            connection, project_id=project_id, query=query, limit=limit
        )
        enriched = []
        for hit in hits:
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
        return {"hits": rank_hits(enriched)}

    def why(
        self, connection: Connection, *, project_id: str, node_id: str
    ) -> dict[str, Any]:
        node = self.store.get_node(connection, project_id=project_id, node_id=node_id)
        if node is None:
            raise LookupError(node_id)
        facts = self.store.neighbors(
            connection, project_id=project_id, node_id=node_id, kind="about", direction="in"
        )
        return {"node": _public_node(node), "why": facts}

    def blast(
        self, connection: Connection, *, project_id: str, node_id: str, max_depth: int = 3
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "affected": blast_radius(
                connection, self.store, project_id=project_id, node_id=node_id, max_depth=max_depth
            ),
        }

    def symbol(self, connection: Connection, *, project_id: str, name: str) -> dict[str, Any]:
        self.store._org_id(connection, project_id, None)
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
        return {"found": True, **dict(row)}

    def callers(self, connection: Connection, *, project_id: str, node_id: str) -> dict[str, Any]:
        return {
            "callers": self.store.neighbors(
                connection, project_id=project_id, node_id=node_id, kind="calls", direction="in"
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
    ) -> dict[str, Any]:
        found = self.search(connection, project_id=project_id, query=query, limit=20)
        return knowledge_pack(
            role=role,
            stage=stage,
            hits=found["hits"],
            budget_tokens=budget_tokens,
            ceiling=ceiling,
        )

    def precedent(
        self, connection: Connection, *, project_id: str, query: str, limit: int = 5
    ) -> dict[str, Any]:
        found = self.search(connection, project_id=project_id, query=query, limit=limit)
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
        return {
            "project_id": project_id,
            "counts": counts,
            "promotion_candidates": candidates,
            "budget_category": "knowledge",
            "traversal": "recursive_cte",
            "ship_gate": "case_study_primary_not_run",
            "ingest_lag_seconds": None,
            "cost_usd": None,
            "retention_years": 1,
            "embedding_model": None,
            "vector_index": "not_created",
            "sources": ["adr folder", "connector document"],
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
