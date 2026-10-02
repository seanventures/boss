"""A worker restart (a deploy) must not use up a job's attempts.

On boss-1 two deploys during one long build re-leased its job twice, and each re-lease added an
attempt: the build reached attempt 3 of 3 without ever failing, and one more deploy would have
deadlettered it. A stopping worker now says it is handing the job back, and the attempt the
reclaim will add is given back first.
"""
from uuid import uuid4

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services.jobs import JobLeaseService, enqueue_job

PROJECT = "boss-dogfood"


def _leased(engine):
    with engine.begin() as connection:
        job = enqueue_job(connection, PROJECT, "investigation.run", {"x": uuid4().hex},
                          dedupe_key=f"handback:{uuid4().hex}")
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=PROJECT, job_id=job["id"], worker_id="w1",
            lease_ttl_seconds=300)
    return job["id"], lease["lease_token"]


def _attempt(engine, job_id):
    with engine.connect() as connection:
        return connection.execute(text("SELECT attempt FROM jobs WHERE id = :j"),
                                  {"j": job_id}).scalar_one()


def test_handing_a_job_back_gives_back_the_attempt_its_reclaim_will_take(client, engine):
    job_id, token = _leased(engine)
    before = _attempt(engine, job_id)
    response = client.post("/v1/worker/heartbeat", json={
        "worker_id": "w1", "job_id": job_id, "lease_token": token, "lease_ttl_seconds": 5,
        "handing_back": True})
    assert response.status_code == 200, response.text
    assert _attempt(engine, job_id) == before - 1, "the reclaim's +1 then nets to zero"
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM events WHERE entity_id = :j AND event_type = 'job.handed_back'"),
            {"j": job_id}).scalar_one() == 1


def test_an_ordinary_heartbeat_leaves_the_attempt_alone(client, engine):
    job_id, token = _leased(engine)
    before = _attempt(engine, job_id)
    response = client.post("/v1/worker/heartbeat", json={
        "worker_id": "w1", "job_id": job_id, "lease_token": token, "lease_ttl_seconds": 60})
    assert response.status_code == 200, response.text
    assert _attempt(engine, job_id) == before
