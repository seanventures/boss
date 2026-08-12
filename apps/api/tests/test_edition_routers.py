from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.bootstrap_ports import build_ports
from apps.api.app.config import get_settings
from apps.api.app.routers import mount_edition_routers
from apps.api.app.routers.enterprise import is_enterprise_path


ROOT = Path(__file__).resolve().parents[3]


def _test_app(edition: str) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/code-tasks")
    def community_route():
        return {"surface": "community"}

    @app.get("/v1/account-connect/link")
    def enterprise_route():
        return {"surface": "enterprise"}

    mount_edition_routers(app, edition)
    return app


def test_community_edition_mounts_only_community_routes():
    client = TestClient(_test_app("community"))
    assert client.get("/v1/code-tasks").status_code == 200
    assert client.get("/v1/account-connect/link").status_code == 404


@pytest.mark.parametrize(
    "edition",
    ["cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted"],
)
def test_paid_editions_mount_both_router_sets(edition):
    client = TestClient(_test_app(edition))
    assert client.get("/v1/code-tasks").status_code == 200
    assert client.get("/v1/account-connect/link").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/saml/v2/project/metadata",
        "/scim/v2/project/Users",
        "/v1/enterprise/clusters",
        "/v1/marketplace/listings",
        "/v1/account-connect/link",
        "/v1/self-improvement/proposals",
        # Write-automation / fleet scale stay paid; credentials do not.
        "/v1/changesets",
        "/v1/worker-pools",
        "/v1/autoscaler/plans",
        "/v1/resource-janitor/run",
    ],
)
def test_paid_surface_classification(path):
    assert is_enterprise_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/code-tasks",
        "/v1/code-tasks/run/preview",
        "/v1/previews",
        "/v1/mcp/servers",
        "/v1/skills",
        "/v1/events",
        # Deploying is community. These were paid, which left a CI/CD console that rendered
        # fully and 404'd on every button: /v1/delivery/deploy was free while everything
        # needed to REGISTER a destination was not. Half a workflow behind a licence.
        "/v1/deployments",
        "/v1/deployment-targets",
        "/v1/deployment-targets/deptgt_x/bootstrap",
        "/v1/deployment-targets/deptgt_x/teardown",
        "/v1/deployment-targets/deptgt_x/purge",
        "/v1/deployment-targets/deptgt_x/auto-deploy",
        "/v1/deployment-runs",
        "/v1/deployment-promotions",
        "/v1/deployment-environment-policies",
        # Community must accept third-party model keys and infra API keys, plus
        # basic ProviderOps read inventory (PRD Community). Catalog + accounts +
        # resources were paid and left System Configuration 404ing on dogfood.
        "/v1/provider-definitions",
        "/v1/providers",
        "/v1/providers/pa_1/test",
        "/v1/providers/pa_1/rotate",
        "/v1/resources",
        "/v1/resources/sync",
        "/v1/resource-receipts",
        "/v1/entitlements/sources",
        "/v1/secret-refs",
    ],
)
def test_community_surface_classification(path):
    assert not is_enterprise_path(path)


def test_community_provider_keys_and_inventory_are_one_edition():
    """Credential setup must not land on the opposite side of the licence from inventory.

    Mission Control System Configuration fans out catalog + accounts + resources together.
    When only deploy was free and connections were paid, the console looked broken rather
    than edition-gated. Keys without a catalog/test path are the same class of bug.
    """
    workflow = [
        "/v1/provider-definitions",
        "/v1/providers",
        "/v1/providers/pa_x/test",
        "/v1/resources",
        "/v1/resource-receipts",
        "/v1/provider-health",
    ]
    assert {is_enterprise_path(p) for p in workflow} == {False}


def test_the_whole_delivery_workflow_is_one_edition():
    """Deploy and register must not land on opposite sides of the licence.

    The console calls /v1/delivery/* to deploy and /v1/deployment-targets/* to onboard. When
    those split across editions the UI does not degrade — it breaks, and the failure looks
    like a bug rather than a missing entitlement."""
    workflow = [
        "/v1/delivery/deploy", "/v1/delivery/pipeline", "/v1/delivery/health",
        "/v1/deployment-targets", "/v1/deployment-targets/deptgt_x/bootstrap",
        "/v1/deployment-runs",
    ]
    assert {is_enterprise_path(p) for p in workflow} == {False}


def test_composition_root_changes_implementations_by_edition(engine):
    settings = get_settings()
    previous = settings.boss_edition
    try:
        settings.boss_edition = "community"
        community = build_ports(settings, engine)
        settings.boss_edition = "enterprise"
        enterprise = build_ports(settings, engine)
    finally:
        settings.boss_edition = previous

    # Community persists to Postgres too, and that is deliberate.
    #
    # This used to assert InMemoryPersistence for community — encoding a boundary that made
    # the free tier unable to EXECUTE anything. `POST /v1/code-tasks` wrote the run to
    # Postgres via raw SQL while `POST /v1/code-tasks/step` read the empty in-memory store and
    # returned {"status": "not_found", "done": true}. Not an error, so the worker marked the
    # job succeeded and the run sat at `queued` for ever. Weeks of production runs died that
    # way. Recording work you cannot perform is not a free tier, it is a broken one.
    assert type(community.persistence).__name__ == "PostgresPersistence"
    assert type(enterprise.persistence).__name__ == "PostgresPersistence"
    # What the edition still decides is GOVERNANCE: spend enforcement and entitlements.
    assert type(community.budget).__name__ == "NoopBudgetPort"
    assert type(enterprise.budget).__name__ == "PostgresBudgetPort"


def test_real_enterprise_app_exposes_both_surfaces(client):
    assert client.get("/v1/code-tasks", params={"project_id": "boss-dogfood"}).status_code != 404
    assert client.get("/v1/account-connect/health-checks", params={"project_id": "boss-dogfood"}).status_code != 404


def test_real_community_app_omits_paid_routes():
    environment = os.environ.copy()
    environment["BOSS_EDITION"] = "community"
    environment["PYTHONPATH"] = f"{ROOT}:{ROOT / 'engine'}"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from apps.api.app.main import app; "
                "paths={getattr(route, 'path', '') for route in app.routes}; "
                "assert '/v1/code-tasks' in paths; "
                "assert '/v1/mcp/servers' in paths; "
                "assert '/v1/skills' in paths; "
                "assert '/v1/events' in paths; "
                # Community: third-party + infra API key surfaces (catalog, accounts, inventory).
                "assert '/v1/provider-definitions' in paths; "
                "assert '/v1/providers' in paths; "
                "assert '/v1/resources' in paths; "
                "assert '/v1/resource-receipts' in paths; "
                # Paid: identity, marketplace, write-automation / fleet scale.
                "assert '/v1/account-connect/link' not in paths; "
                "assert '/v1/enterprise/clusters' not in paths; "
                "assert '/v1/marketplace/listings' not in paths; "
                "assert '/v1/self-improvement/proposals' not in paths; "
                "assert '/v1/changesets' not in paths; "
                "assert '/v1/worker-pools' not in paths; "
                "assert '/v1/autoscaler/plans' not in paths; "
                "assert '/v1/resource-janitor/run' not in paths"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_enterprise_event_and_decision_sinks_preserve_postgres_behavior(engine):
    settings = get_settings()
    previous = settings.boss_edition
    try:
        settings.boss_edition = "enterprise"
        ports = build_ports(settings, engine)
    finally:
        settings.boss_edition = previous

    with engine.begin() as connection:
        event_id = ports.events.emit(
            connection,
            "boss-dogfood",
            "ports.contract_test",
            "tester",
            "project",
            "boss-dogfood",
            {"source": "enterprise_sink"},
        )
        decision_id = ports.decisions.create_blocking(
            connection,
            "boss-dogfood",
            None,
            "Verify the persistence adapter?",
            {"action": "hold"},
            {},
            "ports_contract_test",
        )
        event = connection.execute(
            text("SELECT event_type, data FROM events WHERE id = :id"),
            {"id": event_id},
        ).mappings().one()
        decision = connection.execute(
            text("SELECT status, question FROM decisions WHERE id = :id"),
            {"id": decision_id},
        ).mappings().one()

    assert event["event_type"] == "ports.contract_test"
    assert event["data"]["source"] == "enterprise_sink"
    assert decision["status"] == "pending"
    assert decision["question"] == "Verify the persistence adapter?"
