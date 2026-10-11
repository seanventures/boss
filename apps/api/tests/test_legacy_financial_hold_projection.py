"""Actual canonical GET projections and committed legacy hold visibility."""

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authn
from engine.tests.test_legacy_financial_exposure import legacy, policy
from engine.tests.test_legacy_provider_exposure import history


@pytest.fixture
def owned(client, engine):
    world = IdentityWorld.create(engine)
    owner = world.person("legacy-finance-owner")
    world.bind(owner, "owner", project=world.project_a)
    with engine.begin() as c:
        policy(c, world.project_a)
    client.cookies.set(authn.SESSION_COOKIE, "owned-test-session-selector")
    return world, owner


@pytest.mark.parametrize("route", ["/v1/budget/status", "/v1/govern"])
def test_actual_get_exposes_hold_and_commits_idempotent_decision(client, engine, owned, route):
    world, owner = owned
    ident = legacy(engine, world.project_a)
    params = (
        {"scope_type": "project", "scope_id": world.project_a}
        if route.endswith("/status")
        else {"project_id": world.project_a}
    )
    with world.acting_as(world.ctx(owner)):
        response = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 200, response.text
        body = response.json()
        snapshot = body if route.endswith("/status") else body["meter"]
        hold = snapshot["legacy_financial_hold"]
        assert (
            hold["reservation_id"]
            == hold["lease_id"]
            == hold["invocation_id"]
            == hold["receipt_id"]
            == ident
        )
        assert (
            hold["original_estimate_usd_micros"] == 80000
            and hold["original_reservation_status"] == "expired"
        )
        assert hold["decision_id"] and "not an invoice" in hold["message"]
        assert snapshot["actual_usd_micros"] == snapshot["reserved_usd_micros"] == 0
        again = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"}).json()
        assert (again if route.endswith("/status") else again["meter"])[
            "legacy_financial_hold"
        ] == hold
    with engine.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM decisions WHERE project_id=:p AND recommendation->>'action'='reconcile_legacy_model_exposure'"
                ),
                {"p": world.project_a},
            ).scalar_one()
            == 1
        )
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE id=:id"), {"id": ident}
            ).scalar_one()
            == "expired"
        )
    foreign = world.person("foreign-finance-owner")
    world.bind(foreign, "owner", project=world.project_b)
    with world.acting_as(world.ctx(foreign)):
        denied = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"})
        assert denied.status_code in (403, 404), denied.text
        assert ident not in denied.text


@pytest.mark.parametrize("route", ["/v1/budget/status", "/v1/govern"])
def test_actual_get_clear_project_has_explicit_null_hold(client, owned, route):
    world, owner = owned
    params = (
        {"scope_type": "project", "scope_id": world.project_a}
        if route.endswith("/status")
        else {"project_id": world.project_a}
    )
    with world.acting_as(world.ctx(owner)):
        response = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body if route.endswith("/status") else body["meter"])[
            "legacy_financial_hold"
        ] is None
        assert (body if route.endswith("/status") else body["meter"])[
            "legacy_provider_hold"
        ] is None


@pytest.mark.parametrize("route", ["/v1/budget/status", "/v1/govern"])
@pytest.mark.parametrize("also_model", [False, True])
def test_actual_get_exposes_provider_hold_without_overwriting_model(
    client, engine, owned, route, also_model
):
    world, owner = owned
    provider = history(engine, world.project_a)
    model = legacy(engine, world.project_a) if also_model else None
    params = (
        {"scope_type": "project", "scope_id": world.project_a}
        if route.endswith("/status")
        else {"project_id": world.project_a}
    )
    with world.acting_as(world.ctx(owner)):
        response = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 200, response.text
        body = response.json()
        snapshot = body if route.endswith("/status") else body["meter"]
        hold = snapshot["legacy_provider_hold"]
        assert set(hold) == {
            "reservation_id",
            "changeset_id",
            "operation_id",
            "resource_receipt_id",
            "receipt_id",
            "original_reservation_status",
            "original_estimate_usd_micros",
            "decision_id",
            "message",
        }
        assert (
            hold["reservation_id"] == provider.reservation and hold["changeset_id"] == provider.cs
        )
        assert (
            hold["original_estimate_usd_micros"] == 80000
            and hold["original_reservation_status"] == "released"
        )
        assert hold["decision_id"] and "not an invoice" in hold["message"]
        assert snapshot["actual_usd_micros"] == snapshot["reserved_usd_micros"] == 0
        if also_model:
            assert snapshot["legacy_financial_hold"]["invocation_id"] == model
            assert snapshot["legacy_financial_hold"]["decision_id"] != hold["decision_id"]
        else:
            assert snapshot["legacy_financial_hold"] is None
        again = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"}).json()
        assert (again if route.endswith("/status") else again["meter"])[
            "legacy_provider_hold"
        ] == hold
    with engine.connect() as c:
        saved = (
            c.execute(
                text(
                    "SELECT operation_id,receipt_id FROM resource_receipts WHERE id=:id AND project_id=:p AND changeset_id=:cs"
                ),
                {"id": hold["resource_receipt_id"], "p": world.project_a, "cs": provider.cs},
            )
            .mappings()
            .one()
        )
        assert (
            saved["operation_id"] == hold["operation_id"]
            and saved["receipt_id"] == hold["receipt_id"]
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM decisions WHERE project_id=:p AND recommendation->>'action'='reconcile_legacy_provider_exposure'"
                ),
                {"p": world.project_a},
            ).scalar_one()
            == 1
        )
    foreign = world.person("foreign-provider-owner")
    world.bind(foreign, "owner", project=world.project_b)
    with world.acting_as(world.ctx(foreign)):
        denied = client.get(route, params=params, headers={"Sec-Fetch-Site": "same-origin"})
        assert denied.status_code in (403, 404) and provider.cs not in denied.text
