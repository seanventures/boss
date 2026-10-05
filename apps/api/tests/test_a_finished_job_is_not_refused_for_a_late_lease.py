"""409 from the worker settle routes means "this job is not yours" — and only that.

`/v1/worker/complete` answered 409 whenever the lease had passed its expiry, even when no other
worker had taken the job: same owner, same token, still running. The worker had finished the work;
the API threw the result away, the job sat until the reclaimer re-ran it from the top.

Now an expired lease nobody reclaimed is still settled by its holder (complete, fail, hand back).
A lease another worker holds, a wrong token, or a job already settled is still a 409.
"""
from __future__ import annotations

from sqlalchemy import text


def _leased(client, worker="w-late"):
    created = client.post("/v1/jobs", json={"project_id": "boss-dogfood",
                                            "kind": "test.late-lease"})
    assert created.status_code == 201, created.text
    leased = client.post("/v1/worker/poll", json={
        "worker_id": worker, "capabilities": ["test.late-lease"], "lease_ttl_seconds": 60})
    assert leased.status_code == 200, leased.text
    return leased.json()["job"]


def _expire(engine, job_id):
    with engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '5 seconds' "
                                "WHERE id = :i"), {"i": job_id})


def _settle(client, path, job, worker="w-late", **extra):
    return client.post(path, json={"worker_id": worker, "job_id": job["id"],
                                   "lease_token": job["lease_token"], **extra})


def test_an_expired_lease_nobody_took_is_still_completed_by_its_holder(client, engine):
    job = _leased(client)
    _expire(engine, job["id"])
    done = _settle(client, "/v1/worker/complete", job, result={})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "succeeded"


def test_an_expired_lease_nobody_took_can_be_failed_or_handed_back(client, engine):
    job = _leased(client)
    _expire(engine, job["id"])
    back = _settle(client, "/v1/worker/heartbeat", job, lease_ttl_seconds=5, handing_back=True)
    assert back.status_code == 200, back.text
    # A normal heartbeat on an expired lease still refuses: it would extend work, not settle it.
    _expire(engine, job["id"])
    assert _settle(client, "/v1/worker/heartbeat", job,
                   lease_ttl_seconds=60).status_code == 409
    failed = _settle(client, "/v1/worker/fail", job, error={"message": "x"}, retryable=True,
                     retry_delay_seconds=0)
    assert failed.status_code == 200, failed.text


def test_a_job_someone_else_reclaimed_is_still_a_409(client, engine):
    job = _leased(client)
    _expire(engine, job["id"])
    taken = client.post("/v1/worker/poll", json={
        "worker_id": "w-other", "capabilities": ["test.late-lease"], "lease_ttl_seconds": 60})
    assert taken.json()["job"]["id"] == job["id"]
    refused = _settle(client, "/v1/worker/complete", job, result={})
    assert refused.status_code == 409
    assert _settle(client, "/v1/worker/heartbeat", job, lease_ttl_seconds=5,
                   handing_back=True).status_code == 409


def test_a_settled_job_is_still_a_409(client, engine):
    job = _leased(client)
    assert _settle(client, "/v1/worker/complete", job, result={}).status_code == 200
    assert _settle(client, "/v1/worker/complete", job, result={}).status_code == 409
