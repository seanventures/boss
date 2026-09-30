"""ProviderOps resource topology graph (BOSS-240).

Minimal durable directed edges between ``external_resources`` rows. Inventory
and drift remain separate; this module owns upsert/list/delete of edges plus a
compact graph summary for ProviderOps UI and agents.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id
from .receipts import ReceiptService

# Common relation types (open-ended TEXT in DB; these are suggested values).
KNOWN_RELATION_TYPES = frozenset(
    {
        "in_vpc",
        "attached_to",
        "depends_on",
        "member_of",
        "routes_to",
        "hosts",
        "uses",
        "linked_to",
    }
)


class ResourceTopologyError(ValueError):
    """Domain error for topology mutations (mapped to 4xx by HTTP layer)."""


class ResourceTopologyNotFoundError(ResourceTopologyError):
    pass


class ResourceTopologyService:
    def __init__(self, settings=None):
        self.settings = settings
        self.receipts = ReceiptService()

    # --- writes --------------------------------------------------------------
    def upsert_edge(
        self,
        connection: Connection,
        *,
        project_id: str,
        from_resource_id: str,
        to_resource_id: str,
        relation_type: str,
        provider: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        actor: str = "system",
        source: str = "manual",
    ) -> dict[str, Any]:
        """Create-or-update a directed edge; emits a receipt for the mutation."""
        project_id = self._require_text(project_id, "project_id")
        from_resource_id = self._require_text(from_resource_id, "from_resource_id")
        to_resource_id = self._require_text(to_resource_id, "to_resource_id")
        relation_type = self._require_text(relation_type, "relation_type").lower()
        actor = self._require_text(actor, "actor")
        if from_resource_id == to_resource_id:
            raise ResourceTopologyError("from_resource_id and to_resource_id must differ.")

        from_res = self._load_resource(connection, project_id, from_resource_id)
        to_res = self._load_resource(connection, project_id, to_resource_id)
        if not from_res:
            raise ResourceTopologyNotFoundError(
                f"from_resource_id not found in project: {from_resource_id}"
            )
        if not to_res:
            raise ResourceTopologyNotFoundError(
                f"to_resource_id not found in project: {to_resource_id}"
            )

        normalized_provider = (
            (provider or from_res.get("provider") or to_res.get("provider") or "")
            .strip()
            .lower()
            or None
        )
        meta = dict(metadata or {})
        meta.setdefault("source", source)

        scope = self._scope(connection, project_id)
        dedupe_key = (
            f"resource_topology:upsert:{project_id}:"
            f"{from_resource_id}:{to_resource_id}:{relation_type}"
        )
        effect_scope = {
            "project_id": project_id,
            "from_resource_id": from_resource_id,
            "to_resource_id": to_resource_id,
            "relation_type": relation_type,
            "provider": normalized_provider,
        }
        payload = {
            "action": "topology_edge.upsert",
            "from_resource_id": from_resource_id,
            "to_resource_id": to_resource_id,
            "relation_type": relation_type,
            "provider": normalized_provider,
            "metadata": meta,
            "actor": actor,
            "source": source,
        }
        receipt, _created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "resource_topology.edge.upsert",
            effect_scope,
            payload,
            data_boundary="internal",
        )

        edge_id = make_id("rtop")
        row = connection.execute(
            text(
                """
                INSERT INTO resource_topology_edges (
                    id, project_id, from_resource_id, to_resource_id,
                    relation_type, provider, metadata
                )
                VALUES (
                    :id, :project_id, :from_resource_id, :to_resource_id,
                    :relation_type, :provider, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, from_resource_id, to_resource_id, relation_type)
                DO UPDATE SET
                    provider = COALESCE(EXCLUDED.provider, resource_topology_edges.provider),
                    metadata = resource_topology_edges.metadata || EXCLUDED.metadata,
                    updated_at = now()
                RETURNING id, project_id, from_resource_id, to_resource_id,
                          relation_type, provider, metadata, created_at, updated_at
                """
            ),
            {
                "id": edge_id,
                "project_id": project_id,
                "from_resource_id": from_resource_id,
                "to_resource_id": to_resource_id,
                "relation_type": relation_type,
                "provider": normalized_provider,
                "metadata": json.dumps(meta),
            },
        ).mappings().one()
        edge = self._serialize_edge(row)

        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            provider_ref=edge["id"],
            result={
                "edge_id": edge["id"],
                "action": "upsert",
                "relation_type": relation_type,
                "from_resource_id": from_resource_id,
                "to_resource_id": to_resource_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "resource_topology.edge.upserted",
            actor,
            "resource_topology_edge",
            edge["id"],
            {
                "from_resource_id": from_resource_id,
                "to_resource_id": to_resource_id,
                "relation_type": relation_type,
                "provider": normalized_provider,
                "receipt_id": receipt["id"],
                "source": source,
            },
        )
        return {"edge": edge, "receipt_id": receipt["id"]}

    def delete_edge(
        self,
        connection: Connection,
        *,
        project_id: str,
        edge_id: str,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Delete an edge by id within a project; emits a receipt."""
        project_id = self._require_text(project_id, "project_id")
        edge_id = self._require_text(edge_id, "edge_id")
        actor = self._require_text(actor, "actor")

        existing = connection.execute(
            text(
                """
                SELECT id, project_id, from_resource_id, to_resource_id,
                       relation_type, provider, metadata, created_at, updated_at
                FROM resource_topology_edges
                WHERE id = :edge_id AND project_id = :project_id
                """
            ),
            {"edge_id": edge_id, "project_id": project_id},
        ).mappings().first()
        if not existing:
            raise ResourceTopologyNotFoundError(
                f"Topology edge not found: {edge_id}"
            )
        edge = self._serialize_edge(existing)

        scope = self._scope(connection, project_id)
        # Unique per delete so re-deletes after recreate still leave audit trail.
        dedupe_key = f"resource_topology:delete:{project_id}:{edge_id}:{make_id('del')}"
        effect_scope = {
            "project_id": project_id,
            "edge_id": edge_id,
            "from_resource_id": edge["from_resource_id"],
            "to_resource_id": edge["to_resource_id"],
            "relation_type": edge["relation_type"],
        }
        payload = {
            "action": "topology_edge.delete",
            "edge": edge,
            "actor": actor,
        }
        receipt, _created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "resource_topology.edge.delete",
            effect_scope,
            payload,
            data_boundary="internal",
        )

        connection.execute(
            text(
                """
                DELETE FROM resource_topology_edges
                WHERE id = :edge_id AND project_id = :project_id
                """
            ),
            {"edge_id": edge_id, "project_id": project_id},
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            provider_ref=edge_id,
            result={
                "edge_id": edge_id,
                "action": "delete",
                "relation_type": edge["relation_type"],
            },
        )
        emit_event(
            connection,
            project_id,
            "resource_topology.edge.deleted",
            actor,
            "resource_topology_edge",
            edge_id,
            {
                "from_resource_id": edge["from_resource_id"],
                "to_resource_id": edge["to_resource_id"],
                "relation_type": edge["relation_type"],
                "receipt_id": receipt["id"],
            },
        )
        return {"deleted": True, "edge": edge, "receipt_id": receipt["id"]}

    # --- reads ---------------------------------------------------------------
    def list_edges(
        self,
        connection: Connection,
        project_id: str,
        *,
        from_resource_id: Optional[str] = None,
        to_resource_id: Optional[str] = None,
        relation_type: Optional[str] = None,
        provider: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": limit,
            "offset": offset,
        }
        if from_resource_id:
            clauses.append("from_resource_id = :from_resource_id")
            params["from_resource_id"] = from_resource_id
        if to_resource_id:
            clauses.append("to_resource_id = :to_resource_id")
            params["to_resource_id"] = to_resource_id
        if relation_type:
            clauses.append("relation_type = :relation_type")
            params["relation_type"] = relation_type.strip().lower()
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider.strip().lower()
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, from_resource_id, to_resource_id,
                       relation_type, provider, metadata, created_at, updated_at
                FROM resource_topology_edges
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_edge(row) for row in rows]

    def build_graph_summary(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider: Optional[str] = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Compact graph view: edges + the external_resources they reference."""
        edges = self.list_edges(
            connection,
            project_id,
            provider=provider,
            limit=limit,
            offset=0,
        )
        resource_ids: set[str] = set()
        for edge in edges:
            resource_ids.add(edge["from_resource_id"])
            resource_ids.add(edge["to_resource_id"])

        nodes: list[dict[str, Any]] = []
        if resource_ids:
            rows = connection.execute(
                text(
                    """
                    SELECT id, project_id, provider, resource_type, external_id,
                           name, state, ownership, environment, health_status,
                           tags, metadata
                    FROM external_resources
                    WHERE project_id = :project_id
                      AND id = ANY(:ids)
                    ORDER BY provider ASC, resource_type ASC, name ASC
                    """
                ),
                {"project_id": project_id, "ids": list(resource_ids)},
            ).mappings().all()
            for row in rows:
                payload = dict(row)
                payload["tags"] = payload.get("tags") or []
                payload["metadata"] = payload.get("metadata") or {}
                nodes.append(payload)

        relation_counts: dict[str, int] = {}
        for edge in edges:
            relation_counts[edge["relation_type"]] = (
                relation_counts.get(edge["relation_type"], 0) + 1
            )

        return {
            "project_id": project_id,
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "relation_counts": relation_counts,
        }

    # --- inference (simple heuristics) ---------------------------------------
    def infer_edges_from_inventory(
        self,
        connection: Connection,
        project_id: str,
        *,
        actor: str = "system",
        provider: Optional[str] = None,
    ) -> dict[str, Any]:
        """Infer edges from inventory tags/metadata (e.g. droplet→vpc).

        Heuristics (kept intentionally simple):
        - metadata keys ``vpc_uuid`` / ``vpc_id`` / ``vpc`` matching another
          resource's external_id → ``in_vpc``
        - tags like ``vpc:<external_id>`` or ``vpc_id:<external_id>`` → ``in_vpc``
        - metadata ``volume_id`` / tags ``volume:<id>`` → ``attached_to``
        """
        project_id = self._require_text(project_id, "project_id")
        actor = self._require_text(actor, "actor")
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id}
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider.strip().lower()

        rows = connection.execute(
            text(
                f"""
                SELECT id, provider, resource_type, external_id, name, tags, metadata
                FROM external_resources
                WHERE {' AND '.join(clauses)}
                ORDER BY provider ASC, resource_type ASC, name ASC
                """
            ),
            params,
        ).mappings().all()
        resources = [dict(r) for r in rows]
        by_external: dict[tuple[str, str], dict[str, Any]] = {}
        for res in resources:
            key = (str(res["provider"]).lower(), str(res["external_id"]))
            by_external[key] = res

        upserted: list[dict[str, Any]] = []
        skipped = 0
        for res in resources:
            targets = self._infer_targets_for_resource(res)
            for target_external_id, relation_type, meta in targets:
                peer = by_external.get(
                    (str(res["provider"]).lower(), target_external_id)
                )
                if not peer or peer["id"] == res["id"]:
                    skipped += 1
                    continue
                result = self.upsert_edge(
                    connection,
                    project_id=project_id,
                    from_resource_id=res["id"],
                    to_resource_id=peer["id"],
                    relation_type=relation_type,
                    provider=str(res["provider"]),
                    metadata=meta,
                    actor=actor,
                    source="inferred_inventory",
                )
                upserted.append(result["edge"])

        return {
            "project_id": project_id,
            "resources_scanned": len(resources),
            "edges_upserted": len(upserted),
            "skipped": skipped,
            "edges": upserted,
        }

    def _infer_targets_for_resource(
        self, res: dict[str, Any]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return list of (target_external_id, relation_type, metadata)."""
        out: list[tuple[str, str, dict[str, Any]]] = []
        meta = res.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        tags = res.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        for key, relation in (
            ("vpc_uuid", "in_vpc"),
            ("vpc_id", "in_vpc"),
            ("vpc", "in_vpc"),
            ("network_id", "in_vpc"),
            ("volume_id", "attached_to"),
            ("droplet_id", "attached_to"),
            ("parent_id", "depends_on"),
        ):
            value = meta.get(key)
            if value is None or value == "":
                continue
            out.append(
                (
                    str(value),
                    relation,
                    {"inferred_from": f"metadata.{key}", "source": "inferred_inventory"},
                )
            )

        # DO networks.v4 sometimes embeds vpc_uuid at top level after sync.
        networks = meta.get("networks")
        if isinstance(networks, dict):
            for key in ("vpc_uuid", "vpc_id"):
                if networks.get(key):
                    out.append(
                        (
                            str(networks[key]),
                            "in_vpc",
                            {
                                "inferred_from": f"metadata.networks.{key}",
                                "source": "inferred_inventory",
                            },
                        )
                    )

        for tag in tags:
            tag_s = str(tag)
            for prefix, relation in (
                ("vpc:", "in_vpc"),
                ("vpc_id:", "in_vpc"),
                ("vpc_uuid:", "in_vpc"),
                ("volume:", "attached_to"),
                ("droplet:", "attached_to"),
                ("depends_on:", "depends_on"),
            ):
                if tag_s.lower().startswith(prefix):
                    target = tag_s[len(prefix) :].strip()
                    if target:
                        out.append(
                            (
                                target,
                                relation,
                                {
                                    "inferred_from": f"tag:{prefix.rstrip(':')}",
                                    "source": "inferred_inventory",
                                },
                            )
                        )
        return out

    # --- helpers -------------------------------------------------------------
    def _load_resource(
        self, connection: Connection, project_id: str, resource_id: str
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, resource_type, external_id, name
                FROM external_resources
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": resource_id, "project_id": project_id},
        ).mappings().first()
        return dict(row) if row else None

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise ResourceTopologyError("Project not found.")
        return dict(row)

    @staticmethod
    def _require_text(value: Optional[str], field_name: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ResourceTopologyError(f"{field_name} must not be blank.")
        return cleaned

    @staticmethod
    def _serialize_edge(row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["metadata"] = payload.get("metadata") or {}
        return payload
