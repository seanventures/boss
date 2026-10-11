""""Someone wants Go — can't we fail faster than the Build stage?" (services.stacks)

A building workflow started on a brief that asks for a stack BOSS cannot build is refused at
Start with `stack_unsupported`, in words naming what BOSS does build, unless the person starts it
anyway. A workflow with no build step may discuss Go freely. The console asks the same question
before Start through GET /v1/stacks.
"""
from uuid import uuid4

import pytest

from boss_engine.services import workflows

PROJECT = "boss-dogfood"


def _workflow(engine, steps) -> str:
    key = f"stack_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name="Stack", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": steps[0]["id"], "steps": steps})
    return key


DECIDE = {"id": "decide", "kind": "capability",
          "binding": {"typed_decision": {"question": "Go?", "options": {"yes": "Go", "no": "Stop"}},
                      "inputs": {"state": {"from_input": "directive"}}}}


@pytest.fixture
def building(engine, monkeypatch):
    # The registry asks whether the version builds; this one does.
    from boss_engine.services import stacks

    monkeypatch.setattr(stacks, "_workflow_builds", lambda connection, version_id: True)
    return _workflow(engine, [DECIDE])


@pytest.fixture
def deciding(engine):
    return _workflow(engine, [DECIDE])


def _start(client, key, directive, **extra):
    return client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "input": {"directive": directive, "title": "Helpdesk"}, **extra})


def test_go_is_refused_at_start_in_words(client, building):
    r = _start(client, building, "Build the helpdesk backend in Go")
    assert r.status_code == 409, r.text
    detail = r.json().get("detail") or r.json()
    assert detail["error"] == "stack_unsupported"
    assert "Go, which BOSS cannot build" in detail["message"]
    assert "BOSS builds Python" in detail["message"]
    assert any(l["key"] == "python" for l in detail["supported"]["languages"])


def test_started_anyway_on_the_persons_word(client, building):
    r = _start(client, building, "Build the helpdesk backend in Go", allow_unsupported_stack=True)
    assert "stack_unsupported" not in r.text, r.text


def test_a_buildable_brief_is_not_refused(client, building):
    r = _start(client, building, "A FastAPI backend and a React front end")
    assert "stack_unsupported" not in r.text, r.text


def test_a_workflow_that_does_not_build_may_discuss_go(client, deciding):
    r = _start(client, deciding, "Should we rewrite the helpdesk in Go?")
    assert r.status_code == 200, r.text


def test_the_console_can_ask_before_start(client):
    r = client.get("/v1/stacks", params={"project_id": PROJECT, "text": "build it in Go"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["check"]["ok"] is False and body["check"]["unsupported"] == ["Go"]
    assert "Python" in body["sentence"]
    ok = client.get("/v1/stacks", params={"project_id": PROJECT, "text": "a Flask API"}).json()
    assert ok["check"]["ok"] is True
