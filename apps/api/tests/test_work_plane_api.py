"""BOSS-331 / BOSS-358 — Native Work Plane: initiatives, modules, cycles, roadmaps."""
from __future__ import annotations

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"


def test_work_plane_status_and_board_empty(client):
    status = client.get(
        "/v1/work-plane/status", params={"project_id": PROJECT_ID}
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["project_id"] == PROJECT_ID
    assert body["built_count"] == 0
    assert body["todo_count"] == 0
    assert "totals" in body
    assert body["totals"]["initiatives"] == 0

    board = client.get(
        "/v1/work-plane/board", params={"project_id": PROJECT_ID}
    )
    assert board.status_code == 200, board.text
    for col in ("built", "building", "planned", "blocked"):
        assert col in board.json()["columns"]


def test_initiatives_modules_cycles_roadmap_and_deps(client):
    denied = client.post(
        "/v1/work-plane/initiatives",
        json={
            "project_id": PROJECT_ID,
            "title": "Denied",
            "actor": "cao",
        },
    )
    assert denied.status_code == 403, denied.text

    initiative = client.post(
        "/v1/work-plane/initiatives",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "Platform Reliability",
            "key": "platform-reliability",
            "status": "active",
            "owner": "sre-lead",
            "target_date": "2026-12-01",
            "tracker": "plane",
            "issue_key": "BOSS-331",
        },
    )
    assert initiative.status_code == 201, initiative.text
    init = initiative.json()
    assert init["id"].startswith("wpi_")
    assert init["key"] == "platform-reliability"
    assert init["status"] == "active"
    assert init["column"] == "building"
    assert init["issue_key"] == "BOSS-331"
    assert init["issue_url"]  # defaulted from tracker+key
    assert init["entity_type"] == "initiative"

    module = client.post(
        "/v1/work-plane/modules",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "Knowledge Graph",
            "key": "knowledge-graph",
            "initiative_id": init["id"],
            "status": "building",
        },
    )
    assert module.status_code == 201, module.text
    mod = module.json()
    assert mod["initiative_id"] == init["id"]
    assert mod["entity_type"] == "module"

    cycle = client.post(
        "/v1/work-plane/cycles",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "August Cycle",
            "key": "2026-08",
            "status": "active",
            "starts_on": "2026-08-01",
            "ends_on": "2026-08-31",
        },
    )
    assert cycle.status_code == 201, cycle.text
    cyc = cycle.json()
    assert cyc["starts_on"] == "2026-08-01"

    item = client.post(
        "/v1/work-plane/roadmap-items",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "Provenance + temporal validity",
            "key": "kg-provenance",
            "status": "planned",
            "initiative_id": init["id"],
            "module_id": mod["id"],
            "cycle_id": cyc["id"],
            "sort_order": 10,
            "tracker": "plane",
            "issue_key": "BOSS-355",
        },
    )
    assert item.status_code == 201, item.text
    road = item.json()
    assert road["module_id"] == mod["id"]
    assert road["cycle_id"] == cyc["id"]
    assert road["issue_key"] == "BOSS-355"
    assert road["column"] == "planned"

    # Dependency: roadmap item depends on module readiness
    dep = client.post(
        "/v1/work-plane/dependencies",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "src_type": "roadmap_item",
            "src_id": road["id"],
            "dst_type": "module",
            "dst_id": mod["id"],
            "kind": "depends_on",
        },
    )
    assert dep.status_code == 201, dep.text
    assert dep.json()["kind"] == "depends_on"

    # Link issue on cycle
    linked = client.post(
        "/v1/work-plane/link-issue",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "entity_type": "cycle",
            "entity_id": cyc["id"],
            "tracker": "github",
            "issue_key": "42",
            "url": "https://github.com/example/boss/issues/42",
        },
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["issue_url"] == "https://github.com/example/boss/issues/42"
    assert linked.json()["tracker"] == "github"

    # Lists
    inits = client.get(
        "/v1/work-plane/initiatives", params={"project_id": PROJECT_ID}
    )
    assert inits.status_code == 200
    assert inits.json()["count"] >= 1

    mods = client.get(
        "/v1/work-plane/modules",
        params={"project_id": PROJECT_ID, "initiative_id": init["id"]},
    )
    assert mods.status_code == 200
    assert any(m["id"] == mod["id"] for m in mods.json()["items"])

    roads = client.get(
        "/v1/work-plane/roadmap-items",
        params={"project_id": PROJECT_ID, "cycle_id": cyc["id"]},
    )
    assert roads.status_code == 200
    assert roads.json()["count"] >= 1

    deps = client.get(
        "/v1/work-plane/dependencies",
        params={
            "project_id": PROJECT_ID,
            "entity_type": "roadmap_item",
            "entity_id": road["id"],
        },
    )
    assert deps.status_code == 200
    assert deps.json()["count"] >= 1

    # Status + board + overview populated
    status = client.get(
        "/v1/work-plane/status", params={"project_id": PROJECT_ID}
    )
    assert status.status_code == 200
    st = status.json()
    assert st["totals"]["initiatives"] >= 1
    assert st["totals"]["modules"] >= 1
    assert st["totals"]["cycles"] >= 1
    assert st["totals"]["roadmap_items"] >= 1
    assert st["totals"]["dependencies"] >= 1
    assert st["in_progress"] >= 1  # active initiative/module/cycle
    assert st["todo_count"] >= 1  # planned roadmap item

    board = client.get(
        "/v1/work-plane/board", params={"project_id": PROJECT_ID}
    )
    assert board.status_code == 200
    b = board.json()
    assert b["counts"]["building"] >= 1
    assert b["counts"]["planned"] >= 1

    overview = client.get(
        "/v1/work-plane/overview", params={"project_id": PROJECT_ID}
    )
    assert overview.status_code == 200
    ov = overview.json()
    assert len(ov["initiatives"]) >= 1
    assert len(ov["roadmap_items"]) >= 1
    assert "board" in ov and "status" in ov


def test_work_plane_validation(client):
    bad_status = client.post(
        "/v1/work-plane/initiatives",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "Bad status",
            "status": "not-a-status",
        },
    )
    assert bad_status.status_code == 422

    missing_entity = client.post(
        "/v1/work-plane/modules",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "Orphan module",
            "initiative_id": "wpi_does_not_exist",
        },
    )
    assert missing_entity.status_code == 404

    self_dep = client.post(
        "/v1/work-plane/dependencies",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "src_type": "initiative",
            "src_id": "wpi_x",
            "dst_type": "initiative",
            "dst_id": "wpi_x",
        },
    )
    # Either 404 (entity missing) or 422 (self dep) is fine depending on order
    assert self_dep.status_code in {404, 422}
