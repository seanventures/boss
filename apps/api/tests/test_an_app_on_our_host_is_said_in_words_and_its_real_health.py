"""An app deployed to one of our hosts reads as one in Cloud › Resources (independent walk, 18).

A governed host deploy records its app as a resource with provider `ssh_host` and type
`ssh_host.app`. The resource drawer printed both as its provider and "region", and its health
was the word written when it deployed ("Up") while Observability, reading the probes, said Down.
It is placed on its host by name now ("SSH host", host "edge-2", "App on a host") and its
health is the verdict Observability shows.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=True)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO external_resources (id, project_id, provider, resource_type, external_id, "
            "name, environment, health_status) VALUES ('xr_api', :p, 'ssh_host', 'ssh_host.app', "
            "'app:t_down', 'api', 'prod', 'healthy') ON CONFLICT DO NOTHING"), {"p": HOME})
    yield
    with engine.begin() as c:
        c.execute(text("DELETE FROM external_resources WHERE id = 'xr_api'"))
        clear(c, HOME)


def test_a_host_app_resource_names_its_host_and_reads_its_probes(client, world):
    rows = client.get("/v1/infra/cloud", params={"project_id": HOME, "view": "resources"}).json()
    row = next(r for r in rows["rows"] if r["id"] == "xr_api")
    assert row["provider_name"] == "SSH host"
    assert row["provider"] == "ssh_host", "the filter still keys on the provider id"
    assert row["region"] == "edge-2"
    assert row["service"] == "App on a host"
    pulse = client.get("/v1/observability/apps", params={"project_id": HOME}).json()
    obs = next(r for r in pulse["rows"] if r["target_id"] == "t_down")
    assert row["health"]["word"] == obs["word"] == "Down", (row["health"], obs["word"])
