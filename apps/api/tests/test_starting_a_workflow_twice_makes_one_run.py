"""Pressing Stop-and-run-again twice started two workflows on boss-1 (2026-10-02).

The start endpoint made a fresh dedupe key per request, so nothing tied the second press to the
first. The console now sends one key per intent (`replace:<run>`), and the server returns the
run that key already started — sequentially, and when two identical starts race.
"""
import threading
from uuid import uuid4

import pytest

from boss_engine.services import workflows

PROJECT = "boss-dogfood"


@pytest.fixture
def workflow_key(engine):
    key = f"twice_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name="Twice", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "decide", "steps": [{
                "id": "decide", "kind": "capability",
                "binding": {"typed_decision": {"question": "Go?",
                                               "options": {"yes": "Go", "no": "Stop"}},
                            "inputs": {"state": {"from_input": "ticket"}}}}]})
    return key


def _start(client, workflow_key, key):
    return client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "input": {"ticket": "x"}, "dedupe_key": key,
    })


def test_the_same_key_twice_returns_the_same_run(client, workflow_key):
    key = f"replace:wfr_{uuid4().hex[:12]}"
    first, second = _start(client, workflow_key, key), _start(client, workflow_key, key)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["run"]["id"] == first.json()["run"]["id"]
    assert second.json()["run"].get("duplicate") is True


def test_two_identical_starts_racing_make_one_run(client, workflow_key):
    key = f"replace:wfr_{uuid4().hex[:12]}"
    results = []

    def press():
        results.append(_start(client, workflow_key, key))

    threads = [threading.Thread(target=press) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["run"]["id"] for r in results}) == 1
