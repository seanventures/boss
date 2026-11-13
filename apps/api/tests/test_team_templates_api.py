"""Team templates through the HTTP surface (BOSSAPP-894)."""
from __future__ import annotations

PROJECT = "boss-dogfood"
PACK = "delivery_team_pack"


def test_dry_run_apply_and_drift(client):
    body = {"project_id": PROJECT, "actor": "ceo", "manager": "person:ceo"}
    dry = client.post(f"/v1/packs/{PACK}/team", json={**body, "mode": "dry_run"})
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] and dry.json()["error"] is None
    stale = client.post(f"/v1/packs/{PACK}/team",
                        json={**body, "mode": "apply", "plan_digest": "sha256:nope"})
    assert stale.status_code == 409
    applied = client.post(f"/v1/packs/{PACK}/team", json={
        **body, "mode": "apply", "plan_digest": dry.json()["plan_digest"]})
    assert applied.status_code == 200, applied.text
    drift = client.get(f"/v1/packs/{PACK}/drift", params={"project_id": PROJECT})
    assert drift.status_code == 200 and drift.json()["applied"]
    assert drift.json()["drifted"] == 0


def test_a_pack_without_a_team_is_refused_in_words(client):
    r = client.post("/v1/packs/archetypes_pack/team",
                    json={"project_id": PROJECT, "actor": "ceo", "mode": "dry_run"})
    assert r.status_code == 422 and "no org or starter_tasks" in r.json()["detail"]
    missing = client.post("/v1/packs/no_such_pack/team",
                          json={"project_id": PROJECT, "actor": "ceo", "mode": "dry_run"})
    assert missing.status_code == 404
