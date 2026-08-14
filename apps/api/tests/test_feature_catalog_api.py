"""HTTP surface for FeatureCatalog + capability_manifest (DOC-001 / ORC-003).

Covers:
* GET /v1/features?project_id=
* POST /v1/features/seed  (TRACEABILITY seed; actor required)
* POST /v1/features       (upsert; actor required)
* GET /v1/features/{label}
* POST /v1/features/{label}/documented  (actor required)
* GET /v1/capability-manifest
* GET /v1/capability-manifest/table  (text/plain or JSON)
"""

from __future__ import annotations


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def test_list_features_empty_then_seed(client):
    empty = client.get("/v1/features", params={"project_id": PROJECT_ID})
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["project_id"] == PROJECT_ID
    assert body["items"] == []
    assert body["count"] == 0

    denied = client.post(
        "/v1/features/seed",
        json={"project_id": PROJECT_ID, "actor": "cao"},
    )
    assert denied.status_code == 403, denied.text

    seeded = client.post(
        "/v1/features/seed",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert seeded.status_code == 200, seeded.text
    payload = seeded.json()
    assert payload["seeded"] is True
    assert payload["entry_count"] > 0
    assert payload["source"] is not None
    assert "TRACEABILITY" in (payload["source"] or "")
    assert len(payload["items"]) == payload["entry_count"]
    labels = {item["label"] for item in payload["items"] if item.get("label")}
    assert "MET-001" in labels or any(
        (item.get("label") or "").startswith("DOC-") for item in payload["items"]
    )

    listed = client.get("/v1/features", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200
    assert listed.json()["count"] == payload["entry_count"]


def test_upsert_get_and_mark_documented(client):
    upsert = client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "label": "TST-API-001",
            "title": "Feature catalog HTTP surface",
            "status": "Partial",
            "source": "test_feature_catalog_api",
            "implementation_files": ["apps/api/app/main.py"],
            "test_coverage": ["apps/api/tests/test_feature_catalog_api.py"],
            "actor": ACTOR,
        },
    )
    assert upsert.status_code == 201, upsert.text
    entry = upsert.json()
    assert entry["label"] == "TST-API-001"
    assert entry["title"] == "Feature catalog HTTP surface"
    assert entry["status"] == "Partial"
    assert entry["project_id"] == PROJECT_ID
    assert "feature" in entry["id"]

    missing_actor = client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "title": "No actor",
        },
    )
    assert missing_actor.status_code == 422

    got = client.get(
        f"/v1/features/{entry['label']}",
        params={"project_id": PROJECT_ID},
    )
    assert got.status_code == 200, got.text
    assert got.json()["id"] == entry["id"]
    assert got.json()["slug"] == entry["slug"]

    unknown = client.get(
        "/v1/features/NOT-A-REAL-LABEL",
        params={"project_id": PROJECT_ID},
    )
    assert unknown.status_code == 404

    documented = client.post(
        f"/v1/features/{entry['label']}/documented",
        json={
            "project_id": PROJECT_ID,
            "event_id": "evt_test_docs_1",
            "status": "Implemented",
            "actor": ACTOR,
        },
    )
    assert documented.status_code == 200, documented.text
    body = documented.json()
    assert body["last_documented_event_id"] == "evt_test_docs_1"
    assert body["last_documented_at"] is not None
    assert body["status"] == "Implemented"

    bad_status = client.post(
        f"/v1/features/{entry['label']}/documented",
        json={
            "project_id": PROJECT_ID,
            "event_id": "evt_test_docs_2",
            "status": "NotAStatus",
            "actor": ACTOR,
        },
    )
    assert bad_status.status_code == 422


def test_capability_manifest_json_and_table(client):
    # Seed a couple of catalog rows so informational capabilities appear.
    client.post(
        "/v1/features/seed",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "label": "CAP-API-001",
            "title": "Shipped capability for manifest",
            "status": "Implemented",
            "actor": ACTOR,
        },
    )

    defaulted = client.get("/v1/capability-manifest")
    assert defaulted.status_code == 200, defaulted.text
    default_body = defaulted.json()
    assert default_body["project_id"] == PROJECT_ID
    assert default_body["count"] >= 5
    ids = {item["id"] for item in default_body["capabilities"]}
    assert "code_task" in ids
    assert "meeting" in ids
    assert "prd" in ids
    assert "discovery" in ids
    assert "multi_phase_command" in ids
    assert "CAP-API-001" in ids
    shipped = next(
        item for item in default_body["capabilities"] if item["id"] == "CAP-API-001"
    )
    assert shipped["enabled"] is True
    assert shipped["initiator_tool"] is None

    scoped = client.get(
        "/v1/capability-manifest", params={"project_id": PROJECT_ID}
    )
    assert scoped.status_code == 200
    assert scoped.json()["count"] == default_body["count"]

    plain = client.get(
        "/v1/capability-manifest/table",
        params={"project_id": PROJECT_ID},
    )
    assert plain.status_code == 200, plain.text
    assert "text/plain" in plain.headers.get("content-type", "")
    assert "code_task" in plain.text
    assert "id | capability" in plain.text

    as_json = client.get(
        "/v1/capability-manifest/table",
        params={"project_id": PROJECT_ID},
        headers={"Accept": "application/json"},
    )
    assert as_json.status_code == 200, as_json.text
    table_body = as_json.json()
    assert "table" in table_body
    assert "code_task" in table_body["table"]
    assert table_body["project_id"] == PROJECT_ID
