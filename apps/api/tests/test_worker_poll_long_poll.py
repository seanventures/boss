"""POST /v1/worker/poll with `wait_ms` (P9a-5, BOSSAPP-1097): the request is held up to wait_ms,
answers `long_poll: true` so a worker knows the API honoured it (an older API answers without
it and the worker falls back to its poll interval), still claims at once when a job is queued,
and refuses a wait over the 25 s ceiling."""
from __future__ import annotations

import time


def test_a_poll_with_wait_ms_is_held_and_says_so(client):
    started = time.monotonic()
    got = client.post("/v1/worker/poll", json={
        "worker_id": "w-longpoll", "capabilities": ["test.nothing-queued-here"],
        "lease_ttl_seconds": 60, "wait_ms": 400})
    elapsed = time.monotonic() - started
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "idle" and body["job"] is None
    assert body["long_poll"] is True and body["waited_ms"] >= 350
    assert elapsed >= 0.35


def test_a_poll_without_wait_ms_answers_at_once_as_before(client):
    got = client.post("/v1/worker/poll", json={
        "worker_id": "w-plain", "capabilities": ["test.nothing-queued-here"],
        "lease_ttl_seconds": 60})
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "idle" and body["long_poll"] is False


def test_a_queued_job_is_claimed_at_once_even_with_a_long_wait(client):
    created = client.post("/v1/jobs", json={"project_id": "boss-dogfood",
                                            "kind": "test.longpoll-claim"})
    assert created.status_code == 201
    started = time.monotonic()
    got = client.post("/v1/worker/poll", json={
        "worker_id": "w-longpoll", "capabilities": ["test.longpoll-claim"],
        "lease_ttl_seconds": 60, "wait_ms": 20000})
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "leased" and body["job"]["id"] == created.json()["id"]
    assert time.monotonic() - started < 5, "no wait when work is already there"


def test_wait_ms_over_the_ceiling_is_refused(client):
    got = client.post("/v1/worker/poll", json={
        "worker_id": "w", "capabilities": ["x"], "lease_ttl_seconds": 60, "wait_ms": 60000})
    assert got.status_code == 422
