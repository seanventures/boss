"""People review the Recommended roadmap over HTTP (BOSSAPP-911/912; routers/ambient_learning.py).

* The roadmap lists ranked recommendations as small diagrams; one opens with its diagram and
  the drawer's detail; detector stats read under autonomy:read.
* An operator accepts (as ticket / epic / PRD), says "not now", or rejects with a reason (a
  reason is required; a decided item cannot be decided again), runs the detectors now and
  sweeps outcomes. The scorecard carries the outcome metrics, per task class on request.
* A viewer reads but cannot review or run anything. The demo seed needs autonomy:manage and a
  demo project. The tick is the worker's alone. Another project's recommendation is not found.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from boss_engine.services.ambient import roadmap
from boss_engine.services.ambient import roadmap_demo as demo


def _world(engine):
    from apps.api.tests.identity_fixtures import IdentityWorld

    return IdentityWorld.create(engine)


def _recommendations(engine, project: str, n: int = 2) -> list[str]:
    with engine.begin() as c:
        for k in range(n):
            cause = f"http.cause.{uuid4().hex[:6]}"
            for d in (1, 2):
                demo.put_incident(c, project, title=f"Timeout {k}", cause_key=cause,
                                  at=datetime.now(timezone.utc) - timedelta(days=d + k))
        return roadmap.run(c, project, only=["incident_cause"])["opened"]


def test_an_operator_reviews_the_roadmap_and_the_scorecard_shows_it(client, engine):
    world = _world(engine)
    pa = world.project_a
    ids = _recommendations(engine, pa, 3)
    operator = world.person(f"operator-{uuid4().hex[:6]}")
    world.bind(operator, "operator", project=pa)
    with world.acting_as(world.ctx(operator)):
        listed = client.get("/v1/ambient/roadmap", params={"project_id": pa})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert {i["id"] for i in body["items"]} == set(ids)
        assert body["items"][0]["rank"] == 1 and body["items"][0]["strip"]
        assert body["counts"]["open"] == 3 and body["detectors"]
        one = client.get(f"/v1/ambient/roadmap/{ids[0]}", params={"project_id": pa})
        assert one.status_code == 200 and {"nodes", "edges", "detail"} <= set(one.json())

        accepted = client.post(f"/v1/ambient/roadmap/{ids[0]}/accept",
                               json={"project_id": pa, "as": "ticket", "note": "Do it."})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["recommendation"]["status"] == "accepted"
        assert accepted.json()["decision"]["decided_by"] == f"person:{operator.id}"
        assert client.post(f"/v1/ambient/roadmap/{ids[0]}/accept",
                           json={"project_id": pa}).status_code == 409

        assert client.post(f"/v1/ambient/roadmap/{ids[1]}/reject",
                           json={"project_id": pa}).status_code == 422
        assert client.post(f"/v1/ambient/roadmap/{ids[1]}/reject",
                           json={"project_id": pa, "reason": "x",
                                 "category": "bogus"}).status_code == 422
        rejected = client.post(f"/v1/ambient/roadmap/{ids[1]}/reject",
                               json={"project_id": pa, "reason": "Vendor bug.",
                                     "category": "wrong"})
        assert rejected.status_code == 200, rejected.text

        until = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        snoozed = client.post(f"/v1/ambient/roadmap/{ids[2]}/snooze",
                              json={"project_id": pa, "until": until})
        assert snoozed.status_code == 200 and \
            snoozed.json()["recommendation"]["status"] == "snoozed"

        ran = client.post("/v1/ambient/roadmap/run", json={"project_id": pa,
                                                           "only": ["incident_cause"]})
        assert ran.status_code == 200 and ran.json()["opened"] == []
        swept = client.post("/v1/ambient/outcomes/sweep", json={"project_id": pa})
        assert swept.status_code == 200 and "ignored" in swept.json()["outcomes"]

        card = client.get("/v1/ambient/scorecard", params={"project_id": pa, "days": 7,
                                                           "task_class": "roadmap_item"})
        assert card.status_code == 200, card.text
        out = card.json()["outcomes"]
        assert out["task_class"] == "roadmap_item" and out["decided"] == 3
        assert out["acceptance_pct"] == 50.0
        assert out["recommendations"]["accepted"] == 1
        assert out["recommendations"]["rejected"] == 1
        stats = client.get("/v1/ambient/roadmap/detectors", params={"project_id": pa})
        assert stats.status_code == 200
        by = {d["detector"]: d for d in stats.json()["detectors"]}
        assert by["incident_cause"]["accepted"] == 1 and by["incident_cause"]["rejected"] == 1
        assert client.post("/v1/ambient/roadmap/demo-seed",
                           json={"project_id": pa}).status_code == 403


def test_a_viewer_reads_the_roadmap_but_cannot_review_or_run_it(client, engine):
    world = _world(engine)
    pa = world.project_a
    ids = _recommendations(engine, pa, 1)
    viewer = world.person(f"viewer-{uuid4().hex[:6]}")
    world.bind(viewer, "viewer", project=pa)
    with world.acting_as(world.ctx(viewer)):
        assert client.get("/v1/ambient/roadmap", params={"project_id": pa}).status_code == 200
        assert client.get(f"/v1/ambient/roadmap/{ids[0]}",
                          params={"project_id": pa}).status_code == 200
        assert client.get("/v1/ambient/roadmap/detectors",
                          params={"project_id": pa}).status_code == 200
        for path, body in ((f"/v1/ambient/roadmap/{ids[0]}/accept", {}),
                           (f"/v1/ambient/roadmap/{ids[0]}/reject", {"reason": "no"}),
                           (f"/v1/ambient/roadmap/{ids[0]}/snooze",
                            {"until": "2099-01-01T00:00:00+00:00"}),
                           ("/v1/ambient/roadmap/run", {}),
                           ("/v1/ambient/outcomes/sweep", {}),
                           ("/v1/ambient/roadmap/demo-seed", {})):
            got = client.post(path, json={"project_id": pa, **body})
            assert got.status_code == 403, (path, got.text)
        assert client.post("/v1/ambient/learning/tick", json={}).status_code == 403


def test_an_owner_seeds_only_a_demo_project_and_another_projects_item_is_not_found(
        client, engine, monkeypatch):
    world = _world(engine)
    pa, pb = world.project_a, world.project_b
    foreign = _recommendations(engine, pb, 1)
    owner = world.person(f"owner-{uuid4().hex[:6]}")
    world.bind(owner, "owner", project=pa)
    monkeypatch.delenv("BOSS_DEMO_DATA", raising=False)
    with world.acting_as(world.ctx(owner)):
        refused = client.post("/v1/ambient/roadmap/demo-seed", json={"project_id": pa})
        assert refused.status_code == 403 and "demo" in refused.text
        assert client.get(f"/v1/ambient/roadmap/{foreign[0]}",
                          params={"project_id": pa}).status_code == 404
        assert client.post(f"/v1/ambient/roadmap/{foreign[0]}/accept",
                           json={"project_id": pa}).status_code == 404
        monkeypatch.setenv("BOSS_DEMO_DATA", "1")
        seeded = client.post("/v1/ambient/roadmap/demo-seed", json={"project_id": pa})
        assert seeded.status_code == 200, seeded.text
        assert len(seeded.json()["report"]["opened"]) >= 8
