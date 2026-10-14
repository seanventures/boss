"""Repo and models lists only sources somebody connected, never a retired proof (BOSSAPP-539).

Each Connect stages a temporary proof-<hash> source for its one paid call. The console reads
GET /v1/entitlements/sources and showed every retired one with Replace key / Disconnect
session. The rows are kept for the audit trail; the list leaves them out. Enforced identity and
real local authority; only provider responses are scripted.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.setup_helpers import fakes
from boss_engine.services import setup_checks


@pytest.fixture()
def owner(engine, monkeypatch):
    world = IdentityWorld.create(engine)
    person = world.person("connection owner")
    world.bind(person, "owner", project=world.project_a)
    fakes(monkeypatch)
    return world, person


def connect(client, world, **values):
    return client.post("/v1/setup/models", json={
        "project_id": world.project_a, "provider": "openrouter", "kind": "api_key",
        "api_key": "sk-stub-ok", "model": "stub/small", "idempotency_key": uuid4().hex,
        **values})


def listed(client, world):
    response = client.get(f"/v1/entitlements/sources?project_id={world.project_a}")
    assert response.status_code == 200, response.text
    return [item["source_id"] for item in response.json()["items"]]


def test_connects_leave_no_proof_source_in_the_sources_list(client, engine, owner, monkeypatch):
    world, person = owner
    with world.acting_as(world.ctx(person)):
        assert connect(client, world).status_code == 200
        monkeypatch.setattr(
            setup_checks, "default_http",
            lambda *a, **k: setup_checks.HttpAnswer(404, {}, {"type": "error", "error": {
                "type": "not_found_error", "message": "model: claude-sonnet-9"}}))
        assert connect(client, world, provider="anthropic",
                       model="claude-sonnet-9").status_code == 422
        sources = listed(client, world)
    assert not [s for s in sources if s.startswith("proof-")]
    with engine.begin() as c:
        kept = c.execute(text(
            "SELECT status FROM entitlement_sources WHERE project_id=:p "
            "AND source_id LIKE 'proof-%'"), {"p": world.project_a}).scalars().all()
        named = c.execute(text(
            "SELECT data->>'summary' FROM events WHERE project_id=:p "
            "AND event_type='connection.proof_retired' ORDER BY created_at"),
            {"p": world.project_a}).scalars().all()
    # Kept for the audit trail, retired, and named in history by what each one proved.
    assert kept == ["inactive", "inactive"]
    assert named[-1] == "Anthropic refused: model not found (claude-sonnet-9)."
    assert len(named) == 2
