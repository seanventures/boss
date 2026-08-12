"""BOSS-240: durable ProviderOps resource topology graph."""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services.core import make_id

PROJECT_ID = "boss-dogfood"


def _insert_resource(
    engine,
    *,
    resource_id: str | None = None,
    provider: str = "digitalocean",
    resource_type: str = "droplet",
    external_id: str,
    name: str,
    tags: list | None = None,
    metadata: dict | None = None,
) -> str:
    rid = resource_id or make_id("eres")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO external_resources (
                    id, project_id, provider, resource_type, external_id, name,
                    state, ownership, environment, health_status, tags, metadata
                )
                VALUES (
                    :id, :project_id, :provider, :resource_type, :external_id, :name,
                    'active', 'discovered_unmanaged', 'unknown', 'unknown',
                    CAST(:tags AS JSONB), CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": rid,
                "project_id": PROJECT_ID,
                "provider": provider,
                "resource_type": resource_type,
                "external_id": external_id,
                "name": name,
                "tags": json.dumps(tags or []),
                "metadata": json.dumps(metadata or {}),
            },
        )
    return rid


def test_resource_topology_upsert_list_delete_with_receipts(client, engine):
    droplet_id = _insert_resource(
        engine,
        resource_type="droplet",
        external_id="drop-100",
        name="app-web-1",
        metadata={"region": "nyc3"},
    )
    vpc_id = _insert_resource(
        engine,
        resource_type="vpc",
        external_id="vpc-abc",
        name="default-vpc",
        metadata={"region": "nyc3"},
    )

    created = client.post(
        "/v1/resource-topology/edges",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "from_resource_id": droplet_id,
            "to_resource_id": vpc_id,
            "relation_type": "in_vpc",
            "provider": "digitalocean",
            "metadata": {"note": "manual link"},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["receipt_id"].startswith("rcpt_")
    assert body["deleted"] is False
    edge = body["edge"]
    assert edge["from_resource_id"] == droplet_id
    assert edge["to_resource_id"] == vpc_id
    assert edge["relation_type"] == "in_vpc"
    assert edge["provider"] == "digitalocean"
    assert edge["metadata"]["note"] == "manual link"
    edge_id = edge["id"]

    # Idempotent upsert (same natural key) reuses edge id + receipt dedupe.
    again = client.post(
        "/v1/resource-topology/edges",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "from_resource_id": droplet_id,
            "to_resource_id": vpc_id,
            "relation_type": "in_vpc",
            "provider": "digitalocean",
            "metadata": {"note": "updated"},
        },
    )
    assert again.status_code == 200
    assert again.json()["edge"]["id"] == edge_id
    assert again.json()["edge"]["metadata"]["note"] == "updated"
    # Same receipt dedupe_key → same receipt id
    assert again.json()["receipt_id"] == body["receipt_id"]

    graph = client.get(
        "/v1/resource-topology",
        params={"project_id": PROJECT_ID},
    )
    assert graph.status_code == 200
    summary = graph.json()
    assert summary["edge_count"] == 1
    assert summary["node_count"] == 2
    assert summary["relation_counts"]["in_vpc"] == 1
    node_ids = {n["id"] for n in summary["nodes"]}
    assert node_ids == {droplet_id, vpc_id}
    assert summary["edges"][0]["id"] == edge_id

    # Receipt was recorded for the mutation
    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                """
                SELECT id, effect_kind, status, result
                FROM receipts
                WHERE id = :id
                """
            ),
            {"id": body["receipt_id"]},
        ).mappings().one()
    assert receipt["effect_kind"] == "resource_topology.edge.upsert"
    assert receipt["status"] == "succeeded"

    deleted = client.delete(
        f"/v1/resource-topology/edges/{edge_id}",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert deleted.status_code == 200, deleted.text
    del_body = deleted.json()
    assert del_body["deleted"] is True
    assert del_body["edge"]["id"] == edge_id
    assert del_body["receipt_id"].startswith("rcpt_")

    empty = client.get(
        "/v1/resource-topology",
        params={"project_id": PROJECT_ID},
    )
    assert empty.status_code == 200
    assert empty.json()["edge_count"] == 0
    assert empty.json()["node_count"] == 0

    missing = client.delete(
        f"/v1/resource-topology/edges/{edge_id}",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert missing.status_code == 404


def test_resource_topology_rejects_unknown_resources_and_self_loops(client, engine):
    droplet_id = _insert_resource(
        engine,
        resource_type="droplet",
        external_id="drop-200",
        name="only-one",
    )
    bad = client.post(
        "/v1/resource-topology/edges",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "from_resource_id": droplet_id,
            "to_resource_id": "eres_does_not_exist",
            "relation_type": "in_vpc",
        },
    )
    assert bad.status_code == 404

    loop = client.post(
        "/v1/resource-topology/edges",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "from_resource_id": droplet_id,
            "to_resource_id": droplet_id,
            "relation_type": "linked_to",
        },
    )
    assert loop.status_code == 422  # pydantic validation


def test_resource_topology_infer_from_inventory_metadata_and_tags(client, engine):
    vpc_id = _insert_resource(
        engine,
        resource_type="vpc",
        external_id="vpc-xyz",
        name="prod-vpc",
    )
    droplet_id = _insert_resource(
        engine,
        resource_type="droplet",
        external_id="drop-300",
        name="worker-1",
        metadata={"vpc_uuid": "vpc-xyz", "region": "sfo3"},
        tags=["env:prod"],
    )
    volume_id = _insert_resource(
        engine,
        resource_type="volume",
        external_id="vol-9",
        name="data-vol",
        tags=["droplet:drop-300"],
    )

    inferred = client.post(
        "/v1/resource-topology/infer",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "provider": "digitalocean",
        },
    )
    assert inferred.status_code == 200, inferred.text
    payload = inferred.json()
    assert payload["resources_scanned"] == 3
    assert payload["edges_upserted"] >= 2

    relations = {(e["from_resource_id"], e["to_resource_id"], e["relation_type"]) for e in payload["edges"]}
    assert (droplet_id, vpc_id, "in_vpc") in relations
    assert (volume_id, droplet_id, "attached_to") in relations

    graph = client.get(
        "/v1/resource-topology",
        params={"project_id": PROJECT_ID, "provider": "digitalocean"},
    )
    assert graph.status_code == 200
    assert graph.json()["edge_count"] >= 2

    # Re-infer is idempotent (no duplicate edges for same natural key).
    again = client.post(
        "/v1/resource-topology/infer",
        json={"project_id": PROJECT_ID, "actor": "ceo", "provider": "digitalocean"},
    )
    assert again.status_code == 200
    graph2 = client.get(
        "/v1/resource-topology",
        params={"project_id": PROJECT_ID},
    )
    assert graph2.json()["edge_count"] == graph.json()["edge_count"]


def test_resource_topology_service_list_edges_filters(engine):
    service = api_main.resource_topology
    a = _insert_resource(
        engine, resource_type="droplet", external_id="d1", name="a"
    )
    b = _insert_resource(
        engine, resource_type="vpc", external_id="v1", name="b"
    )
    c = _insert_resource(
        engine, resource_type="droplet", external_id="d2", name="c"
    )
    with engine.begin() as connection:
        service.upsert_edge(
            connection,
            project_id=PROJECT_ID,
            from_resource_id=a,
            to_resource_id=b,
            relation_type="in_vpc",
            provider="digitalocean",
            actor="ceo",
        )
        service.upsert_edge(
            connection,
            project_id=PROJECT_ID,
            from_resource_id=c,
            to_resource_id=b,
            relation_type="in_vpc",
            provider="digitalocean",
            actor="ceo",
        )
        service.upsert_edge(
            connection,
            project_id=PROJECT_ID,
            from_resource_id=a,
            to_resource_id=c,
            relation_type="depends_on",
            provider="digitalocean",
            actor="ceo",
        )
        only_from_a = service.list_edges(
            connection, PROJECT_ID, from_resource_id=a
        )
        only_to_b = service.list_edges(
            connection, PROJECT_ID, to_resource_id=b
        )
        only_depends = service.list_edges(
            connection, PROJECT_ID, relation_type="depends_on"
        )
    assert len(only_from_a) == 2
    assert len(only_to_b) == 2
    assert len(only_depends) == 1
    assert only_depends[0]["from_resource_id"] == a
    assert only_depends[0]["to_resource_id"] == c
