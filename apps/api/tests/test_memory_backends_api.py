"""External memory backends through the HTTP surface (BOSSAPP-892)."""
from __future__ import annotations

import uuid

PROJECT = "boss-dogfood"


def test_configure_list_review_and_refuse_a_bad_credential(client, engine):
    from boss_engine.services import agent_memory

    name = f"mem0-{uuid.uuid4().hex[:6]}"
    made = client.put("/v1/memory-backends", json={
        "project_id": PROJECT, "actor": "ceo", "name": name, "kind": "mem0",
        # Nothing listens here: an approved send fails and stays approved, to be retried.
        "base_url": "http://127.0.0.1:9"})
    assert made.status_code == 200, made.text
    backend_id = made.json()["id"]
    body = f"Deploys go out on Tuesdays {uuid.uuid4().hex[:6]}."
    with engine.begin() as c:
        agent_memory.remember(c, project_id=PROJECT, actor="ceo", kind="project", body=body)
    listed = client.get("/v1/memory-backends", params={"project_id": PROJECT}).json()
    waiting = [e for e in listed["awaiting_review"] if e["body"] == body]
    assert len(waiting) == 1 and any(b["id"] == backend_id for b in listed["backends"])
    reviewed = client.post(f"/v1/memory-backends/{backend_id}/review", json={
        "project_id": PROJECT, "actor": "ceo", "export_ids": [waiting[0]["id"]],
        "decision": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "approved" and reviewed.json()["sync"]["failed"] == 1
    refused = client.put("/v1/memory-backends", json={
        "project_id": PROJECT, "actor": "ceo", "name": "x", "kind": "mem0",
        "base_url": "https://api.mem0.ai", "credential_env": "DATABASE_URL"})
    assert refused.status_code == 422
    # Leave the shared project as it was: no backend mirroring its memory.
    client.put("/v1/memory-backends", json={
        "project_id": PROJECT, "actor": "ceo", "name": name, "kind": "mem0",
        "base_url": "http://127.0.0.1:9", "enabled": False})


def test_an_unknown_backend_is_404(client):
    r = client.post("/v1/memory-backends/mbk_nope/sync",
                    json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 404
