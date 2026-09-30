"""Distributed job lease + enqueue dedupe.

Lease / heartbeat / reclaim coverage already lives in test_api.py:
  test_two_workers_cannot_claim_same_job
  test_expired_lease_can_be_reclaimed
  test_lease_heartbeat_and_complete_fail_on_invalid_lease_token
  test_multiple_heartbeats_extend_lease_and_emit_events
  test_chaos_lease_reclaim_recovers

This file closes the real hole: enqueue_job (project_id, dedupe_key)
idempotency was untested at the HTTP and service boundary.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.jobs import enqueue_job


def test_enqueue_job_dedupe_key_is_idempotent(engine):
    key = f"job-dedupe:{uuid4().hex}"
    with begin(engine) as connection:
        first = enqueue_job(
            connection,
            "boss-dogfood",
            "plan.generate",
            {"n": 1},
            dedupe_key=key,
        )
        second = enqueue_job(
            connection,
            "boss-dogfood",
            "plan.generate",
            {"n": 2},
            dedupe_key=key,
        )
        assert first["created"] is True
        assert second["created"] is False
        assert first["id"] == second["id"]
        count = connection.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE project_id = :p AND dedupe_key = :k"
            ),
            {"p": "boss-dogfood", "k": key},
        ).scalar_one()
        assert int(count) == 1


def test_http_job_create_dedupes_on_dedupe_key(client):
    key = f"http-job-dedupe:{uuid4().hex}"
    first = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"step": 1},
            "dedupe_key": key,
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/v1/jobs",
        json={
            "project_id": "boss-dogfood",
            "kind": "plan.generate",
            "payload": {"step": 2},
            "dedupe_key": key,
        },
    )
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    listed = client.get("/v1/jobs", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200, listed.text
    matching = [item for item in listed.json()["items"] if item["id"] == first.json()["id"]]
    assert len(matching) == 1
