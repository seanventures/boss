"""Enforced identity and real local authority; only provider responses are scripted."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.setup_helpers import fakes
from boss_engine.services import setup_actions, setup_checks


@pytest.fixture()
def owner(engine, monkeypatch):
    world = IdentityWorld.create(engine)
    person = world.person("connection owner")
    world.bind(person, "owner", project=world.project_a)
    fakes(monkeypatch)
    return world, person


def request(world, **values):
    return {
        "project_id": world.project_a,
        "provider": "openrouter",
        "kind": "api_key",
        "api_key": "sk-stub-ok",
        "model": "stub/small",
        "idempotency_key": uuid4().hex,
        **values,
    }


def test_owner_intent_replays_across_process_cache_and_records_canonical_actor(
    client, engine, owner, monkeypatch
):
    world, person = owner
    calls = []
    fakes(monkeypatch, model_calls=calls)
    # The enforced legacy boundary ignores a forged claimed actor and resolves
    # the signed-in principal. It must never attribute this proof to the claim.
    payload = request(world, actor=world.person("claimed other person").id)
    with world.acting_as(world.ctx(person)):
        first = client.post("/v1/setup/models", json=payload)
        assert first.status_code == 200, first.text
        first_checked_at = first.json()["models"][0]["checked_at"]
        setup_actions._PRESSES.clear()  # A different API process has no short cache.
        again = client.post("/v1/setup/models", json=payload)
        assert again.status_code == 200, again.text
        assert again.json()["models"][0]["checked_at"] == first_checked_at
    assert len(calls) == 1
    assert "sk-stub-ok" not in first.text + again.text
    with engine.begin() as c:
        assert (
            c.execute(
                text(
                    "SELECT payload->>'actor' FROM jobs WHERE project_id=:p "
                    "AND kind='connection.proof'"
                ),
                {"p": world.project_a},
            ).scalar_one()
            == person.id
        )
        assert (
            c.execute(
                text(
                    "SELECT result->>'connection_actor' FROM receipts WHERE project_id=:p "
                    "AND result ? 'connection_actor'"
                ),
                {"p": world.project_a},
            ).scalar_one()
            == person.id
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"),
                {"p": world.project_a},
            ).scalar_one()
            == 1
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM entitlement_sources WHERE project_id=:p "
                    "AND COALESCE(connection_metadata->>'proof_only','false')<>'true'"
                ),
                {"p": world.project_a},
            ).scalar_one()
            == 1
        )


def test_replaying_an_old_intent_cannot_restore_a_replaced_key(client, engine, owner, monkeypatch):
    world, person = owner
    calls = []
    fakes(monkeypatch, model_calls=calls)
    original = request(world)
    with world.acting_as(world.ctx(person)):
        first = client.post("/v1/setup/models", json=original)
        assert first.status_code == 200, first.text
        source = first.json()["models"][0]["source_id"]
        replacement = client.post("/v1/setup/models", json=request(world, source_id=source))
        assert replacement.status_code == 200, replacement.text
        current_time = replacement.json()["models"][0]["checked_at"]
        stale = client.post("/v1/setup/models", json=original)
        assert stale.status_code == 409, stale.text
    assert len(calls) == 2
    with engine.begin() as c:
        assert (
            c.execute(
                text(
                    "SELECT connection_metadata->'last_check'->>'at' FROM entitlement_sources "
                    "WHERE project_id=:p AND source_id=:s"
                ),
                {"p": world.project_a, "s": source},
            ).scalar_one()
            == current_time
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM entitlement_sources WHERE project_id=:p "
                    "AND COALESCE(connection_metadata->>'proof_only','false')<>'true'"
                ),
                {"p": world.project_a},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
@pytest.mark.parametrize("route", ["/v1/setup/models", "/v1/setup/coding-agents/credential"])
def test_supplied_subscription_material_is_held_before_mutation_or_contact(
    client, engine, owner, monkeypatch, provider, route
):
    from boss_engine.config import Settings
    from boss_engine.services import setup_service
    from boss_engine.services.secrets_service import put_secret, resolve_secret_ref

    world, person = owner
    with engine.begin() as c:
        original = put_secret(
            c,
            Settings(),
            project_id=world.project_a,
            name="owned_subscription_session",
            value="owned-working-session",
            secret_kind="api_token",
            backend="database",
            actor=person.id,
        )
    monkeypatch.setattr(
        setup_service,
        "store_coding_agent_credential",
        lambda *_a, **_k: pytest.fail("blocked import overwrote canonical store"),
    )
    monkeypatch.setattr(
        setup_service,
        "prove_coding_agent",
        lambda *_a, **_k: pytest.fail("blocked import contacted CLI"),
    )
    payload = request(
        world, provider=provider, kind="subscription", api_key="", credential="owned-candidate-one"
    )
    with world.acting_as(world.ctx(person)):
        for value in ("owned-candidate-one", "owned-candidate-two", "owned-candidate-one"):
            response = client.post(route, json={**payload, "credential": value})
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["headline"] == "CLI credential import held"
            assert value not in response.text
            if route.endswith("/credential"):
                assert response.headers["Deprecation"] == "true"
    with engine.begin() as c:
        assert (
            resolve_secret_ref(c, original["secret_ref_id"], Settings()) == "owned-working-session"
        )
        assert (
            c.execute(
                text("SELECT status FROM secret_refs WHERE id=:r"), {"r": original["secret_ref_id"]}
            ).scalar_one()
            == "active"
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM jobs WHERE project_id=:p"), {"p": world.project_a}
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"),
                {"p": world.project_a},
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM receipts WHERE project_id=:p"), {"p": world.project_a}
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("forbidden", ["foreign_project", "viewer"])
def test_forbidden_requests_make_no_provider_contact(client, engine, owner, monkeypatch, forbidden):
    world, person = owner
    calls = []
    fakes(monkeypatch, model_calls=calls)
    payload = request(world)
    if forbidden == "foreign_project":
        payload["project_id"] = world.project_b
    elif forbidden == "viewer":
        person = world.person("connection viewer")
        world.bind(person, "viewer", project=world.project_a)
    with world.acting_as(world.ctx(person)):
        response = client.post("/v1/setup/models", json=payload)
    assert response.status_code == 403, response.text
    assert calls == []
    with engine.begin() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"),
                {"p": payload["project_id"]},
            ).scalar_one()
            == 0
        )


def test_changed_payload_cannot_hide_behind_the_short_cache(client, owner, monkeypatch):
    world, person = owner
    calls = []
    fakes(monkeypatch, model_calls=calls)
    payload = request(world)
    with world.acting_as(world.ctx(person)):
        assert client.post("/v1/setup/models", json=payload).status_code == 200
        changed = client.post("/v1/setup/models", json={**payload, "api_key": "sk-stub-401"})
    assert changed.status_code in (409, 422), changed.text
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["timeout", "rejected"])
def test_failed_replacement_keeps_the_working_connection_and_unknown_hold(
    client, engine, owner, monkeypatch, outcome
):
    world, person = owner
    with world.acting_as(world.ctx(person)):
        first = client.post("/v1/setup/models", json=request(world))
        assert first.status_code == 200, first.text
    with engine.begin() as c:
        saved = (
            c.execute(
                text(
                    "SELECT source_id,credential_secret_ref_id FROM entitlement_sources "
                    "WHERE project_id=:p AND COALESCE(connection_metadata->>'proof_only','false')<>'true'"
                ),
                {"p": world.project_a},
            )
            .mappings()
            .one()
        )
    if outcome == "timeout":
        monkeypatch.setattr(
            setup_checks,
            "default_http",
            lambda *a, **k: setup_checks.HttpAnswer(0, {}, None, "owned timeout"),
        )
    payload = request(world, api_key="sk-stub-401", source_id=saved["source_id"])
    with world.acting_as(world.ctx(person)):
        response = client.post("/v1/setup/models", json=payload)
    assert response.status_code == (409 if outcome == "timeout" else 422), response.text
    with engine.begin() as c:
        current = (
            c.execute(
                text(
                    "SELECT credential_secret_ref_id,connection_metadata FROM entitlement_sources "
                    "WHERE project_id=:p AND source_id=:s"
                ),
                {"p": world.project_a, "s": saved["source_id"]},
            )
            .mappings()
            .one()
        )
        assert current["credential_secret_ref_id"] == saved["credential_secret_ref_id"]
        assert current["connection_metadata"]["last_check"]["ok"] is True
        if outcome == "timeout":
            assert response.json()["detail"]["reconciliation_required"] is True
            assert response.json()["detail"]["receipt_id"]
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM budget_reservations WHERE project_id=:p "
                        "AND status='active' AND amount_usd_micros>0"
                    ),
                    {"p": world.project_a},
                ).scalar_one()
                == 1
            )
