"""No-spend meeting fixture + existing /run phase evidence."""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.db import begin


PROJECT_ID = "boss-dogfood"
PHASES = ["research", "round1", "round2", "synthesis", "draft", "finalize"]


def test_existing_run_persists_phase_timeline_and_receipt(client, engine):
    created = client.post(
        "/v1/meetings",
        json={
            "project_id": PROJECT_ID,
            "title": "Phased review",
            "purpose": "Record a full panel transcript.",
            "participants": ["ceo", "program_manager"],
            "agenda": PHASES,
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    meeting_id = created.json()["id"]
    transcript = [
        {"speaker": "researcher", "message": f"{phase} note", "phase": phase}
        for phase in PHASES
    ]
    run = client.post(
        f"/v1/meetings/{meeting_id}/run",
        json={
            "actor": "ceo",
            "transcript": transcript,
            "minutes": {"summary": "Phased fixture via canonical run.", "simulated": True},
        },
    )
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["status"] == "completed"
    assert [item["phase"] for item in payload["phase_timeline"]] == PHASES
    assert payload["receipt_id"]
    assert {item["kind"] for item in payload["artifact_links"]} >= {"transcript", "minutes"}
    with begin(engine) as connection:
        receipt = connection.execute(
            text("SELECT dedupe_key, effect_kind FROM receipts WHERE id = :id"),
            {"id": payload["receipt_id"]},
        ).mappings().one()
    assert receipt["dedupe_key"] == f"meeting.run:{PROJECT_ID}:{meeting_id}"
    assert receipt["effect_kind"] == "meeting.run"


def test_simulated_run_walks_all_phases_without_llm(client, engine):
    created = client.post(
        "/v1/meetings/simulated-run",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "directive": "Prove the panel state machine without a model.",
            "simulated": True,
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["simulated"] is True
    assert payload["llm_called"] is False
    assert payload["status"] == "simulated"
    assert payload["phase"] == "done"
    assert [item["phase"] for item in payload["phase_timeline"]] == PHASES
    assert all(item.get("artifact_id") for item in payload["phase_timeline"])
    assert payload["receipt_id"]
    assert payload["minutes_artifact_id"]
    kinds = {item["kind"] for item in payload["artifact_links"]}
    assert kinds >= set(PHASES) | {"transcript", "minutes"}

    evidence = client.get(
        f"/v1/meetings/{payload['meeting_id']}/evidence",
        params={"project_id": PROJECT_ID},
    )
    assert evidence.status_code == 200, evidence.text
    body = evidence.json()
    assert body["simulated"] is True
    assert [item["phase"] for item in body["phase_timeline"]] == PHASES
    assert body["receipt_id"] == payload["receipt_id"]

    # Idempotent: same meeting does not create a second spendy run.
    again = client.post(
        f"/v1/meetings/{payload['meeting_id']}/simulated-run",
        json={"project_id": PROJECT_ID, "actor": "ceo", "simulated": True},
    )
    assert again.status_code == 200, again.text
    assert again.json()["already"] is True
    assert again.json()["receipt_id"] == payload["receipt_id"]

    with begin(engine) as connection:
        jobs = connection.execute(
            text("SELECT count(*) FROM jobs WHERE payload->>'run_id' = :id"),
            {"id": payload["run_id"]},
        ).scalar_one()
        usage = connection.execute(
            text("SELECT usage FROM meeting_runs WHERE id = :id"),
            {"id": payload["run_id"]},
        ).scalar_one()
    assert int(jobs) == 0
    assert int((usage or {}).get("cost_usd_micros") or 0) == 0


def test_simulated_run_rejects_live_flag(client):
    response = client.post(
        "/v1/meetings/simulated-run",
        json={"project_id": PROJECT_ID, "actor": "ceo", "simulated": False},
    )
    assert response.status_code == 422
