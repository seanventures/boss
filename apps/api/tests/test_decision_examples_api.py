"""Decision examples through the HTTP surface (BOSSAPP-893)."""
from __future__ import annotations

import json
import uuid

PROJECT = "boss-dogfood"


def _decision(engine, question: str) -> str:
    from boss_engine.services.core import create_blocking_decision

    with engine.begin() as c:
        return create_blocking_decision(
            c, PROJECT, None, question,
            {"action": "review_deployment", "summary": "owner bob@example.com"}, {},
            "deploy_gate", risk_tier=1)


def test_reject_is_captured_saved_examples_list_and_export_is_anonymized(client, engine):
    tag = uuid.uuid4().hex[:8]
    rejected = _decision(engine, f"Deploy {tag}?")
    r = client.post(f"/v1/decisions/{rejected}/reject",
                    json={"actor": "ceo", "note": "not on a Friday"})
    assert r.status_code == 200, r.text
    approved = _decision(engine, f"Merge {tag}?")
    assert client.post(f"/v1/decisions/{approved}/approve",
                       json={"actor": "ceo"}).status_code == 200
    saved = client.post(f"/v1/decisions/{approved}/example",
                        json={"project_id": PROJECT, "actor": "ceo", "label": "good call"})
    assert saved.status_code == 200, saved.text
    listed = client.get("/v1/decision-examples", params={"project_id": PROJECT}).json()
    mine = {e["decision_id"]: e for e in listed["examples"]
            if e["decision_id"] in (rejected, approved)}
    assert mine[rejected]["source"] == "override" and mine[rejected]["outcome"] == "reject"
    assert mine[approved]["source"] == "saved" and mine[approved]["label"] == "good call"
    exported = client.get("/v1/decision-examples/export", params={"project_id": PROJECT})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in exported.text.strip().splitlines()]
    assert any(line["question"] == f"Deploy {tag}?" and line["note"] == "not on a Friday"
               for line in lines)
    assert "bob@example.com" not in exported.text and PROJECT not in exported.text
    gone = client.delete(f"/v1/decision-examples/{mine[approved]['id']}",
                         params={"project_id": PROJECT, "actor": "ceo"})
    assert gone.status_code == 200


def test_a_pending_decision_cannot_be_saved(client, engine):
    pending = _decision(engine, f"Pending {uuid.uuid4().hex[:8]}?")
    r = client.post(f"/v1/decisions/{pending}/example",
                    json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 422 and "answered" in r.json()["detail"]
