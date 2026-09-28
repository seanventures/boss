"""HTTP surface for project delivery board + release planning.

Covers:
* GET  /v1/release-planning/status?project_id=
* GET  /v1/release-planning/board?project_id=
* POST /v1/release-planning/releases  (actor required)
* GET  /v1/release-planning/releases?project_id=
* GET  /v1/release-planning/releases/{id}
* POST /v1/release-planning/releases/{id}/link-issue
* POST /v1/release-planning/sync-from-features
* POST /v1/release-planning/sync-from-trackers  (fail soft)
"""

from __future__ import annotations


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def test_status_empty_board(client):
    res = client.get(
        "/v1/release-planning/status",
        params={"project_id": PROJECT_ID},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project_id"] == PROJECT_ID
    assert body["built_count"] == 0
    assert body["todo_count"] == 0
    assert body["in_progress"] == 0
    assert body["blocked"] == 0
    assert body["last_release"] is None
    assert "totals" in body
    lock = body["v1_lock"]
    assert set(lock) == {"in", "out", "cloud"}
    assert lock["in"] >= 65
    assert lock["out"] >= 14
    assert lock["cloud"] == 12


def test_board_columns_shape(client):
    res = client.get(
        "/v1/release-planning/board",
        params={"project_id": PROJECT_ID},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project_id"] == PROJECT_ID
    for col in ("built", "building", "planned", "blocked"):
        assert col in body["columns"]
        assert isinstance(body["columns"][col], list)
        assert body["counts"][col] == len(body["columns"][col])


def test_create_list_get_release(client):
    denied = client.post(
        "/v1/release-planning/releases",
        json={
            "project_id": PROJECT_ID,
            "name": "Denied release",
            "actor": "cao",
        },
    )
    assert denied.status_code == 403, denied.text

    missing_actor = client.post(
        "/v1/release-planning/releases",
        json={"project_id": PROJECT_ID, "name": "No actor"},
    )
    # actor has a default of "system" in the model — system may be authorized
    # depending on seed; accept 201/403/422. Prefer explicit actor in happy path.
    assert missing_actor.status_code in {201, 403, 422}

    created = client.post(
        "/v1/release-planning/releases",
        json={
            "project_id": PROJECT_ID,
            "name": "v1.2 Mission Control",
            "target_date": "2026-09-01",
            "item_ids": ["feat_a", "feat_b"],
            "actor": ACTOR,
        },
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["name"] == "v1.2 Mission Control"
    assert plan["target_date"] == "2026-09-01"
    assert plan["item_ids"] == ["feat_a", "feat_b"]
    assert plan["issue_links"] == []
    assert plan["status"] == "planned"
    assert plan["project_id"] == PROJECT_ID
    assert plan["id"].startswith("rel_")
    assert plan["created_by"] == ACTOR

    empty_name = client.post(
        "/v1/release-planning/releases",
        json={
            "project_id": PROJECT_ID,
            "name": "   ",
            "actor": ACTOR,
        },
    )
    assert empty_name.status_code == 422, empty_name.text

    listed = client.get(
        "/v1/release-planning/releases",
        params={"project_id": PROJECT_ID},
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["count"] >= 1
    assert any(item["id"] == plan["id"] for item in payload["items"])

    got = client.get(
        f"/v1/release-planning/releases/{plan['id']}",
        params={"project_id": PROJECT_ID},
    )
    assert got.status_code == 200, got.text
    detail = got.json()
    assert detail["id"] == plan["id"]
    assert "items" in detail

    missing = client.get(
        "/v1/release-planning/releases/rel_does_not_exist",
        params={"project_id": PROJECT_ID},
    )
    assert missing.status_code == 404

    # Status now shows last_release
    status = client.get(
        "/v1/release-planning/status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200
    assert status.json()["last_release"] is not None
    assert status.json()["last_release"]["id"] == plan["id"]
    assert status.json()["totals"]["release_plans"] >= 1


def test_link_issue_to_release(client):
    created = client.post(
        "/v1/release-planning/releases",
        json={
            "project_id": PROJECT_ID,
            "name": "Link-issue release",
            "actor": ACTOR,
        },
    )
    assert created.status_code == 201, created.text
    release_id = created.json()["id"]

    linked = client.post(
        f"/v1/release-planning/releases/{release_id}/link-issue",
        json={
            "project_id": PROJECT_ID,
            "tracker": "plane",
            "issue_key": "BOSS-999",
            "url": "https://plane.local.example.test/goldeneye/browse/BOSS-999",
            "actor": ACTOR,
        },
    )
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert len(body["issue_links"]) == 1
    link = body["issue_links"][0]
    assert link["tracker"] == "plane"
    assert link["issue_key"] == "BOSS-999"
    assert "BOSS-999" in link["url"]

    # Idempotent upsert on same tracker+key
    again = client.post(
        f"/v1/release-planning/releases/{release_id}/link-issue",
        json={
            "project_id": PROJECT_ID,
            "tracker": "plane",
            "issue_key": "BOSS-999",
            "url": "https://plane.local.example.test/goldeneye/browse/BOSS-999?v=2",
            "actor": ACTOR,
        },
    )
    assert again.status_code == 200
    assert len(again.json()["issue_links"]) == 1
    assert "v=2" in again.json()["issue_links"][0]["url"]

    # Default Plane URL when url omitted
    auto = client.post(
        f"/v1/release-planning/releases/{release_id}/link-issue",
        json={
            "project_id": PROJECT_ID,
            "tracker": "plane",
            "issue_key": "BOSS-1000",
            "actor": ACTOR,
        },
    )
    assert auto.status_code == 200
    assert len(auto.json()["issue_links"]) == 2
    plane_links = [
        ln for ln in auto.json()["issue_links"] if ln["issue_key"] == "BOSS-1000"
    ]
    assert plane_links and plane_links[0]["url"]

    bad = client.post(
        f"/v1/release-planning/releases/{release_id}/link-issue",
        json={
            "project_id": PROJECT_ID,
            "tracker": "",
            "issue_key": "X",
            "actor": ACTOR,
        },
    )
    assert bad.status_code == 422

    unknown = client.post(
        "/v1/release-planning/releases/rel_missing/link-issue",
        json={
            "project_id": PROJECT_ID,
            "tracker": "jira",
            "issue_key": "PROJ-1",
            "actor": ACTOR,
        },
    )
    assert unknown.status_code == 404


def test_sync_from_features_populates_board(client):
    # Seed catalog via features API
    client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "label": "REL-API-001",
            "title": "Release planning board",
            "status": "Implemented",
            "actor": ACTOR,
        },
    )
    client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "label": "REL-API-002",
            "title": "Release planning blocked item",
            "status": "Blocked",
            "actor": ACTOR,
        },
    )
    client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "label": "REL-API-003",
            "title": "Release planning planned item",
            "status": "Planned",
            "actor": ACTOR,
        },
    )
    client.post(
        "/v1/features",
        json={
            "project_id": PROJECT_ID,
            "label": "REL-API-004",
            "title": "Release planning partial item",
            "status": "Partial",
            "actor": ACTOR,
        },
    )

    synced = client.post(
        "/v1/release-planning/sync-from-features",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert synced.status_code == 200, synced.text
    body = synced.json()
    assert body["source"] == "features"
    assert body["upserted"] >= 4
    board = body["board"]
    titles_built = {i["title"] for i in board["columns"]["built"]}
    titles_blocked = {i["title"] for i in board["columns"]["blocked"]}
    titles_planned = {i["title"] for i in board["columns"]["planned"]}
    titles_building = {i["title"] for i in board["columns"]["building"]}
    assert "Release planning board" in titles_built
    assert "Release planning blocked item" in titles_blocked
    assert "Release planning planned item" in titles_planned
    assert "Release planning partial item" in titles_building

    # Status counts reflect board
    status = client.get(
        "/v1/release-planning/status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200
    snap = status.json()
    assert snap["built_count"] >= 1
    assert snap["blocked"] >= 1
    assert snap["todo_count"] >= 1
    assert snap["in_progress"] >= 1

    # GET board returns durable items now
    board_get = client.get(
        "/v1/release-planning/board",
        params={"project_id": PROJECT_ID},
    )
    assert board_get.status_code == 200
    assert board_get.json()["counts"]["built"] >= 1


def test_release_planning_http_e2e_and_v1_lock_summary(client):
    """HTTP E2E: status/board/releases 200 and GET /v1/v1-lock counts."""
    lock = client.get("/v1/v1-lock")
    assert lock.status_code == 200, lock.text
    body = lock.json()
    assert set(body) == {"in", "out", "cloud"}
    assert body["in"] >= 65
    assert body["out"] >= 14
    assert body["cloud"] == 12

    status = client.get(
        "/v1/release-planning/status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200, status.text
    assert status.json()["v1_lock"] == body

    board = client.get(
        "/v1/release-planning/board",
        params={"project_id": PROJECT_ID},
    )
    assert board.status_code == 200, board.text
    assert set(board.json()["columns"]) >= {"built", "building", "planned", "blocked"}

    releases = client.get(
        "/v1/release-planning/releases",
        params={"project_id": PROJECT_ID},
    )
    assert releases.status_code == 200, releases.text
    assert "items" in releases.json()
    assert releases.json()["count"] == len(releases.json()["items"])


def test_oss_vs_commercial_rows_are_not_community_shipped(client):
    """Cloud/Enterprise lock rows must never land as Community shipped.

    Catalog may say Implemented; the v1 lock still keeps ✦ rows off `built`
    with community_shipped=false. Community ✅ rows may be built.
    """
    community_title = "Durable tasks, plans, jobs (Postgres)"
    cloud_title = "Managed multi-tenant model gateway"
    enterprise_title = "Self-improvement automation routes"

    for label, title in (
        ("V1-IN-001", community_title),
        ("V1-CE-001", cloud_title),
        ("V1-CE-005", enterprise_title),
    ):
        seeded = client.post(
            "/v1/features",
            json={
                "project_id": PROJECT_ID,
                "label": label,
                "title": title,
                "status": "Implemented",
                "actor": ACTOR,
            },
        )
        assert seeded.status_code in {200, 201}, seeded.text

    synced = client.post(
        "/v1/release-planning/sync-from-features",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert synced.status_code == 200, synced.text
    board = synced.json()["board"]

    def _by_title(title: str) -> dict:
        matches = [
            item
            for col in board["columns"].values()
            for item in col
            if item["title"] == title
        ]
        assert matches, f"missing board row for {title!r}"
        return matches[0]

    community = _by_title(community_title)
    cloud = _by_title(cloud_title)
    enterprise = _by_title(enterprise_title)

    assert community["column"] == "built"
    assert community["metadata"].get("community_shipped") is True
    assert community["metadata"].get("v1_lock") == "in"

    assert cloud["column"] != "built"
    assert cloud["metadata"].get("community_shipped") is False
    assert cloud["metadata"].get("v1_lock") == "cloud_enterprise"
    assert cloud["metadata"].get("v1_features_mark") == "✦"

    assert enterprise["column"] != "built"
    assert enterprise["metadata"].get("community_shipped") is False
    assert enterprise["metadata"].get("v1_lock") == "cloud_enterprise"

    status = client.get(
        "/v1/release-planning/status",
        params={"project_id": PROJECT_ID},
    )
    assert status.status_code == 200, status.text
    listed = client.get(
        "/v1/release-planning/releases",
        params={"project_id": PROJECT_ID},
    )
    assert listed.status_code == 200, listed.text
    board_get = client.get(
        "/v1/release-planning/board",
        params={"project_id": PROJECT_ID},
    )
    assert board_get.status_code == 200, board_get.text
    durable_titles_built = {
        item["title"] for item in board_get.json()["columns"]["built"]
    }
    assert cloud_title not in durable_titles_built
    assert enterprise_title not in durable_titles_built


def test_sync_from_trackers_fail_soft(client):
    """Tracker sync always returns ok=True even with empty bindings."""
    res = client.post(
        "/v1/release-planning/sync-from-trackers",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["source"] == "trackers"
    assert "upserted" in body
    assert "errors" in body
    assert "board" in body

    # Seed a work item then sync again
    from apps.api.app.config import get_settings
    from apps.api.app.services.work_orchestration import WorkOrchestrationService
    from sqlalchemy import create_engine

    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    service = WorkOrchestrationService(settings)
    with engine.begin() as connection:
        service.upsert_work_item(
            connection,
            project_id=PROJECT_ID,
            subject_type="feature",
            subject_id="rel-planning-wi",
            title="Release planning work item",
            status="active",
            strategic_value=80,
            customer_impact=70,
            urgency=60,
            cost_of_delay=50,
            risk_reduction=40,
            dependency_pressure=30,
            confidence=90,
            actor=ACTOR,
        )

    res2 = client.post(
        "/v1/release-planning/sync-from-trackers",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["ok"] is True
    assert body2["upserted"] >= 1
    titles = {
        item["title"]
        for col in body2["board"]["columns"].values()
        for item in col
    }
    assert "Release planning work item" in titles
