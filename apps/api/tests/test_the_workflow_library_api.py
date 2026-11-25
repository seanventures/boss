"""The Workflow Library over HTTP (design PR #287, BOSSAPP-1296), as the console drives it.

Browse (tags, steps, inputs, the projects you may add to), Add to project (the API gateway
onboarding template installs as an editable copy), Edit a copy (a Save on the installed copy
keeps the change and marks it Modified), and Share to Library: the publish-time checks run, and
sharing org-wide needs the org-level `workflow:share` — a project operator cannot share, an org
admin can, and what they share is listed in the org's other project.
"""
from __future__ import annotations

import copy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import workflows

PROJECT = "boss-dogfood"
PACK = "api-gateway-onboarding"
KEY = "api_gateway_onboarding"


def _post(client, path: str, project=PROJECT, **body):
    return client.post(f"/v1/delivery-workflows/marketplace/{path}",
                       json={"project_id": project, "actor": "ceo", **body})


def _library(client, project=PROJECT) -> dict:
    r = client.get("/v1/delivery-workflows/marketplace", params={"project_id": project})
    assert r.status_code == 200, r.text
    return r.json()


def _workflows(client, project=PROJECT) -> dict:
    r = client.get("/v1/delivery-workflows", params={"project_id": project})
    assert r.status_code == 200, r.text
    return {w["workflow_key"]: w for w in r.json()["items"]}


def test_the_library_lists_the_onboarding_template_with_what_a_person_picks_by(client):
    body = _library(client)
    pack = {p["id"]: p for p in body["items"]}[PACK]
    assert pack["source"] == "boss" and pack["name"] == "API gateway onboarding"
    assert pack["tags"] == ["gateway", "onboarding", "config"]
    (wf,) = pack["workflows"]
    assert wf["key"] == KEY and wf["step_count"] >= 5
    assert "Start from a file" in wf["inputs"] and wf["objective"]
    assert isinstance(pack["installed_in"], list)
    # The projects a person may Add to: this one at least, each with its name.
    assert PROJECT in {p["id"] for p in body["projects"]}
    assert all(p["name"] for p in body["projects"])


def test_add_to_project_then_edit_the_copy(client):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": PROJECT, "actor": "ceo"})
    r = _post(client, "install", pack_id=PACK)
    assert r.status_code == 200, r.text
    assert [i["key"] for i in r.json()["installed"]] == [KEY]
    item = _workflows(client)[KEY]
    assert item["is_catalogue"] is False and item["marketplace"]["pack_id"] == PACK
    # The standard it was made from is still there, as a standard.
    assert _workflows(client)["onboard_config_file"]["is_catalogue"] is True
    assert PROJECT in {p["id"]: p for p in _library(client)["items"]}[PACK]["installed_in"]

    # EDIT A COPY: the editor saves the installed copy like any workflow of yours.
    spec = client.get(f"/v1/delivery-workflows/{KEY}/spec", params={"project_id": PROJECT}).json()
    assert spec["marketplace"]["pack_name"] == "API gateway onboarding"
    changed = copy.deepcopy({k: v for k, v in spec["spec"].items()
                             if k != workflows.AGENT_RULES_KEY})
    next(s for s in changed["steps"] if s["id"] == "staging")["title"] = "Northgate staging"
    saved = client.post("/v1/delivery-workflows/publish",
                        json={"project_id": PROJECT, "actor": "ceo", "spec": changed,
                              "name": "API gateway onboarding",
                              "definition_id": spec["definition_id"]})
    assert saved.status_code == 200, saved.text
    after = client.get(f"/v1/delivery-workflows/{KEY}/spec", params={"project_id": PROJECT}).json()
    assert after["marketplace"]["modified"] is True
    assert next(s for s in after["spec"]["steps"] if s["id"] == "staging")["title"] == \
        "Northgate staging"


def test_share_takes_tags_and_unknown_shapes_are_refused(client):
    r = _post(client, "publish", definition_id="wfd_none", tags="gateway")
    assert r.status_code == 422, r.text


# ── Sharing org-wide is an org-level permission ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def id_client():
    return TestClient(main.app, raise_server_exceptions=False)


def _own_workflow(engine, project: str) -> str:
    """A workflow of the project's own: one notify step (no agent, nothing to hold)."""
    with engine.begin() as c:
        d = workflows.create_definition(
            c, project_id=project, workflow_key=f"lumen_review_{uuid4().hex[:6]}",
            name="Lumen Pay partner review", description="Tell the team a partner file landed.",
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(c, project_id=project, definition_id=d["id"], actor="ceo",
                                  spec={"entry_step": "tell", "steps": [{
                                      "id": "tell", "kind": "capability", "next": [],
                                      "binding": {"capability": "notify",
                                                  "inputs": {"message": "landed"}}}]})
    return d["id"]


def test_a_project_operator_cannot_share_to_the_org_and_an_org_admin_can(
        world, id_client, engine):
    definition_id = _own_workflow(engine, world.project_a)
    operator = world.person(f"lib-operator-{uuid4().hex[:4]}")
    world.bind(operator, "operator", project=world.project_a)
    admin = world.person(f"lib-admin-{uuid4().hex[:4]}")
    world.bind(admin, "admin")
    body = {"project_id": world.project_a, "definition_id": definition_id,
            "tags": ["Gateway", "review"]}

    with world.acting_as(world.ctx(operator)):
        refused = id_client.post("/v1/delivery-workflows/marketplace/publish", json=body)
    assert refused.status_code == 403, refused.text
    assert "workflow:share" in refused.text

    with world.acting_as(world.ctx(admin)):
        shared = id_client.post("/v1/delivery-workflows/marketplace/publish", json=body)
        assert shared.status_code == 200, shared.text
        pack = shared.json()["pack"]
        assert pack["tags"] == ["gateway", "review"]
        # The org's other project sees it, labelled as the team's.
        other = id_client.get("/v1/delivery-workflows/marketplace",
                              params={"project_id": world.project_b})
        assert other.status_code == 200, other.text
        listed = {p["id"]: p for p in other.json()["items"]}[pack["id"]]
        assert listed["source"] == "org" and listed["tags"] == ["gateway", "review"]
        assert {p["id"] for p in other.json()["projects"]} >= {world.project_a, world.project_b}

    with world.acting_as(world.ctx(operator)):
        unshare = id_client.post("/v1/delivery-workflows/marketplace/unpublish",
                                 json={"project_id": world.project_a, "pack_id": pack["id"]})
    assert unshare.status_code == 403, unshare.text
    with engine.begin() as c:
        c.execute(text("DELETE FROM workflow_pack_versions WHERE pack_id = :p"), {"p": pack["id"]})
        c.execute(text("DELETE FROM workflow_packs WHERE id = :p"), {"p": pack["id"]})


def test_the_project_picker_lists_only_projects_a_person_may_add_to(world, id_client):
    viewer_elsewhere = world.person(f"lib-op-a-{uuid4().hex[:4]}")
    world.bind(viewer_elsewhere, "operator", project=world.project_a)
    world.bind(viewer_elsewhere, "viewer", project=world.project_b)
    with world.acting_as(world.ctx(viewer_elsewhere)):
        r = id_client.get("/v1/delivery-workflows/marketplace",
                          params={"project_id": world.project_a})
    assert r.status_code == 200, r.text
    ids = {p["id"] for p in r.json()["projects"]}
    assert world.project_a in ids and world.project_b not in ids
